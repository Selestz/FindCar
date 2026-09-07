import json
import logging
import uuid
from datetime import timedelta
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.auth.security import COOKIE, authenticate, digest, hasher, issue_session, require_user
from app.catalog import catalogue, makes, models, search_name, source_scope
from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.dedup_api import router as dedup_router
from app.domain.models import SearchInput, Source
from app.http_security import RequestBodyLimit
from app.services.searches import enqueue, next_refresh, owned
from app.services.vehicles import vehicle_page
from app.services.visibility import visible_source
from app.workspace_api import router as workspace_router

app = FastAPI(title="FindCar", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(RequestBodyLimit)
app.include_router(dedup_router)
app.include_router(workspace_router)
User = Annotated[dict[str, Any], Depends(require_user)]


@app.middleware("http")
async def origin_guard(request: Request, call_next: Any) -> Response:
    request.state.request_id = uuid.uuid4().hex
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.headers.get("origin") != settings().app_origin
    ):
        response: Response = JSONResponse({"detail": "Недопустимый Origin"}, status_code=403)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(SQLAlchemyError)
async def db_failure(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logging.getLogger("findcar.api").error(
        json.dumps(
            {
                "event": "database_error",
                "request_id": getattr(request.state, "request_id", None),
                "exception_type": type(exc).__name__,
            }
        )
    )
    return JSONResponse({"detail": "База данных временно недоступна"}, status_code=503)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


@app.post("/api/auth/login")
def login(body: Login, request: Request, response: Response) -> dict[str, Any]:
    user = authenticate(
        body.username.strip(), body.password, request.client.host if request.client else "unknown"
    )
    token, csrf = issue_session(user["id"], request.cookies.get(COOKIE))
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings().cookie_secure,
        samesite="lax",
        max_age=settings().session_hours * 3600,
        path="/",
    )
    return {"id": user["id"], "username": user["username"], "role": user["role"], "csrf_token": csrf}


@app.get("/api/auth/me")
def me(user: User) -> dict[str, Any]:
    return user


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response, user: User) -> None:
    with engine().begin() as conn:
        conn.execute(t.sessions.delete().where(t.sessions.c.token_digest == digest(request.cookies[COOKIE])))
    response.delete_cookie(COOKIE, path="/")


class NewUser(Login):
    password: str = Field(min_length=12, max_length=128)


@app.post("/api/admin/users", status_code=201)
def create_user(body: NewUser, user: User) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(403, "Только для администратора")
    if not body.username.strip():
        raise HTTPException(422, "Имя пользователя не может быть пустым")
    try:
        with engine().begin() as conn:
            conn.execute(sa.text("SELECT pg_advisory_xact_lock(7140081)"))
            if conn.execute(sa.select(sa.func.count()).select_from(t.users)).scalar_one() >= 10:
                raise HTTPException(409, "Лимит: 10 пользователей")
            user_id = conn.execute(
                t.users.insert()
                .values(username=body.username.strip(), password_hash=hasher.hash(body.password))
                .returning(t.users.c.id)
            ).scalar_one()
    except IntegrityError:
        raise HTTPException(409, "Имя пользователя занято") from None
    return {"id": user_id, "username": body.username.strip()}


@app.get("/api/health")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/ready")
def ready() -> dict[str, str]:
    with engine().connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        if version != "0005_query_indexes":
            raise HTTPException(503, "Необходима миграция")
    return {"status": "ready"}


