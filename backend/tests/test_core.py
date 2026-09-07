import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.main import app
from app.services.ingestion import ingest
from app.sources.mock.adapter import MockSourceAdapter
from app.worker.runner import claim, run_once

SEARCH = {
    "name": "Panamera до 1,9 млн",
    "filters": {
        "make": "porsche",
        "model": "panamera",
        "price_to": "1900000",
        "region": "москва",
        "owners_max": 5,
    },
}


def drain():
    for _ in range(10):
        if not asyncio.run(run_once()):
            return
    pytest.fail("Queue did not drain")


def count(table):
    with engine().connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def create(client, **extra):
    response = client.post("/api/searches", json=SEARCH | extra)
    assert response.status_code == 201, response.text
    drain()
    return response.json()


def test_vertical_slice_idempotency_and_partial(client):
    ids = create(client, enabled_sources=["mock", "avito"])
    run = client.get("/api/search-runs/" + ids["run_id"]).json()
    assert run["outcome"] == "partial"
    response = client.get("/api/vehicles", params={"search_id": ids["search_id"]})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 2 and data["unverified_count"] == 1
    assert len(data["items"]) == 2
    assert data["items"][0]["source"] == "mock"
    initial = [count(table) for table in (t.listings, t.clusters, t.snapshots, t.events)]
    assert initial == [4, 3, 4, 3]
    assert client.post(f"/api/searches/{ids['search_id']}/refresh").status_code == 202
    drain()
    assert initial == [count(table) for table in (t.listings, t.clusters, t.snapshots, t.events)]
    page = client.get("/api/vehicles", params={"search_id": ids["search_id"], "limit": 1}).json()
    next_page = client.get(
        "/api/vehicles", params={"search_id": ids["search_id"], "limit": 1, "after": page["next_cursor"]}
    ).json()
    assert page["items"][0]["id"] != next_page["items"][0]["id"]
    assert next_page["next_cursor"] is None


def test_user_isolation_csrf_logout(client, db):
    ids = create(client)
    car = client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()["items"][0]
    with TestClient(app, headers={"Origin": "http://testserver"}) as bob:
        login = bob.post("/api/auth/login", json={"username": "bob", "password": "test-password-bob"})
        bob.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        for url in [
            f"/api/searches/{ids['search_id']}",
            f"/api/search-runs/{ids['run_id']}",
            f"/api/vehicles/{car['cluster_id']}",
        ]:
            assert bob.get(url).status_code == 404
        assert bob.post(f"/api/searches/{ids['search_id']}/refresh").status_code == 404
        assert bob.delete(f"/api/searches/{ids['search_id']}").status_code == 404
        assert bob.get("/api/searches").json()["items"] == []
        assert bob.get("/api/events").json()["items"] == []
        assert (
            bob.post(
                "/api/admin/users", json={"username": "third", "password": "sufficient-password"}
            ).status_code
            == 403
        )
        own = create(bob)
        other_car = bob.get("/api/vehicles", params={"search_id": own["search_id"]}).json()["items"][0]
        assert other_car["cluster_id"] != car["cluster_id"]
    assert count(t.listings) == 4
    assert client.post("/api/searches", json=SEARCH, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert (
        client.post("/api/searches", json=SEARCH, headers={"Origin": "https://evil.invalid"}).status_code
        == 403
    )
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/searches").status_code == 401


def test_price_snapshot_roundtrip_and_source_failure(client, monkeypatch):
    ids = create(client)
    for scenario in ("price_drop", "normal"):
        monkeypatch.setenv("MOCK_SCENARIO", scenario)
        settings.cache_clear()
        assert client.post(f"/api/searches/{ids['search_id']}/refresh").status_code == 202
        drain()
    assert count(t.snapshots) == 6
    events = client.get("/api/events").json()["items"]
    assert sum(e["type"] == "PRICE_DROP" for e in events) == 1
    assert sum(e["type"] == "PRICE_INCREASE" for e in events) == 1
    for scenario in ("empty", "parser_error"):
        monkeypatch.setenv("MOCK_SCENARIO", scenario)
        settings.cache_clear()
        client.post(f"/api/searches/{ids['search_id']}/refresh")
        drain()
    assert count(t.snapshots) == 6
    assert client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()["total"] == 2
    assert not any(e["type"] == "LISTING_REMOVED" for e in client.get("/api/events").json()["items"])


def test_cooldown_and_active_run_reuse(client, monkeypatch):
    monkeypatch.setenv("MANUAL_REFRESH_COOLDOWN", "300")
    settings.cache_clear()
    response = client.post("/api/searches", json=SEARCH)
    ids = response.json()
    active = client.post(f"/api/searches/{ids['search_id']}/refresh")
    assert active.json()["run_id"] == ids["run_id"]
    drain()
    denied = client.post(f"/api/searches/{ids['search_id']}/refresh")
    assert denied.status_code == 429
    assert int(denied.headers["Retry-After"]) > 0


def test_lease_reclaim(client):
    ids = client.post("/api/searches", json=SEARCH).json()
    job = claim()
    with engine().begin() as conn:
        conn.execute(
            t.jobs.update().where(t.jobs.c.id == job["id"]).values(lease_until=t.now() - timedelta(seconds=1))
        )
    drain()
    assert client.get("/api/search-runs/" + ids["run_id"]).json()["outcome"] == "complete"
    assert count(t.clusters) == 3


def test_partial_and_stale_observations_do_not_erase_fields(client):
    ids = create(client)
    adapter = MockSourceAdapter()
    with engine().begin() as conn:
        search = dict(
            conn.execute(sa.select(t.searches).where(t.searches.c.id == ids["search_id"])).mappings().one()
        )
        raw = adapter.rows()[0]
        partial = {k: v for k, v in raw.items() if k != "description"}
        ingest(conn, search, adapter.normalize(partial, t.now()))
        ingest(conn, search, adapter.normalize(raw | {"price": "100"}, t.now() - timedelta(days=1)))
        listing = (
            conn.execute(
                sa.select(t.listings).where(t.listings.c.source_listing_id == raw["source_listing_id"])
            )
            .mappings()
            .one()
        )
        assert listing["description"] == raw["description"]
        assert listing["price"] == 1790000


def test_database_rejects_cross_user_membership(client, db):
    create(client)
    with engine().connect() as conn:
        member = conn.execute(sa.select(t.memberships)).mappings().first()
    with pytest.raises(IntegrityError), engine().begin() as conn:
        conn.execute(
            t.memberships.insert().values(
                user_id=db["bob"], listing_id=member["listing_id"], cluster_id=member["cluster_id"]
            )
        )


def test_invalid_login_and_disabled_user(db):
    with TestClient(app, headers={"Origin": "http://testserver"}) as c:
        assert c.post("/api/auth/login", json={"username": "alice", "password": "bad"}).status_code == 401
        with engine().begin() as conn:
            conn.execute(t.users.update().where(t.users.c.id == db["alice"]).values(enabled=False))
        assert (
            c.post(
                "/api/auth/login", json={"username": "alice", "password": "test-password-alice"}
            ).status_code
            == 401
        )


def test_admin_creates_account_and_rejects_blank_name(client):
    assert (
        client.post("/api/admin/users", json={"username": " ", "password": "long-test-password"}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/admin/users", json={"username": "friend", "password": "long-test-password"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/admin/users", json={"username": "FRIEND", "password": "long-test-password"}
        ).status_code
        == 409
    )
    with TestClient(app, headers={"Origin": "http://testserver"}) as friend:
        response = friend.post(
            "/api/auth/login", json={"username": "friend", "password": "long-test-password"}
        )
        assert response.status_code == 200
        assert response.json()["role"] == "user"


def test_session_expiry_and_login_rate_limit(client):
    with engine().begin() as conn:
        conn.execute(t.sessions.update().values(expires_at=t.now() - timedelta(seconds=1)))
    assert client.get("/api/auth/me").status_code == 401
    response = None
    for _ in range(21):
        response = client.post("/api/auth/login", json={"username": "missing", "password": "incorrect"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"
