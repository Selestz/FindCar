import uuid
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.security import require_user
from app.db import schema as t
from app.db.connection import engine
from app.services.matching import audit, owned_clusters
from app.services.visibility import visible_source

router = APIRouter(prefix="/api")
User = Annotated[dict[str, Any], Depends(require_user)]


@router.get("/listings/{listing_id}/image/{index}")
def listing_image(listing_id: uuid.UUID, index: int, user: User) -> Any:
    from fastapi.responses import Response

    from app.sources.base import SourceFailure
    from app.sources.images import thumbnail

    with engine().connect() as conn:
        row = (
            conn.execute(
                sa.select(t.listings)
                .join(t.memberships)
                .where(
                    t.memberships.c.user_id == user["id"],
                    t.listings.c.id == listing_id,
                    visible_source(t.listings.c.source),
                )
            )
            .mappings()
            .first()
        )
    if row is None or not 0 <= index < min(6, len(row["images"])):
        raise HTTPException(404, "Фотография не найдена")
    try:
        data = thumbnail(row["source"], row["images"][index])
    except SourceFailure as exc:
        raise HTTPException(404, "Фотография недоступна") from exc
    return Response(
        data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


class VehicleState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int = Field(ge=1)
    favourite: bool | None = None
    hidden: bool | None = None
    note: str | None = Field(default=None, max_length=5000)


@router.patch("/vehicles/{cluster_id}/state")
def change_state(cluster_id: uuid.UUID, body: VehicleState, user: User) -> dict[str, Any]:
    with engine().begin() as conn:
        cluster = owned_clusters(conn, user["id"], {cluster_id: body.version})[0]
        values = {
            k: v
            for k, v in body.model_dump(exclude_unset=True).items()
            if k in {"favourite", "hidden"} and v is not None
        }
        if body.note is not None:
            # Append-only notes preserve provenance across merge/split; an empty note never erases existing ones.
            if not body.note:
                raise HTTPException(422, "Введите текст заметки")
            if len(cluster["notes"]) >= 200:
                raise HTTPException(409, "Достигнут лимит заметок для автомобиля")
            values["notes"] = cluster["notes"] + [
                {
                    "id": str(uuid.uuid4()),
                    "text": body.note,
                    "created_at": t.now().isoformat(),
                    "origin": str(cluster_id),
                }
            ]
        if not values:
            raise HTTPException(422, "Не указано изменение")
        conn.execute(
            t.clusters.update()
            .where(t.clusters.c.id == cluster_id)
            .values(**values, version=cluster["version"] + 1)
        )
        audit(conn, user["id"], "state", {"cluster_id": str(cluster_id), "fields": list(values)})
        return {
            "version": cluster["version"] + 1,
            "favourite": values.get("favourite", cluster["favourite"]),
            "hidden": values.get("hidden", cluster["hidden"]),
            "notes": values.get("notes", cluster["notes"]),
        }


class ReadEvents(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class EditNote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int = Field(ge=1)
    text: str | None = Field(max_length=5000)


@router.patch("/vehicles/{cluster_id}/notes/{note_id}")
def edit_note(cluster_id: uuid.UUID, note_id: uuid.UUID, body: EditNote, user: User) -> dict[str, int]:
    if body.text == "":
        raise HTTPException(422, "Введите текст заметки")
    with engine().begin() as conn:
        cluster = owned_clusters(conn, user["id"], {cluster_id: body.version})[0]
        found = False

        def replace(note: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal found
            if note.get("id") == str(note_id):
                found = True
                return (
                    None
                    if body.text is None
                    else note | {"text": body.text, "edited_at": t.now().isoformat()}
                )
            if isinstance(note.get("note"), dict):
                child = replace(note["note"])
                return note | {"note": child} if child else None
            return note

        notes = [updated for note in cluster["notes"] if (updated := replace(note)) is not None]
        if not found:
            raise HTTPException(404, "Заметка не найдена")
        conn.execute(
            t.clusters.update()
            .where(t.clusters.c.id == cluster_id)
            .values(notes=notes, version=cluster["version"] + 1)
        )
        audit(
            conn,
            user["id"],
            "note_edit",
            {
                "cluster_id": str(cluster_id),
                "note_id": str(note_id),
                "before": cluster["notes"],
                "after": notes,
            },
        )
        return {"version": cluster["version"] + 1}


@router.post("/events/read")
def read_events(body: ReadEvents, user: User) -> dict[str, bool]:
    with engine().begin() as conn:
        found = set(
            conn.execute(
                sa.select(t.events.c.id).where(t.events.c.user_id == user["id"], t.events.c.id.in_(body.ids))
            ).scalars()
        )
        if found != set(body.ids):
            raise HTTPException(404, "Событие не найдено")
        conn.execute(
            t.events.update()
            .where(t.events.c.id.in_(body.ids), t.events.c.read_at.is_(None))
            .values(read_at=t.now())
        )
    return {"ok": True}


@router.get("/events")
def list_events(
    user: User,
    unread: bool = False,
    after: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    with engine().connect() as conn:
        conditions = [
            t.events.c.user_id == user["id"],
            sa.or_(
                t.events.c.listing_id.is_(None),
                t.events.c.listing_id.in_(
                    sa.select(t.listings.c.id).where(visible_source(t.listings.c.source))
                ),
            ),
        ]
        count = conn.execute(
            sa.select(sa.func.count()).select_from(t.events).where(*conditions, t.events.c.read_at.is_(None))
        ).scalar_one()
        if unread:
            conditions.append(t.events.c.read_at.is_(None))
        if after:
            previous = (
                conn.execute(
                    sa.select(t.events).where(t.events.c.id == after, t.events.c.user_id == user["id"])
                )
                .mappings()
                .first()
            )
            if not previous:
                raise HTTPException(404, "Событие не найдено")
            conditions.append(
                sa.or_(
                    t.events.c.occurred_at < previous["occurred_at"],
                    sa.and_(t.events.c.occurred_at == previous["occurred_at"], t.events.c.id > after),
                )
            )
        rows = [
            dict(r)
            for r in conn.execute(
                sa.select(t.events, t.memberships.c.cluster_id, t.listings.c.title, t.listings.c.source)
                .outerjoin(
                    t.memberships,
                    sa.and_(
                        t.events.c.listing_id == t.memberships.c.listing_id,
                        t.events.c.user_id == t.memberships.c.user_id,
                    ),
                )
                .outerjoin(t.listings, t.events.c.listing_id == t.listings.c.id)
                .where(*conditions)
                .order_by(t.events.c.occurred_at.desc(), t.events.c.id)
                .limit(limit + 1)
            ).mappings()
        ]
        return {
            "items": rows[:limit],
            "unread_count": count,
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }
