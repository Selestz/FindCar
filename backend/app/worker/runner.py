import asyncio
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.diagnostics import failure_context
from app.domain.models import NormalizedListing, UnifiedSearchFilters
from app.matching.synthetic import prepare_images
from app.services.ingestion import ingest
from app.services.matching import match_listing
from app.services.monitoring import locked_limit, request_source, retry_delay, schedule_due
from app.services.searches import next_refresh
from app.sources.base import SourceFailure
from app.sources.registry import adapter_for

log = logging.getLogger("findcar.worker")


def claim() -> dict[str, Any] | None:
    with engine().begin() as conn:
        conn.execute(
            insert(t.heartbeats)
            .values(worker_id="worker", last_seen_at=t.now())
            .on_conflict_do_update(index_elements=["worker_id"], set_={"last_seen_at": t.now()})
        )
        candidates = (
            conn.execute(
                sa.select(t.jobs)
                .where(
                    sa.or_(
                        sa.and_(t.jobs.c.state == "queued", t.jobs.c.not_before <= t.now()),
                        sa.and_(t.jobs.c.state == "running", t.jobs.c.lease_until < t.now()),
                    )
                )
                .order_by(t.jobs.c.not_before, t.jobs.c.id)
                .with_for_update(skip_locked=True)
                .limit(20)
            )
            .mappings()
            .all()
        )
        for row in candidates:
            limit = locked_limit(conn, row["source"])
            if limit["cooldown_until"] and limit["cooldown_until"] > t.now():
                conn.execute(
                    t.jobs.update()
                    .where(t.jobs.c.id == row["id"])
                    .values(not_before=limit["cooldown_until"], state="queued", lease_until=None)
                )
                continue
            if conn.execute(
                sa.select(t.jobs.c.id).where(
                    t.jobs.c.source == row["source"],
                    t.jobs.c.id != row["id"],
                    t.jobs.c.state == "running",
                    t.jobs.c.lease_until > t.now(),
                )
            ).first():
                continue
            token = uuid.uuid4()
            conn.execute(
                t.jobs.update()
                .where(t.jobs.c.id == row["id"])
                .values(
                    state="running",
                    lease_token=token,
                    lease_until=t.now() + timedelta(seconds=settings().job_timeout_seconds + 30),
                    attempt=row["attempt"] + 1,
                )
            )
            conn.execute(t.runs.update().where(t.runs.c.id == row["run_id"]).values(outcome="running"))
            return dict(row) | {"lease_token": token, "attempt": row["attempt"] + 1}
        return None


async def fetch(
    source: str, filters: dict[str, Any], progress: dict[str, Any] | None = None
) -> tuple[list[NormalizedListing], list[str]]:
    adapter = adapter_for(source)
    adapter.enrich_details = False  # The job-wide queue chooses detail checks after catalog discovery.
    result: list[NormalizedListing] = []
    warnings: list[str] = []
    progress = progress if progress is not None else {}
    resume = max(2, min(progress.get("scan_next_page", 2), settings().search_max_depth))
    progress.update(
        records=result,
        warnings=warnings,
        prepared={},
        attempted=[],
        detailed_ids=set(),
        pages_checked=[],
        catalog_complete=False,
        scan_next_page=resume,
    )
    page = 1
    seen: set[str] = set()
    for _ in range(adapter.capabilities().max_pages):
        try:
            data = await adapter.search(UnifiedSearchFilters.model_validate(filters), page)
        except SourceFailure as exc:
            if progress["pages_checked"]:
                warnings.append("PARTIAL_" + exc.code)
                return result, warnings
            raise
        # An unchanged page under another page number must not loop or claim full coverage.
        ids = {str(raw["source_listing_id"]) for raw in data.items}
        if ids and ids <= seen:
            warnings.append("REPEATED_PAGE")
            return result, warnings
        warnings.extend(w for w in data.warnings if w != "BOUNDED_SEARCH")
        progress["pages_checked"].append(page)
        for raw in data.items:
            record = adapter.normalize(raw, t.now())
            if record.source_listing_id not in seen:
                result.append(record)
                seen.add(record.source_listing_id)
        if data.next_page is None:
            progress.update(scan_next_page=2, catalog_complete=resume == 2 or page == 1)
            if resume > 2 and page != 1:
                warnings.append("ROTATING_SEARCH")
            return result, warnings
        if data.next_page != page + 1:
            raise SourceFailure("PARSER_ERROR")
        page = max(data.next_page, resume) if page == 1 else data.next_page
        if page > settings().search_max_depth:
            progress["scan_next_page"] = 2
            warnings.append("DEPTH_LIMIT")
            return result, warnings
        progress["scan_next_page"] = page
        if len(result) >= adapter.capabilities().max_results:
            warnings.append("RESULT_LIMIT")
            return result, warnings
    warnings.append("PAGE_LIMIT")
    return result, warnings


