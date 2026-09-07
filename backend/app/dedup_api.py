import uuid
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.security import require_user
from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.services.matching import merge, reject, split
from app.services.searches import owned

router = APIRouter(prefix="/api")
User = Annotated[dict[str, Any], Depends(require_user)]


class ClusterSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    versions: dict[uuid.UUID, Annotated[int, Field(ge=1)]] = Field(min_length=2, max_length=10)
    supersede_rejections: bool = False


class SplitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    groups: list[list[uuid.UUID]] = Field(min_length=2, max_length=100)


@router.get("/duplicates")
def duplicates(
    user: User, search_id: uuid.UUID | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 30
) -> dict[str, Any]:
    with engine().connect() as conn:
        search = owned(conn, search_id, user["id"]) if search_id else None
        lm, rm = t.memberships.alias("lm"), t.memberships.alias("rm")
        lc, rc = t.clusters.alias("lc"), t.clusters.alias("rc")
        ll, rl = t.listings.alias("ll"), t.listings.alias("rl")
        query = (
            sa.select(
                t.candidates,
                lm.c.cluster_id.label("left_cluster_id"),
                rm.c.cluster_id.label("right_cluster_id"),
                lc.c.version.label("left_version"),
                rc.c.version.label("right_version"),
                ll.c.title.label("left_title"),
                rl.c.title.label("right_title"),
                ll.c.source_listing_id.label("left_source_id"),
                rl.c.source_listing_id.label("right_source_id"),
                *[
                    table.c[field].label(side + "_" + field)
                    for table, side in ((ll, "left"), (rl, "right"))
                    for field in (
                        "year",
                        "mileage_km",
                        "price",
                        "currency",
                        "description",
                        "source",
                        "color",
                        "engine_volume",
                    )
                ],
            )
            .join(
                lm, sa.and_(lm.c.user_id == t.candidates.c.user_id, lm.c.listing_id == t.candidates.c.left_id)
            )
            .join(
                rm,
                sa.and_(rm.c.user_id == t.candidates.c.user_id, rm.c.listing_id == t.candidates.c.right_id),
            )
            .join(lc, lc.c.id == lm.c.cluster_id)
            .join(rc, rc.c.id == rm.c.cluster_id)
            .join(ll, ll.c.id == t.candidates.c.left_id)
            .join(rl, rl.c.id == t.candidates.c.right_id)
            .where(
                t.candidates.c.user_id == user["id"],
                t.candidates.c.decision == "possible_duplicate",
                lm.c.cluster_id != rm.c.cluster_id,
                sa.or_(
                    sa.literal(search_id is None),
                    sa.exists(
                        sa.select(1).where(
                            t.search_listings.c.search_id == search_id,
                            t.search_listings.c.filters_version
                            == (search["filters_version"] if search else 0),
                            t.search_listings.c.match_state.in_(["confirmed", "unverified"]),
                            t.search_listings.c.listing_id.in_(
                                [t.candidates.c.left_id, t.candidates.c.right_id]
                            ),
                        )
                    ),
                ),
                ~sa.exists(
                    sa.select(1).where(
                        t.decisions.c.user_id == user["id"],
                        t.decisions.c.left_id == t.candidates.c.left_id,
                        t.decisions.c.right_id == t.candidates.c.right_id,
                        t.decisions.c.superseded_at.is_(None),
                    )
                ),
            )
            .order_by(t.candidates.c.updated_at.desc(), t.candidates.c.id)
            .limit(limit + 1)
        )
        rows = [dict(r) for r in conn.execute(query).mappings()]
        return {"items": rows[:limit], "has_more": len(rows) > limit}


@router.post("/vehicles/merge")
def manual_merge(body: ClusterSelection, user: User) -> dict[str, Any]:
    with engine().begin() as conn:
        cluster_id = merge(
            conn,
            user["id"],
            body.versions,
            manual=True,
            config=settings().matching,
            supersede_rejections=body.supersede_rejections,
        )
        return {"cluster_id": cluster_id}


@router.post("/duplicates/reject")
def manual_reject(body: ClusterSelection, user: User) -> dict[str, bool]:
    if body.supersede_rejections:
        raise HTTPException(422, "Параметр применяется только к объединению")
    with engine().begin() as conn:
        reject(conn, user["id"], body.versions)
    return {"ok": True}


@router.post("/vehicles/{cluster_id}/split")
def manual_split(cluster_id: uuid.UUID, body: SplitInput, user: User) -> dict[str, Any]:
    with engine().begin() as conn:
        return {"cluster_ids": split(conn, user["id"], cluster_id, body.version, body.groups)}
