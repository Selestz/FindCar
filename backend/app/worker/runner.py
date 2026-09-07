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
    result: list[NormalizedListing] = []
    warnings: list[str] = []
    if progress is not None:
        progress.update(records=result, warnings=warnings, prepared={}, attempted=[], detailed_ids=set())
    page = 1
    for _ in range(adapter.capabilities().max_pages):
        try:
            data = await adapter.search(UnifiedSearchFilters.model_validate(filters), page)
        except SourceFailure as exc:
            if result:
                return result, warnings + ["PARTIAL_" + exc.code]
            raise
        warnings.extend(data.warnings)
        for raw in data.items:
            record = adapter.normalize(raw, t.now())
            result.append(record)
            if progress is not None and raw.get("detail"):
                progress["detailed_ids"].add(record.source_listing_id)
            if len(result) >= adapter.capabilities().max_results:
                return result, warnings + ["RESULT_LIMIT"]
        if data.next_page is None:
            return result, warnings
        if data.next_page <= page:
            raise SourceFailure("PARSER_ERROR")
        page = data.next_page
    return result, warnings + ["PAGE_LIMIT"]


async def collect(
    source: str, filters: dict[str, Any], progress: dict[str, Any] | None = None
) -> tuple[list[NormalizedListing], list[str], dict[str, Any]]:
    from app.matching.photos import PHashProvider
    from app.sources.images import thumbnail

    records, warnings = await fetch(source, filters, progress)
    prepared: dict[str, Any] = {}
    if progress is not None:
        progress.update(records=records, warnings=warnings, prepared=prepared, attempted=[])
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
    return records, sorted(set(warnings)), prepared


async def collect_job(
    job: dict[str, Any], run: dict[str, Any], progress: dict[str, Any]
) -> tuple[list[NormalizedListing], list[str], dict[str, Any], list[uuid.UUID]]:
    records, warnings, prepared = await collect(job["source"], run["filters"], progress)
    # Rotate known cards not already checked in detail, even if they remain in the catalog.
    with engine().connect() as conn:
        known = (
            conn.execute(
                sa.select(t.listings.c.id, t.listings.c.source_listing_id, t.listings.c.source_url)
                .join(t.search_listings, t.search_listings.c.listing_id == t.listings.c.id)
                .where(
                    t.search_listings.c.search_id == job["search_id"],
                    t.search_listings.c.filters_version == run["filters_version"],
                    t.listings.c.source == job["source"],
                    t.listings.c.source_listing_id.not_in(progress.get("detailed_ids", set())),
                )
                .order_by(t.search_listings.c.last_detail_attempt_at.asc().nullsfirst(), t.listings.c.id)
                .limit(settings().known_detail_limit + 1)
            )
            .mappings()
            .all()
        )
    attempted: list[uuid.UUID] = []
    progress.update(records=records, warnings=warnings, prepared=prepared, attempted=attempted)
    if any(
        w.startswith(("PARTIAL_", "DETAIL_RATE_LIMITED", "DETAIL_AUTH_REQUIRED", "DETAIL_SOURCE_UNAVAILABLE"))
        for w in warnings
    ):
        return records, warnings, prepared, attempted
    adapter = adapter_for(job["source"])
    if not adapter.capabilities().detail:
        return records, warnings, prepared, attempted
    if len(known) > settings().known_detail_limit:
        warnings.append("KNOWN_DETAIL_LIMIT")
    for item in known[: settings().known_detail_limit]:
        attempted.append(item["id"])
        try:
            target = item["source_listing_id"] if job["source"] == "mock" else item["source_url"]
            raw = await adapter.get_listing(target)
            record = adapter.normalize(raw, t.now())
            if record.source_listing_id != item["source_listing_id"]:
                raise SourceFailure("PARSER_ERROR")
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
            state_values.update(last_success_at=t.now(), last_result_count=len(records))
        conn.execute(
            insert(t.source_states)
            .values(search_id=job["search_id"], source=job["source"], **state_values)
            .on_conflict_do_update(index_elements=["search_id", "source"], set_=state_values)
        )
        health_values = {k: v for k, v in state_values.items() if k != "last_result_count"}
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