async def collect(
    source: str, filters: dict[str, Any], progress: dict[str, Any] | None = None
) -> tuple[list[NormalizedListing], list[str], dict[str, Any]]:
    records, warnings = await fetch(source, filters, progress)
    prepared: dict[str, Any] = {}
    if progress is not None:
        progress.update(records=records, warnings=warnings, prepared=prepared)
    return records, warnings, prepared


async def prepare_photos(
    records: list[NormalizedListing], warnings: list[str], prepared: dict[str, Any]
) -> None:
    from app.matching.photos import PHashProvider
    from app.sources.images import thumbnail

    budget = 6
    for record in records:
        urls = list(
            dict.fromkeys(([record.main_image_url] if record.main_image_url else []) + record.images)
        )[: settings().matching.maximum_processed_photos]
        if record.source == "mock":
            if {"images", "main_image_url"} & record.field_presence:
                prepared[record.source_listing_id] = prepare_images(urls)
            continue
        # At most two cars x three photos per job. Incomplete evidence never enables auto merge.
        if len(urls) < 3 or budget < 3:
            continue
        fingerprints = []
        for url in urls[:3]:
            budget -= 1
            try:
                data = await asyncio.to_thread(thumbnail, record.source, url)
                fingerprints.append(PHashProvider().fingerprint(url, data))
            except SourceFailure:
                warnings.append("IMAGE_UNAVAILABLE")
        if fingerprints:
            prepared[record.source_listing_id] = fingerprints


