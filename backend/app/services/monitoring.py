"""Durable scheduling and shared source limits; no network calls inside transactions."""

import random
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.services.searches import enqueue, next_refresh
from app.sources.base import SourceFailure
from app.sources.registry import source_enabled

request_source: ContextVar[str | None] = ContextVar("request_source", default=None)


def schedule_due() -> int:
    if not settings().scheduler_enabled:
        return 0
    with engine().begin() as conn:
        searches = (
            conn.execute(
                sa.select(t.searches)
                .join(t.users, t.users.c.id == t.searches.c.user_id)
                .where(
                    t.searches.c.enabled,
                    t.users.c.enabled,
                    ~sa.exists(
                        sa.select(t.jobs.c.id).where(
                            t.jobs.c.search_id == t.searches.c.id, t.jobs.c.state.in_(["queued", "running"])
                        )
                    ),
                    sa.or_(t.searches.c.next_refresh_at.is_(None), t.searches.c.next_refresh_at <= t.now()),
                )
                .order_by(t.searches.c.next_refresh_at.asc().nullsfirst(), t.searches.c.id)
                .with_for_update(of=t.searches, skip_locked=True)
                .limit(20)
            )
            .mappings()
            .all()
        )
        count = 0
        for row in searches:
            active = conn.execute(
                sa.select(t.jobs.c.id).where(
                    t.jobs.c.search_id == row["id"], t.jobs.c.state.in_(["queued", "running"])
                )
            ).first()
            if active:
                continue
            sources = [s for s in row["enabled_sources"] if source_enabled(s)]
            if sources:
                enqueue(conn, dict(row) | {"enabled_sources": sources}, scheduled=True)
                count += 1
            else:
                conn.execute(
                    t.searches.update()
                    .where(t.searches.c.id == row["id"])
                    .values(next_refresh_at=next_refresh(row["refresh_interval_seconds"]))
                )
        return count


def locked_limit(conn: Connection, source: str) -> dict[str, Any]:
    conn.execute(
        insert(t.source_limits)
        .values(source=source, window_started_at=t.now())
        .on_conflict_do_nothing(index_elements=["source"])
    )
    return dict(
        conn.execute(sa.select(t.source_limits).where(t.source_limits.c.source == source).with_for_update())
        .mappings()
        .one()
    )


def cooldown(source: str, seconds: float) -> None:
    with engine().begin() as conn:
        row = locked_limit(conn, source)
        until = t.now() + timedelta(seconds=seconds)
        conn.execute(
            t.source_limits.update()
            .where(t.source_limits.c.source == source)
            .values(cooldown_until=max(row["cooldown_until"] or until, until))
        )


def reserve_request(source: str) -> float:
    """Count HTML and cold image attempts across all processes."""
    blocked_until = None
    delay = 0.0
    with engine().begin() as conn:
        row = locked_limit(conn, source)
        now = t.now()
        if row["cooldown_until"] and row["cooldown_until"] > now:
            blocked_until = row["cooldown_until"]
        else:
            start, count = row["window_started_at"], row["request_count"]
            if now >= start + timedelta(hours=1):
                start, count = now, 0
            if count >= settings().source_requests_per_hour:
                blocked_until = start + timedelta(hours=1)
                conn.execute(
                    t.source_limits.update()
                    .where(t.source_limits.c.source == source)
                    .values(cooldown_until=blocked_until)
                )
            else:
                at = max(now, row["next_request_at"] or now)
                delay = (at - now).total_seconds()
                conn.execute(
                    t.source_limits.update()
                    .where(t.source_limits.c.source == source)
                    .values(
                        window_started_at=start,
                        request_count=count + 1,
                        next_request_at=at + timedelta(seconds=settings().source_request_interval_seconds),
                    )
                )
    if blocked_until:
        raise SourceFailure("RATE_LIMITED")
    return delay


def retry_delay(attempt: int) -> float:
    base = settings().retry_base_seconds * 2 ** max(0, attempt - 1)
    return base + random.uniform(0, base * 0.2)
