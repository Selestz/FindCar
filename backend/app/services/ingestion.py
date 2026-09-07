import hashlib
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from app.db import schema as t
from app.domain.filtering import evaluate
from app.domain.models import NormalizedListing, UnifiedSearchFilters
from app.matching.photos import Fingerprint

SIGNIFICANT = {"price", "currency", "mileage_km", "status", "description"}


def event(
    conn: Connection,
    user_id: uuid.UUID,
    listing_id: uuid.UUID,
    kind: str,
    key: str,
    payload: dict[str, Any],
    search_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
) -> None:
    conn.execute(
        insert(t.events)
        .values(
            user_id=user_id,
            listing_id=listing_id,
            type=kind,
            event_key=key,
            payload=payload,
            search_id=search_id,
            snapshot_id=snapshot_id,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "event_key"])
    )


def ingest(
    conn: Connection,
    search: dict[str, Any],
    listing: NormalizedListing,
    photos: list[Fingerprint] | None = None,
) -> None:
    conn.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": str(search["user_id"])}
    )
    observed = listing.observed_at
    raw = listing.model_dump()
    values = {
        k: v
        for k, v in raw.items()
        if k in t.listings.c and k not in {"first_seen_at", "last_seen_at", "version"}
    }
    # Serialize concurrent first observations, then lock the persisted row.
    inserted = conn.execute(
        insert(t.listings)
        .values(**values, first_seen_at=observed, last_seen_at=observed)
        .on_conflict_do_nothing(index_elements=["source", "source_listing_id"])
        .returning(t.listings.c.id)
    ).scalar_one_or_none()
    previous = dict(
        conn.execute(
            sa.select(t.listings)
            .where(
                t.listings.c.source == listing.source,
                t.listings.c.source_listing_id == listing.source_listing_id,
            )
            .with_for_update()
        )
        .mappings()
        .one()
    )
    listing_id = previous["id"]
    if photos is not None:
        conn.execute(
            insert(t.fingerprints)
            .values(listing_id=listing_id, photos=[p.json() for p in photos], observed_at=observed)
            .on_conflict_do_update(
                index_elements=["listing_id"],
                set_={"photos": [p.json() for p in photos], "observed_at": observed},
                where=t.fingerprints.c.observed_at <= observed,
            )
        )
    updates: dict[str, Any] = {}
    field_times = dict(previous["field_observed_at"])
    for name in listing.field_presence:
        if name not in values or name in {"source", "source_listing_id"}:
            continue
        last = field_times.get(name)
        if last is None or observed >= datetime.fromisoformat(last):
            if values[name] != previous[name]:
                updates[name] = values[name]
            field_times[name] = observed.isoformat()
    changed = bool(SIGNIFICANT & updates.keys())
    version = previous["version"] + int(bool(updates) and inserted is None)
    conn.execute(
        t.listings.update()
        .where(t.listings.c.id == listing_id)
        .values(
            **updates,
            field_observed_at=field_times,
            last_seen_at=max(previous["last_seen_at"], observed),
            version=version,
        )
    )
    current = previous | updates
    if inserted is not None or changed:
        description = current["description"]
        snapshot_id = conn.execute(
            t.snapshots.insert()
            .values(
                listing_id=listing_id,
                listing_version=version,
                observed_at=observed,
                price=current["price"],
                currency=current["currency"],
                mileage_km=current["mileage_km"],
                status=current["status"],
                description_hash=hashlib.sha256((description or "").encode()).hexdigest(),
                description_text_on_change=description
                if inserted is not None or "description" in updates
                else None,
            )
            .returning(t.snapshots.c.id)
        ).scalar_one()
        if inserted is None:
            kinds = []
            if (
                "price" in updates
                and previous["price"] is not None
                and current["price"] is not None
                and previous["currency"] == current["currency"]
            ):
                kinds.append("PRICE_DROP" if current["price"] < previous["price"] else "PRICE_INCREASE")
            if "status" in updates:
                if current["status"] == "REMOVED":
                    kinds.append("LISTING_REMOVED")
                elif previous["status"] == "REMOVED" and current["status"] == "ACTIVE":
                    kinds.append("LISTING_RETURNED")
            observers = conn.execute(
                sa.select(t.memberships.c.user_id).where(t.memberships.c.listing_id == listing_id)
            ).scalars()
            for user_id in observers:
                for kind in kinds:
                    event(
                        conn,
                        user_id,
                        listing_id,
                        kind,
                        f"{kind}:{snapshot_id}",
                        {
                            "old_price": str(previous["price"]) if previous["price"] is not None else None,
                            "new_price": str(current["price"]) if current["price"] is not None else None,
                            "status": current["status"],
                        },
                        snapshot_id=snapshot_id,
                    )
    # Detail pages can omit region or other fields observed in the search page.
    # Evaluate the merged, timestamp-ordered observation rather than losing that evidence.
    merged = NormalizedListing.model_validate(
        raw | {k: v for k, v in current.items() if k in NormalizedListing.model_fields}
    )
    state, unknown = evaluate(UnifiedSearchFilters.model_validate(search["filters"]), merged)
    existing_link = (
        conn.execute(
            sa.select(t.search_listings).where(
                t.search_listings.c.search_id == search["id"], t.search_listings.c.listing_id == listing_id
            )
        )
        .mappings()
        .first()
    )
    if state == "not_matching" and existing_link is None:
        return
    if existing_link and observed < existing_link["last_seen_at"]:
        return
    conn.execute(
        insert(t.search_listings)
        .values(
            search_id=search["id"],
            listing_id=listing_id,
            user_id=search["user_id"],
            first_seen_at=observed,
            last_seen_at=observed,
            match_state=state,
            unknown_filters=unknown,
            filters_version=search["filters_version"],
        )
        .on_conflict_do_update(
            index_elements=["search_id", "listing_id"],
            set_={
                "last_seen_at": observed,
                "match_state": state,
                "unknown_filters": unknown,
                "filters_version": search["filters_version"],
            },
        )
    )
    # One short per-user lock prevents duplicate clusters across concurrent searches.
    conn.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": str(search["user_id"])}
    )
    membership = (
        conn.execute(
            sa.select(t.memberships).where(
                t.memberships.c.user_id == search["user_id"], t.memberships.c.listing_id == listing_id
            )
        )
        .mappings()
        .first()
    )
    if membership is None:
        cluster_id = conn.execute(
            t.clusters.insert()
            .values(
                user_id=search["user_id"],
                representative_listing_id=listing_id,
                first_seen_at=observed,
                last_seen_at=observed,
            )
            .returning(t.clusters.c.id)
        ).scalar_one()
        conn.execute(
            t.memberships.insert().values(
                user_id=search["user_id"], listing_id=listing_id, cluster_id=cluster_id, attached_at=observed
            )
        )
    else:
        conn.execute(
            t.clusters.update()
            .where(t.clusters.c.id == membership["cluster_id"])
            .values(last_seen_at=sa.func.greatest(t.clusters.c.last_seen_at, observed))
        )
    if existing_link is None:
        event(
            conn,
            search["user_id"],
            listing_id,
            "NEW_LISTING",
            f"new:{search['id']}:{listing_id}",
            {"title": listing.title, "match_state": state},
            search_id=search["id"],
        )
