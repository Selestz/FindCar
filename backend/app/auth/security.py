import hashlib
import secrets
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db import schema as t
from app.db.connection import engine

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))
COOKIE = "findcar_session"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def require_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Войдите в аккаунт")
    with engine().connect() as conn:
        row = (
            conn.execute(
                sa.select(t.users.c.id, t.users.c.username, t.users.c.role, t.sessions.c.csrf_token)
                .join(t.sessions)
                .where(
                    t.sessions.c.token_digest == digest(token),
                    t.sessions.c.expires_at > t.now(),
                    t.users.c.enabled.is_(True),
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(401, "Сессия закончилась. Войдите снова")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf_token"]):
            raise HTTPException(403, "Не удалось проверить запрос. Обновите страницу")
    return dict(row)


def authenticate(username: str, password: str, client: str) -> dict[str, Any]:
    key = digest(client)
    with engine().begin() as conn:
        conn.execute(
            insert(t.login_limits).values(key=key, attempts=0, window_start=t.now()).on_conflict_do_nothing()
        )
        limit = (
            conn.execute(sa.select(t.login_limits).where(t.login_limits.c.key == key).with_for_update())
            .mappings()
            .one()
        )
        if limit["window_start"] < t.now() - timedelta(minutes=15):
            conn.execute(
                t.login_limits.update()
                .where(t.login_limits.c.key == key)
                .values(attempts=1, window_start=t.now())
            )
        elif limit["attempts"] >= 20:
            raise HTTPException(429, "Слишком много попыток входа", headers={"Retry-After": "900"})
        else:
            conn.execute(
                t.login_limits.update()
                .where(t.login_limits.c.key == key)
                .values(attempts=limit["attempts"] + 1)
            )
    with engine().connect() as conn:
        row = (
            conn.execute(sa.select(t.users).where(sa.func.lower(t.users.c.username) == username.casefold()))
            .mappings()
            .first()
        )
    try:
        valid: bool = hasher.verify(row["password_hash"] if row else DUMMY_HASH, password)
    except VerificationError:
        valid = False
    if row is None or not row["enabled"] or not valid:
        raise HTTPException(401, "Неверное имя пользователя или пароль")
    return dict(row)


def issue_session(user_id: Any, old_token: str | None = None) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with engine().begin() as conn:
        conn.execute(t.sessions.delete().where(t.sessions.c.expires_at <= t.now()))
        if old_token:
            conn.execute(t.sessions.delete().where(t.sessions.c.token_digest == digest(old_token)))
        conn.execute(
            t.sessions.insert().values(
                user_id=user_id,
                token_digest=digest(token),
                csrf_token=csrf,
                expires_at=t.now() + timedelta(hours=settings().session_hours),
            )
        )
    return token, csrf
