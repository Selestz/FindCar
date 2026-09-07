import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

from app.db import schema as t
from app.services.searches import owned
from app.services.visibility import visible_source

CHANGES = ["PRICE_DROP", "PRICE_INCREASE", "LISTING_REMOVED", "LISTING_RETURNED", "PROBABLE_RELIST"]


def vehicle_page(
    conn: Connection,
    user_id: uuid.UUID,
    search_id: uuid.UUID | None,
    include_unverified: bool,
    after: uuid.UUID | None,
    limit: int,
    view: str,
    sort: str,
) -> dict[str, Any]:
    joined = t.listings.join(t.memberships).join(t.clusters, t.memberships.c.cluster_id == t.clusters.c.id)
    conditions = [
        t.memberships.c.user_id == user_id,
        t.clusters.c.archived_at.is_(None),
        t.listings.c.status == "ACTIVE",
        visible_source(t.listings.c.source),
    ]
    state: Any = sa.literal("confirmed")
    unknown: Any = sa.literal([], type_=JSONB)
    if search_id:
        search = owned(conn, search_id, user_id)
        joined = joined.join(t.search_listings, t.search_listings.c.listing_id == t.listings.c.id)
        conditions += [
            t.search_listings.c.search_id == search_id,
            t.search_listings.c.filters_version == search["filters_version"],
        ]
        state, unknown = t.search_listings.c.match_state, t.search_listings.c.unknown_filters
    unverified = conn.execute(
        sa.select(sa.func.count(sa.distinct(t.memberships.c.cluster_id)))
        .select_from(joined)
        .where(*conditions, state == "unverified", t.clusters.c.hidden.is_(False))
    ).scalar_one()
    conditions.append(state.in_(["confirmed", "unverified"] if include_unverified else ["confirmed"]))
    conditions.append(t.clusters.c.hidden.is_(view == "hidden"))
    if view == "favourites":
        conditions.append(t.clusters.c.favourite.is_(True))

    def event_value(types: list[str], unread: bool = False, amount: bool = False) -> Any:
        membership = t.memberships.alias()
        event_listing = t.listings.alias()
        value = (
            (
                sa.cast(t.events.c.payload["old_price"].astext, sa.Numeric)
                - sa.cast(t.events.c.payload["new_price"].astext, sa.Numeric)
            )
            if amount
            else t.events.c.occurred_at
        )
        query = (
            sa.select(value)
            .select_from(
                t.events.join(
                    membership,
                    sa.and_(
                        membership.c.listing_id == t.events.c.listing_id,
                        membership.c.user_id == t.events.c.user_id,
                    ),
                ).join(event_listing, event_listing.c.id == t.events.c.listing_id)
            )
            .where(
                t.events.c.user_id == user_id,
                membership.c.cluster_id == t.clusters.c.id,
                t.events.c.type.in_(types),
                visible_source(event_listing.c.source),
            )
        )
        if unread:
            query = query.where(t.events.c.read_at.is_(None))
        if types == ["PRICE_DROP"]:
            query = query.where(event_listing.c.status == "ACTIVE")
        return (
            query.order_by(t.events.c.occurred_at.desc(), t.events.c.id)
            .limit(1)
            .correlate(t.clusters)
            .scalar_subquery()
        )

    new_at, changed_at, drop_at = (
        event_value(["NEW_LISTING"], True),
        event_value(CHANGES, True),
        event_value(["PRICE_DROP"]),
    )
    if view == "new":
        conditions.append(new_at.is_not(None))
    elif view == "changed":
        conditions.append(changed_at.is_not(None))
    group = t.memberships.c.cluster_id
    price = sa.case((t.listings.c.currency == "RUB", t.listings.c.price), else_=None)
    active = t.listings.c.status == "ACTIVE"
    active_count = sa.func.count().filter(active).over(partition_by=group)

    def current_value(field: Any, aggregate: Any) -> Any:
        return sa.case(
            (active_count > 0, aggregate(field).filter(active).over(partition_by=group)),
            else_=aggregate(field).over(partition_by=group),
        )

    base = (
        sa.select(
            t.listings,
            group,
            t.clusters.c.version.label("cluster_version"),
            t.clusters.c.favourite,
            t.clusters.c.hidden,
            t.clusters.c.first_seen_at.label("cluster_first_seen_at"),
            sa.func.count().over(partition_by=group).label("listing_count"),
            current_value(price, sa.func.min).label("price_min"),
            current_value(price, sa.func.max).label("price_max"),
            current_value(t.listings.c.mileage_km, sa.func.min).label("minimum_mileage"),
            sa.func.max(t.listings.c.published_at).over(partition_by=group).label("latest_published_at"),
            sa.func.array_agg(t.listings.c.source).over(partition_by=group).label("sources"),
            new_at.label("new_at"),
            changed_at.label("changed_at"),
            drop_at.label("price_drop_at"),
            event_value(["PRICE_DROP"], amount=True).label("price_drop_amount"),
            state.label("match_state"),
            unknown.label("unknown_filters"),
        )
        .select_from(joined)
        .where(*conditions)
        .distinct(group)
        .order_by(
            group,
            (state == "confirmed").desc(),
            (t.listings.c.status == "ACTIVE").desc(),
            t.listings.c.last_seen_at.desc(),
            t.listings.c.id,
        )
        .cte("vehicles")
    )
    total = conn.execute(sa.select(sa.func.count()).select_from(base)).scalar_one()
    orders = {
        "found": base.c.cluster_first_seen_at.desc(),
        "newest": base.c.latest_published_at.desc().nulls_last(),
        "price_asc": base.c.price_min.asc().nulls_last(),
        "price_desc": base.c.price_max.desc().nulls_last(),
        "mileage": base.c.minimum_mileage.asc().nulls_last(),
        "price_drop": base.c.price_drop_at.desc().nulls_last(),
    }
    ranked = sa.select(
        base, sa.func.row_number().over(order_by=[orders[sort], base.c.cluster_id]).label("position")
    ).cte("ranked")
    query = sa.select(ranked)
    if after:
        position = conn.execute(
            sa.select(ranked.c.position).where(ranked.c.cluster_id == after)
        ).scalar_one_or_none()
        if position is None:
            raise HTTPException(409, "Результаты изменились. Обновите список.")
        query = query.where(ranked.c.position > position)
    rows = [dict(r) for r in conn.execute(query.order_by(ranked.c.position).limit(limit + 1)).mappings()]
    for row in rows:
        for name in ("position", "source_metadata", "field_observed_at"):
            row.pop(name, None)
        for name in ("price", "price_min", "price_max", "price_drop_amount", "engine_volume"):
            if row[name] is not None:
                row[name] = str(row[name])
        row["sources"] = sorted(set(row["sources"]))
    return {
        "items": rows[:limit],
        "total": total,
        "unverified_count": unverified,
        "next_cursor": rows[limit - 1]["cluster_id"] if len(rows) > limit else None,
    }
