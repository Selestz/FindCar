import math
import random
import uuid
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.engine import Connection

from app.config import settings
from app.db import schema as t


def owned(conn: Connection, search_id: uuid.UUID, user_id: uuid.UUID, lock: bool = False) -> dict[str, Any]:
    query = sa.select(t.searches).where(t.searches.c.id == search_id, t.searches.c.user_id == user_id)
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).mappings().first()
    if row is None:
        raise HTTPException(404, "Поиск не найден")
    return dict(row)


def next_refresh(interval: int) -> Any:
    return t.now() + timedelta(seconds=interval + random.randint(0, settings().scheduler_jitter_seconds))


def enqueue(conn: Connection, search: dict[str, Any], *, scheduled: bool = False) -> uuid.UUID:
    existing = conn.execute(
        sa.select(t.jobs.c.run_id).where(
            t.jobs.c.search_id == search["id"], t.jobs.c.state.in_(["queued", "running"])
        )
    ).scalar()
    if existing:
        return existing
    if not scheduled and search["last_manual_refresh_at"]:
        wait = (
            settings().manual_refresh_cooldown - (t.now() - search["last_manual_refresh_at"]).total_seconds()
        )
        if wait > 0:
            raise HTTPException(
                429,
                f"Повторите обновление через {math.ceil(wait)} с",
                headers={"Retry-After": str(math.ceil(wait))},
            )
    run_id = conn.execute(
        t.runs.insert()
        .values(
            search_id=search["id"],
            user_id=search["user_id"],
            filters=search["filters"],
            filters_version=search["filters_version"],
            trigger="scheduled" if scheduled else "manual",
        )
        .returning(t.runs.c.id)
    ).scalar_one()
    for source in search["enabled_sources"]:
        conn.execute(t.jobs.insert().values(run_id=run_id, search_id=search["id"], source=source))
    updates = {
        "next_refresh_at": next_refresh(search["refresh_interval_seconds"]) if search["enabled"] else None
    }
    if not scheduled:
        updates["last_manual_refresh_at"] = t.now()
    conn.execute(t.searches.update().where(t.searches.c.id == search["id"]).values(**updates))
    return run_id
