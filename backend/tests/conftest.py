import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.auth.security import hasher
from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.main import app


@pytest.fixture
def db(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for real PostgreSQL integration tests")
    if not sa.engine.make_url(url).database.endswith("_test"):
        pytest.fail("Test database name must end with _test; refusing to clear other databases")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ORIGIN", "http://testserver")
    monkeypatch.setenv("MANUAL_REFRESH_COOLDOWN", "0")
    monkeypatch.setenv("MOCK_SCENARIO", "normal")
    monkeypatch.setenv("DROM_ENABLED", "false")
    monkeypatch.setenv("AUTO_RU_ENABLED", "false")
    settings.cache_clear()
    engine.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with engine().begin() as conn:
        names = ", ".join('"' + name + '"' for name in t.metadata.tables)
        conn.execute(sa.text(f"TRUNCATE {names} CASCADE"))
        alice = conn.execute(
            t.users.insert()
            .values(username="alice", password_hash=hasher.hash("test-password-alice"), role="admin")
            .returning(t.users.c.id)
        ).scalar_one()
        bob = conn.execute(
            t.users.insert()
            .values(username="bob", password_hash=hasher.hash("test-password-bob"))
            .returning(t.users.c.id)
        ).scalar_one()
    yield {"alice": alice, "bob": bob}
    engine().dispose()
    engine.cache_clear()
    settings.cache_clear()


@pytest.fixture
def client(db):
    with TestClient(app, headers={"Origin": "http://testserver"}) as c:
        response = c.post("/api/auth/login", json={"username": "alice", "password": "test-password-alice"})
        assert response.status_code == 200, response.text
        c.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield c