@app.get("/api/sources/health")
def sources_health(user: User) -> dict[str, Any]:
    from app.sources.registry import adapter_for, source_enabled

    with engine().connect() as conn:
        rows = {r["source"]: dict(r) for r in conn.execute(sa.select(t.health)).mappings()}
        heartbeat = conn.execute(sa.select(sa.func.max(t.heartbeats.c.last_seen_at))).scalar()
        limits = {r["source"]: r for r in conn.execute(sa.select(t.source_limits)).mappings()}
    result = []
    for source in Source:
        if source in {Source.MOCK, Source.AVITO} and not settings().test_fixtures_enabled:
            continue
        entry: dict[str, Any] = {
            "source": source.value,
            "enabled": source_enabled(source),
            "status": "NOT_CHECKED" if source_enabled(source) else "ACCESS_NOT_CONFIGURED",
        }
        if source_enabled(source) and source in rows:
            entry.update(rows[source])
        if source_enabled(source):
            entry["capabilities"] = adapter_for(source).capabilities().model_dump()
        if source in limits:
            entry["cooldown_until"] = limits[source]["cooldown_until"]
        result.append(entry)
    return {
        "items": result,
        "worker_alive": heartbeat is not None
        and heartbeat > t.now() - timedelta(seconds=settings().job_timeout_seconds + 60),
        "scheduler_enabled": settings().scheduler_enabled,
    }


@app.get("/api/catalog")
def make_catalog(user: User) -> dict[str, Any]:
    return {
        "updated_at": catalogue()["updated_at"],
        "items": [
            {
                "id": m["id"],
                "label": m["label"],
                "sources": list(m["sources"]),
                "model_count": len(m["models"]),
            }
            for m in makes().values()
        ],
    }


@app.get("/api/catalog/{make}")
def model_catalog(make: str, user: User) -> dict[str, Any]:
    if make not in makes():
        raise HTTPException(404, "Марка не найдена")
    return {
        "items": [
            {"id": m["id"], "label": m["label"], "sources": list(m["sources"])} for m in models(make).values()
        ]
    }


def search_values(body: SearchInput) -> dict[str, Any]:
    from app.sources.base import SourceFailure
    from app.sources.registry import source_enabled

    if not settings().test_fixtures_enabled:
        for source in body.enabled_sources:
            if source not in {Source.DROM, Source.AUTO_RU} or not source_enabled(source):
                raise HTTPException(422, "Выберите доступную площадку")
            if not body.filters.make:
                raise HTTPException(422, "Выберите марку автомобиля")
            try:
                source_scope(source, body.filters.make, body.filters.model)
            except SourceFailure as exc:
                raise HTTPException(422, "Эта модель недоступна на выбранной площадке") from exc
    values = body.model_dump(mode="json")
    values["name"] = search_name(values["filters"])
    return values


@app.post("/api/searches", status_code=201)
def create_search(body: SearchInput, user: User, start: bool = True) -> dict[str, Any]:
    values = search_values(body)
    with engine().begin() as conn:
        conn.execute(sa.select(t.users.c.id).where(t.users.c.id == user["id"]).with_for_update()).one()
        if (
            conn.execute(
                sa.select(sa.func.count()).select_from(t.searches).where(t.searches.c.user_id == user["id"])
            ).scalar_one()
            >= 30
        ):
            raise HTTPException(409, "Лимит: 30 сохранённых поисков")
        search_id = conn.execute(
            t.searches.insert()
            .values(user_id=user["id"], **values, next_refresh_at=next_refresh(1500))
            .returning(t.searches.c.id)
        ).scalar_one()
        run_id = enqueue(conn, owned(conn, search_id, user["id"])) if start else None
    return {"search_id": search_id, "run_id": run_id}


@app.get("/api/searches")
def list_searches(user: User) -> dict[str, Any]:
    with engine().connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                sa.select(t.searches)
                .where(
                    t.searches.c.user_id == user["id"],
                    sa.or_(
                        sa.literal(settings().test_fixtures_enabled),
                        t.searches.c.enabled_sources.op("?")("drom"),
                        t.searches.c.enabled_sources.op("?")("auto_ru"),
                    ),
                )
                .order_by(t.searches.c.created_at.desc())
            ).mappings()
        ]
    for row in rows:
        row["name"] = search_name(row["filters"])
        if not settings().test_fixtures_enabled:
            row["enabled_sources"] = [s for s in row["enabled_sources"] if s in {"drom", "auto_ru"}]
    return {"items": rows}