async def collect_job(
    job: dict[str, Any], run: dict[str, Any], progress: dict[str, Any]
) -> tuple[list[NormalizedListing], list[str], dict[str, Any], list[uuid.UUID]]:
    from app.domain.filtering import evaluate

    # Position is scoped to the exact saved-filter version, never reused after an edit.
    with engine().connect() as conn:
        cursor = (
            conn.execute(
                sa.select(t.source_states).where(
                    t.source_states.c.search_id == job["search_id"], t.source_states.c.source == job["source"]
                )
            )
            .mappings()
            .first()
        )
    if cursor and cursor["scan_filters_version"] == run["filters_version"]:
        progress["scan_next_page"] = cursor["scan_next_page"]
    records, warnings, prepared = await collect(job["source"], run["filters"], progress)
    attempted: list[uuid.UUID] = []
    progress.update(
        records=records, warnings=warnings, prepared=prepared, attempted=attempted, detail_attempts={}
    )
    if any(
        w.startswith(("PARTIAL_", "DETAIL_RATE_LIMITED", "DETAIL_AUTH_REQUIRED", "DETAIL_SOURCE_UNAVAILABLE"))
        for w in warnings
    ):
        return records, warnings, prepared, attempted
    adapter = adapter_for(job["source"])
    if adapter.capabilities().detail:
        with engine().connect() as conn:
            # Include this user's favourites even when they disappeared from this search's catalog.
            link = t.search_listings.alias()
            known = (
                conn.execute(
                    sa.select(
                        t.listings.c.id,
                        t.listings.c.source_listing_id,
                        t.listings.c.source_url,
                        t.listings.c.detail_attempted_at,
                        t.listings.c.first_seen_at,
                        t.clusters.c.favourite,
                        link.c.match_state,
                    )
                    .join(
                        t.memberships,
                        sa.and_(
                            t.memberships.c.listing_id == t.listings.c.id,
                            t.memberships.c.user_id == run["user_id"],
                        ),
                    )
                    .join(t.clusters, t.clusters.c.id == t.memberships.c.cluster_id)
                    .outerjoin(
                        link,
                        sa.and_(
                            link.c.listing_id == t.listings.c.id,
                            link.c.search_id == job["search_id"],
                            link.c.filters_version == run["filters_version"],
                        ),
                    )
                    .where(
                        t.listings.c.source == job["source"],
                        t.clusters.c.archived_at.is_(None),
                        sa.or_(link.c.match_state.in_(["confirmed", "unverified"]), t.clusters.c.favourite),
                    )
                    .order_by(t.listings.c.detail_attempted_at.asc().nullsfirst(), t.listings.c.id)
                )
                .mappings()
                .all()
            )
            # Reuse public listing attempt times across searches and users without exposing their state.
            observed = {
                row["source_listing_id"]: dict(row)
                for row in conn.execute(
                    sa.select(
                        t.listings.c.id,
                        t.listings.c.source_listing_id,
                        t.listings.c.detail_attempted_at,
                        t.listings.c.first_seen_at,
                    ).where(
                        t.listings.c.source == job["source"],
                        t.listings.c.source_listing_id.in_([r.source_listing_id for r in records]),
                    )
                ).mappings()
            }
        by_source_id = {item["source_listing_id"]: dict(item) for item in known}
        filters = UnifiedSearchFilters.model_validate(run["filters"])
        # Newly discovered matching or unverified cars enter the same fair queue.
        for record in records:
            if (
                record.source_listing_id not in by_source_id
                and evaluate(filters, record)[0] != "not_matching"
            ):
                by_source_id[record.source_listing_id] = (
                    record.model_dump()
                    | {
                        "id": None,
                        "favourite": False,
                        "detail_attempted_at": None,
                        "first_seen_at": record.observed_at,
                        "match_state": evaluate(filters, record)[0],
                    }
                    | observed.get(record.source_listing_id, {})
                )
        candidates = []
        for item in by_source_id.values():
            last = item.get("detail_attempted_at")
            interval = 1200 if item["favourite"] else 7200
            if job["source"] != "mock" and last and last > t.now() - timedelta(seconds=interval):
                continue
            candidates.append(item)
        # Age dominates within each lane: repeatedly missing characteristics cannot starve other cars.
        queue_time = t.now()
        candidates.sort(
            key=lambda item: (
                item.get("detail_attempted_at") or item.get("first_seen_at") or queue_time,
                item.get("match_state") != "unverified",
                item["source_listing_id"],
            )
        )
        limit = settings().known_detail_limit
        favourites = [item for item in candidates if item["favourite"]]
        ordinary = [item for item in candidates if not item["favourite"]]
        quota = (limit + 1) // 2 if ordinary else limit
        selected = favourites[:quota] + ordinary[: limit - min(len(favourites), quota)]
        selected += favourites[quota : quota + limit - len(selected)]
        if len(candidates) > len(selected):
            warnings.append("KNOWN_DETAIL_LIMIT")
        for item in selected:
            if item["id"]:
                attempted.append(item["id"])
            progress["detail_attempts"][item["source_listing_id"]] = t.now()
            try:
                target = item["source_listing_id"] if job["source"] == "mock" else item["source_url"]
                raw = await adapter.get_listing(target)
                record = adapter.normalize(raw, t.now())
                if record.source_listing_id != item["source_listing_id"]:
                    raise SourceFailure("PARSER_ERROR")
                # Only a successfully parsed detail can establish this timestamp.
                if record.status != "UNKNOWN":
                    record = record.model_copy(
                        update={
                            "detail_checked_at": record.observed_at,
                            "field_presence": record.field_presence | {"detail_checked_at"},
                        }
                    )
                prior = next((r for r in records if r.source_listing_id == record.source_listing_id), None)
                if prior is not None:
                    merged = prior.model_dump() | {k: getattr(record, k) for k in record.field_presence}
                    merged.update(
                        observed_at=record.observed_at,
                        field_presence=prior.field_presence | record.field_presence,
                    )
                    records[records.index(prior)] = NormalizedListing.model_validate(merged)
                else:
                    records.append(record)
            except SourceFailure as exc:
                warnings.append("DETAIL_" + exc.code)
                if exc.code in {"RATE_LIMITED", "AUTH_REQUIRED", "SOURCE_UNAVAILABLE"}:
                    break
    await prepare_photos(records, warnings, prepared)
    return records, sorted(set(warnings)), prepared, attempted


