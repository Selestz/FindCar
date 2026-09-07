import uuid
from decimal import Decimal

import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_core import create, drain
from test_matching import car, seed

from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.main import app


def test_favourites_hidden_notes_and_versions(client):
    search = create(client)["search_id"]
    listing = client.get("/api/vehicles", params={"search_id": search}).json()["items"][0]
    cid = listing["cluster_id"]
    path = f"/api/vehicles/{cid}/state"
    state = client.patch(
        path,
        json={
            "version": listing["cluster_version"],
            "favourite": True,
            "note": "Уточнить историю обслуживания",
        },
    )
    assert state.status_code == 200, state.text
    assert client.get("/api/vehicles", params={"view": "favourites"}).json()["total"] == 1
    detail = client.get(f"/api/vehicles/{cid}").json()
    assert detail["notes"][0]["text"] == "Уточнить историю обслуживания"
    nid = detail["notes"][0]["id"]
    assert (
        client.patch(path, json={"version": listing["cluster_version"], "favourite": False}).status_code
        == 409
    )
    edit = client.patch(
        f"/api/vehicles/{cid}/notes/{nid}",
        json={"version": detail["version"], "text": "Осмотр в субботу <script>test</script>"},
    )
    assert edit.status_code == 200, edit.text
    hidden = client.patch(path, json={"version": edit.json()["version"], "hidden": True})
    assert hidden.status_code == 200
    assert client.get("/api/vehicles", params={"view": "hidden"}).json()["total"] == 1
    assert client.get("/api/vehicles", params={"view": "favourites"}).json()["total"] == 0
    assert client.get("/api/vehicles", params={"search_id": search}).json()["total"] == 1
    restored = client.patch(path, json={"version": hidden.json()["version"], "hidden": False})
    deleted = client.patch(
        f"/api/vehicles/{cid}/notes/{nid}", json={"version": restored.json()["version"], "text": None}
    )
    assert deleted.status_code == 200
    assert client.get(f"/api/vehicles/{cid}").json()["notes"] == []


def test_state_and_events_are_user_scoped(client, db):
    search = create(client)["search_id"]
    row = client.get("/api/vehicles", params={"search_id": search}).json()["items"][0]
    events = client.get("/api/events").json()["items"]
    with TestClient(app, headers={"Origin": "http://testserver"}) as bob:
        login = bob.post("/api/auth/login", json={"username": "bob", "password": "test-password-bob"})
        bob.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (
            bob.patch(
                f"/api/vehicles/{row['cluster_id']}/state",
                json={"version": row["cluster_version"], "note": "foreign"},
            ).status_code
            == 404
        )
        assert bob.get("/api/vehicles", params={"view": "favourites"}).json()["total"] == 0
        assert bob.get("/api/duplicates").json()["items"] == []
        assert bob.post("/api/events/read", json={"ids": [events[0]["id"]]}).status_code == 404
        assert bob.get("/api/events", params={"after": events[0]["id"]}).status_code == 404
    with engine().connect() as conn:
        assert (
            conn.execute(
                sa.select(t.events.c.read_at).where(t.events.c.id == uuid.UUID(events[0]["id"]))
            ).scalar_one()
            is None
        )


def test_events_pagination_read_and_new_filter(client):
    search = create(client)["search_id"]
    first = client.get("/api/events", params={"limit": 1}).json()
    second = client.get("/api/events", params={"limit": 1, "after": first["next_cursor"]}).json()
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert first["items"][0]["cluster_id"]
    assert client.get("/api/vehicles", params={"search_id": search, "view": "new"}).json()["total"] == 2
    ids = [e["id"] for e in client.get("/api/events").json()["items"]]
    assert client.post("/api/events/read", json={"ids": ids}).status_code == 200
    assert client.post("/api/events/read", json={"ids": ids}).status_code == 200
    assert client.get("/api/events", params={"unread": True}).json()["unread_count"] == 0
    assert client.get("/api/vehicles", params={"search_id": search, "view": "new"}).json()["total"] == 0


def test_sorting_and_cluster_pagination(client, db):
    search, _ = seed(
        db["alice"],
        [
            car(source_listing_id="cheap", price="1000000", mileage_km=180000),
            car(source_listing_id="dear", price="2000000", mileage_km=100000),
            car(source_listing_id="middle", price="1500000", mileage_km=130000),
        ],
        with_photos=False,
    )
    for sort, expected in [("price_asc", "cheap"), ("price_desc", "dear"), ("mileage", "dear")]:
        first = client.get(
            "/api/vehicles", params={"search_id": str(search), "sort": sort, "limit": 1}
        ).json()
        assert first["items"][0]["source_listing_id"] == expected
        second = client.get(
            "/api/vehicles",
            params={"search_id": str(search), "sort": sort, "limit": 1, "after": first["next_cursor"]},
        ).json()
        assert second["items"][0]["id"] != first["items"][0]["id"]
    assert client.get("/api/vehicles", params={"sort": "invalid"}).status_code == 422
    assert client.get("/api/vehicles", params={"after": str(uuid.uuid4())}).status_code == 409


def test_deleted_search_keeps_favourites_and_notes(client):
    search = create(client)["search_id"]
    row = client.get("/api/vehicles", params={"search_id": search}).json()["items"][0]
    assert (
        client.patch(
            f"/api/vehicles/{row['cluster_id']}/state",
            json={
                "version": row["cluster_version"],
                "favourite": True,
                "note": "Сохранить после удаления поиска",
            },
        ).status_code
        == 200
    )
    assert client.delete(f"/api/searches/{search}").status_code == 204
    assert client.get("/api/vehicles", params={"view": "favourites"}).json()["total"] == 1
    assert client.get(f"/api/vehicles/{row['cluster_id']}").json()["notes"]


def test_state_validation_and_csrf(client):
    search = create(client)["search_id"]
    row = client.get("/api/vehicles", params={"search_id": search}).json()["items"][0]
    path = f"/api/vehicles/{row['cluster_id']}/state"
    assert client.patch(path, json={"version": row["cluster_version"], "note": " "}).status_code == 422
    assert client.patch(path, json={"version": row["cluster_version"], "note": "x" * 5001}).status_code == 422
    assert (
        client.patch(
            path,
            json={"version": row["cluster_version"], "hidden": True},
            headers={"X-CSRF-Token": "invalid"},
        ).status_code
        == 403
    )


def test_changed_filter_and_price_drop_sort(client, monkeypatch):
    search = create(client)["search_id"]
    assert client.get("/api/vehicles", params={"search_id": search, "view": "changed"}).json()["total"] == 0
    monkeypatch.setenv("MOCK_SCENARIO", "price_drop")
    settings.cache_clear()
    assert client.post(f"/api/searches/{search}/refresh").status_code == 202
    drain()
    result = client.get(
        "/api/vehicles", params={"search_id": search, "view": "changed", "sort": "price_drop"}
    ).json()
    assert result["total"] == 1 and Decimal(result["items"][0]["price_drop_amount"]) == Decimal("100000")


def test_cannot_delete_running_search(client):
    result = client.post(
        "/api/searches", json={"name": "Queued", "filters": {}, "enabled_sources": ["mock"]}
    ).json()
    assert client.delete("/api/searches/" + result["search_id"]).status_code == 409
    drain()
    assert client.delete("/api/searches/" + result["search_id"]).status_code == 204