@app.get("/api/searches/{search_id}")
def get_search(search_id: uuid.UUID, user: User) -> dict[str, Any]:
    with engine().connect() as conn:
        result = owned(conn, search_id, user["id"])
        result["name"] = search_name(result["filters"])
        if not settings().test_fixtures_enabled:
            result["enabled_sources"] = [s for s in result["enabled_sources"] if s in {"drom", "auto_ru"}]
            if not result["enabled_sources"]:
                raise HTTPException(404, "Поиск не найден")
        result["sources"] = [
            dict(r)
            for r in conn.execute(
                sa.select(t.source_states).where(
                    t.source_states.c.search_id == search_id, visible_source(t.source_states.c.source)
                )
            ).mappings()
        ]
        result["latest_run"] = conn.execute(
            sa.select(t.runs.c.id)
            .where(t.runs.c.search_id == search_id)
            .order_by(t.runs.c.requested_at.desc())
            .limit(1)
        ).scalar()
        return result


@app.put("/api/searches/{search_id}")
def edit_search(search_id: uuid.UUID, body: SearchInput, user: User) -> dict[str, Any]:
    values = search_values(body)
    with engine().begin() as conn:
        search = owned(conn, search_id, user["id"], lock=True)
        if conn.execute(
            sa.select(t.jobs.c.id).where(
                t.jobs.c.search_id == search_id, t.jobs.c.state.in_(["queued", "running"])
            )
        ).first():
            raise HTTPException(409, "Дождитесь завершения поиска")
        conn.execute(
            t.searches.update()
            .where(t.searches.c.id == search_id)
            .values(
                **values,
                filters_version=search["filters_version"]
                + int(
                    search["filters"] != body.filters.model_dump(mode="json")
                    or set(search["enabled_sources"]) != set(body.enabled_sources)
                ),
            )
        )
    return {"id": search_id}


@app.delete("/api/searches/{search_id}", status_code=204)
def delete_search(search_id: uuid.UUID, user: User) -> None:
    with engine().begin() as conn:
        owned(conn, search_id, user["id"], lock=True)
        if conn.execute(
            sa.select(t.jobs.c.id).where(
                t.jobs.c.search_id == search_id, t.jobs.c.state.in_(["queued", "running"])
            )
        ).first():
            raise HTTPException(409, "Дождитесь завершения поиска перед удалением")
        conn.execute(t.searches.delete().where(t.searches.c.id == search_id))


class MonitoringInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    refresh_interval_seconds: int = Field(ge=1200, le=86400)


@app.patch("/api/searches/{search_id}/monitoring")
def edit_monitoring(search_id: uuid.UUID, body: MonitoringInput, user: User) -> dict[str, Any]:
    with engine().begin() as conn:
        search = owned(conn, search_id, user["id"], lock=True)
        due = search["next_refresh_at"]
        if not body.enabled:
            due = None
        elif not search["enabled"] or body.refresh_interval_seconds != search["refresh_interval_seconds"]:
            due = next_refresh(body.refresh_interval_seconds)
        conn.execute(
            t.searches.update()
            .where(t.searches.c.id == search_id)
            .values(
                enabled=body.enabled,
                refresh_interval_seconds=body.refresh_interval_seconds,
                next_refresh_at=due,
            )
        )
    return {"id": search_id, "enabled": body.enabled, "next_refresh_at": due}


@app.post("/api/searches/{search_id}/refresh", status_code=202)
def refresh_search(search_id: uuid.UUID, user: User) -> dict[str, Any]:
    with engine().begin() as conn:
        run_id = enqueue(conn, owned(conn, search_id, user["id"], lock=True))
    return {"run_id": run_id, "search_id": search_id}


@app.get("/api/search-runs/{run_id}")
def get_run(run_id: uuid.UUID, user: User) -> dict[str, Any]:
    with engine().connect() as conn:
        row = (
            conn.execute(sa.select(t.runs).where(t.runs.c.id == run_id, t.runs.c.user_id == user["id"]))
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(404, "Запуск не найден")
        return dict(row) | {
            "sources": [
                dict(r)
                for r in conn.execute(
                    sa.select(
                        t.jobs.c.source,
                        t.jobs.c.state,
                        t.jobs.c.error_code,
                        t.jobs.c.result_count,
                        t.jobs.c.warnings,
                        t.jobs.c.not_before,
                        t.jobs.c.attempt,
                    ).where(t.jobs.c.run_id == run_id, visible_source(t.jobs.c.source))
                ).mappings()
            ]
        }


def public_listing(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: str(v) if k in {"price", "engine_volume"} and v is not None else v
        for k, v in row.items()
        if k not in {"source_metadata", "field_observed_at"}
    }


@app.get("/api/vehicles")
def vehicles(
    user: User,
    search_id: uuid.UUID | None = None,
    include_unverified: bool = False,
    after: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    view: Literal["all", "new", "favourites", "hidden", "changed"] = "all",
    sort: Literal["found", "newest", "price_asc", "price_desc", "mileage", "price_drop"] = "found",
) -> dict[str, Any]:
    with engine().connect() as conn:
        return vehicle_page(conn, user["id"], search_id, include_unverified, after, limit, view, sort)


@app.get("/api/vehicles/{cluster_id}")
def vehicle_detail(cluster_id: uuid.UUID, user: User) -> dict[str, Any]:
    with engine().connect() as conn:
        rows = [
            public_listing(dict(r))
            for r in conn.execute(
                sa.select(t.listings, t.memberships.c.attached_at)
                .join(t.memberships)
                .where(
                    t.memberships.c.cluster_id == cluster_id,
                    t.memberships.c.user_id == user["id"],
                    visible_source(t.listings.c.source),
                )
                .order_by(
                    (t.listings.c.status == "ACTIVE").desc(),
                    t.listings.c.last_seen_at.desc(),
                    t.listings.c.id,
                )
            ).mappings()
        ]
        if not rows:
            raise HTTPException(404, "Автомобиль не найден")
        history = [
            dict(r)
            for r in conn.execute(
                sa.select(t.snapshots)
                .join(t.memberships, t.memberships.c.listing_id == t.snapshots.c.listing_id)
                .where(
                    t.memberships.c.cluster_id == cluster_id,
                    t.memberships.c.user_id == user["id"],
                    t.snapshots.c.observed_at >= t.memberships.c.attached_at,
                    t.snapshots.c.listing_id.in_([row["id"] for row in rows]),
                )
                .order_by(t.snapshots.c.observed_at.desc())
                .limit(100)
            ).mappings()
        ]
        cluster = dict(
            conn.execute(
                sa.select(t.clusters).where(t.clusters.c.id == cluster_id, t.clusters.c.user_id == user["id"])
            )
            .mappings()
            .one()
        )
        links = [
            dict(r)
            for r in conn.execute(
                sa.select(t.relist_links).where(
                    t.relist_links.c.user_id == user["id"],
                    t.relist_links.c.old_id.in_([r["id"] for r in rows]),
                )
            ).mappings()
        ]
        for row in rows:
            row["display_status"] = (
                "RELISTED" if any(link["old_id"] == row["id"] for link in links) else row["status"]
            )
        member_ids = [r["id"] for r in rows]
        matching = [
            dict(r)
            for r in conn.execute(
                sa.select(t.candidates)
                .where(
                    t.candidates.c.user_id == user["id"],
                    t.candidates.c.left_id.in_(member_ids),
                    t.candidates.c.right_id.in_(member_ids),
                )
                .order_by(t.candidates.c.updated_at.desc())
                .limit(100)
            ).mappings()
        ]
        manual = [
            dict(r)
            for r in conn.execute(
                sa.select(t.decisions)
                .where(
                    t.decisions.c.user_id == user["id"],
                    t.decisions.c.left_id.in_(member_ids),
                    t.decisions.c.right_id.in_(member_ids),
                    t.decisions.c.superseded_at.is_(None),
                )
                .limit(100)
            ).mappings()
        ]
    return {
        "id": cluster_id,
        "version": cluster["version"],
        "listings": rows,
        "snapshots": history,
        "relist_links": links,
        "matching": matching,
        "manual_decisions": manual,
        "favourite": cluster["favourite"],
        "hidden": cluster["hidden"],
        "notes": cluster["notes"],
        "first_seen_at": cluster["first_seen_at"],
    }