async def run_once() -> bool:
    job = claim()
    if job is None:
        return False
    started = time.monotonic()
    with engine().connect() as conn:
        run = conn.execute(sa.select(t.runs).where(t.runs.c.id == job["run_id"])).mappings().first()
    if run is None:
        return True
    error = None
    records: list[NormalizedListing] = []
    warnings: list[str] = []
    attempted: list[uuid.UUID] = []
    progress: dict[str, Any] = {}
    context_token = request_source.set(job["source"])
    parser_version = "unconfigured"
    try:
        if job["attempt"] > settings().max_job_attempts:
            raise SourceFailure("RETRY_EXHAUSTED")
        parser_version = adapter_for(job["source"]).capabilities().parser_version
        records, warnings, prepared, attempted = await asyncio.wait_for(
            collect_job(job, dict(run), progress), timeout=settings().job_timeout_seconds
        )
    except SourceFailure as exc:
        error = exc.code
    except (TimeoutError, ConnectionError):
        if progress.get("records"):
            records, prepared, attempted = progress["records"], progress["prepared"], progress["attempted"]
            warnings = progress["warnings"] + ["PARTIAL_SOURCE_UNAVAILABLE"]
        else:
            error = "SOURCE_UNAVAILABLE"
    except Exception as exc:
        error = "PARSER_ERROR"
        log.error(
            json.dumps(
                {
                    "event": "source_error",
                    "job_id": str(job["id"]),
                    "source": job["source"],
                    "code": error,
                    **failure_context(exc),
                }
            )
        )
    finally:
        request_source.reset(context_token)
    if error:
        records = []
        prepared = {}
    with engine().begin() as conn:
        locked = (
            conn.execute(
                sa.select(t.jobs)
                .where(
                    t.jobs.c.id == job["id"],
                    t.jobs.c.lease_token == job["lease_token"],
                    t.jobs.c.state == "running",
                    t.jobs.c.lease_until > t.now(),
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if locked is None:
            return True
        search_row = (
            conn.execute(sa.select(t.searches).where(t.searches.c.id == job["search_id"]).with_for_update())
            .mappings()
            .first()
        )
        if search_row is None:
            return True
        search = dict(search_row) | {"filters": run["filters"], "filters_version": run["filters_version"]}
        conn.execute(
            t.search_listings.update()
            .where(
                t.search_listings.c.search_id == job["search_id"],
                t.search_listings.c.listing_id.in_(attempted),
            )
            .values(last_detail_attempt_at=t.now())
        )
        for listing in sorted(records, key=lambda row: row.source_listing_id):
            ingest(conn, search, listing, prepared.get(listing.source_listing_id))
        for source_id, attempted_at in progress.get("detail_attempts", {}).items():
            conn.execute(
                t.listings.update()
                .where(t.listings.c.source == job["source"], t.listings.c.source_listing_id == source_id)
                .values(detail_attempted_at=attempted_at)
            )
        for listing_id in conn.execute(
            sa.select(t.listings.c.id).where(
                t.listings.c.source == job["source"],
                t.listings.c.source_listing_id.in_([r.source_listing_id for r in records]),
            )
        ).scalars():
            match_listing(conn, search["user_id"], listing_id, settings().matching)
        partial_error = next(
            (
                w.split("_", 1)[1]
                for w in warnings
                if w.startswith("PARTIAL_")
                or w
                in {
                    "DETAIL_RATE_LIMITED",
                    "DETAIL_AUTH_REQUIRED",
                    "DETAIL_SOURCE_UNAVAILABLE",
                    "DETAIL_PARSER_ERROR",
                }
            ),
            None,
        )
        status = error or partial_error or "OK"
        state_values: dict[str, Any] = {
            "status": status,
            "last_attempt_at": t.now(),
            "last_error": error or partial_error,
        }
        if error is None:
            state_values.update(
                last_success_at=t.now(),
                last_result_count=len(records),
                scan_next_page=progress.get("scan_next_page", 2),
                scan_filters_version=run["filters_version"],
            )
        conn.execute(
            insert(t.source_states)
            .values(search_id=job["search_id"], source=job["source"], **state_values)
            .on_conflict_do_update(index_elements=["search_id", "source"], set_=state_values)
        )
        health_values = {
            k: v
            for k, v in state_values.items()
            if k not in {"last_result_count", "scan_next_page", "scan_filters_version"}
        }
        conn.execute(
            insert(t.health)
            .values(source=job["source"], **health_values)
            .on_conflict_do_update(index_elements=["source"], set_=health_values)
        )
        problem = error or partial_error
        retry = (
            problem in {"SOURCE_UNAVAILABLE", "RATE_LIMITED"} and job["attempt"] < settings().max_job_attempts
        )
        next_attempt = t.now() + timedelta(seconds=retry_delay(job["attempt"]))
        if problem in {"SOURCE_UNAVAILABLE", "RATE_LIMITED", "AUTH_REQUIRED", "PARSER_ERROR"}:
            limit = locked_limit(conn, job["source"])
            pause = (
                next_attempt
                if problem == "SOURCE_UNAVAILABLE"
                else t.now() + timedelta(seconds=600 if problem == "RATE_LIMITED" else 1800)
            )
            until = max(limit["cooldown_until"] or pause, pause)
            conn.execute(
                t.source_limits.update()
                .where(t.source_limits.c.source == job["source"])
                .values(cooldown_until=until)
            )
            next_attempt = max(next_attempt, until)
        conn.execute(
            t.jobs.update()
            .where(t.jobs.c.id == job["id"])
            .values(
                state="queued" if retry else "failed" if error and not locked["result_count"] else "complete",
                not_before=next_attempt,
                error_code=error or partial_error,
                result_count=max(len(records), locked["result_count"] or 0),
                warnings=warnings,
                pages_checked=progress.get("pages_checked", []),
                catalog_complete=progress.get("catalog_complete", False),
                completed_at=None if retry else t.now(),
                lease_until=None,
            )
        )
        # Serialize finalization across sources belonging to the same run.
        conn.execute(sa.select(t.runs.c.id).where(t.runs.c.id == job["run_id"]).with_for_update()).one()
        states = list(
            conn.execute(sa.select(t.jobs.c.state).where(t.jobs.c.run_id == job["run_id"])).scalars()
        )
        if not any(s in {"queued", "running"} for s in states):
            has_partial = (
                conn.execute(
                    sa.select(t.jobs.c.id).where(
                        t.jobs.c.run_id == job["run_id"], t.jobs.c.error_code.is_not(None)
                    )
                ).first()
                is not None
            )
            outcome = (
                "complete"
                if all(s == "complete" for s in states) and not has_partial
                else "failed"
                if all(s == "failed" for s in states)
                else "partial"
            )
            conn.execute(
                t.runs.update()
                .where(t.runs.c.id == job["run_id"])
                .values(outcome=outcome, completed_at=t.now())
            )
            conn.execute(
                t.searches.update()
                .where(t.searches.c.id == job["search_id"])
                .values(
                    last_checked_at=t.now(),
                    next_refresh_at=next_refresh(search_row["refresh_interval_seconds"])
                    if search_row["enabled"]
                    else None,
                )
            )
    log.info(
        json.dumps(
            {
                "source": job["source"],
                "job_id": str(job["id"]),
                "run_id": str(job["run_id"]),
                "attempt": job["attempt"],
                "trigger": run["trigger"],
                "parser_versions": sorted({r.parser_version for r in records}),
                "parser_version": parser_version,
                "warnings": warnings,
                "saved_search_id": str(job["search_id"]),
                "request_type": "search",
                "duration_ms": round((time.monotonic() - started) * 1000),
                "result_count": len(records),
                "status": error or partial_error or "OK",
            }
        )
    )
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    while True:
        try:
            schedule_due()
            await run_once()
        except Exception as exc:
            log.error(json.dumps({"event": "worker_iteration_failed", **failure_context(exc)}))
        await asyncio.sleep(settings().worker_poll_seconds)


if __name__ == "__main__":
    asyncio.run(main())
