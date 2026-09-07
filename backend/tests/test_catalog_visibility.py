from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from test_matching import C, car, seed, versions

from app.catalog import canonical_names, makes, models, search_name, source_scope
from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.domain.models import UnifiedSearchFilters
from app.services.ingestion import event, ingest
from app.services.matching import merge
from app.sources.auto_ru.adapter import search_url as auto_url
from app.sources.base import SourceFailure
from app.sources.drom import parser
from app.sources.drom.adapter import search_url as drom_url
from app.sources.drom.normalizer import normalize
from app.sources.registry import adapter_for, source_enabled


def production(monkeypatch):
    monkeypatch.setenv("TEST_FIXTURES_ENABLED", "false")
    monkeypatch.setenv("DROM_ENABLED", "true")
    monkeypatch.setenv("AUTO_RU_ENABLED", "true")
    settings.cache_clear()


def test_catalog_source_mappings_and_roundtrips():
    assert len(makes()) >= 70
    assert sum(len(models(make)) for make in makes()) > 3000
    for make in makes():
        for model in models(make).values():
            assert model["id"] not in {"engine", "frame", "photo"}
            for source in model["sources"]:
                remote = source_scope(source, make, model["id"])
                assert canonical_names(source, *remote) == (make, model["id"])
    assert source_scope("auto_ru", "bmw", "3-series") == ("bmw", "3er")
    assert source_scope("auto_ru", "lada", None) == ("vaz", None)
    assert source_scope("auto_ru", "lada", "priora") == ("vaz", "2170")
    assert source_scope("auto_ru", "mazda", "mazda3") == ("mazda", "3")
    assert search_name({"price_to": "100000000"}) == "Автомобили · до 100 млн ₽"
    assert source_scope("auto_ru", "mercedes-benz", None) == ("mercedes", None)
    assert "/bmw/3er/" in auto_url(UnifiedSearchFilters(make="bmw", model="3-series"), 1)
    assert drom_url(UnifiedSearchFilters(make="toyota"), 1).startswith("https://auto.drom.ru/toyota/")
    with pytest.raises(SourceFailure):
        source_scope("drom", "toyota", "../../private")


def test_catalog_authenticated_and_automatic_search_name(client, monkeypatch):
    production(monkeypatch)
    assert len(client.get("/api/catalog").json()["items"]) >= 70
    assert len(client.get("/api/catalog/toyota").json()["items"]) > 50
    assert client.get("/api/catalog/unknown").status_code == 404
    body = {
        "name": "Ignored old client title",
        "enabled_sources": ["drom"],
        "filters": {
            "make": "porsche",
            "model": "panamera",
            "year_from": 2010,
            "year_to": 2015,
            "price_to": "1900000.00",
        },
    }
    response = client.post("/api/searches?start=false", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["run_id"] is None
    path = "/api/searches/" + response.json()["search_id"]
    search = client.get(path).json()
    assert search["name"] == "Porsche Panamera · 2010–2015 · до 1,9 млн ₽"
    assert search["latest_run"] is None
    assert client.post("/api/searches", json=body | {"enabled_sources": ["mock"]}).status_code == 422
    assert client.post("/api/searches", json=body | {"filters": {"make": "unknown"}}).status_code == 422
    assert client.put(path, json=body | {"filters": {"make": "bmw", "model": "3-series"}}).status_code == 200
    assert client.get(path).json()["name"] == "BMW 3-Series"
    assert (
        search_name({"make": "toyota", "year_from": 2018, "price_from": "1000000"})
        == "Toyota · от 2018 · от 1 млн ₽"
    )
    assert client.post(path + "/refresh").status_code == 202


def test_production_excludes_existing_demo_everywhere(client, db, monkeypatch):
    search, ids = seed(db["alice"], [car(source_listing_id="demo")], with_photos=False)
    with engine().connect() as conn:
        cluster = next(iter(versions(conn, db["alice"])))
    assert client.get("/api/vehicles").json()["total"] == 1
    production(monkeypatch)
    assert not source_enabled("mock")
    with pytest.raises(SourceFailure):
        adapter_for("mock")
    assert {s["source"] for s in client.get("/api/sources/health").json()["items"]} == {"drom", "auto_ru"}
    assert client.get("/api/vehicles?include_unverified=true").json()["total"] == 0
    assert client.get("/api/searches").json()["items"] == []
    assert client.get(f"/api/searches/{search}").status_code == 404
    assert client.get(f"/api/vehicles/{cluster}").status_code == 404
    assert client.get(f"/api/listings/{ids[0]}/image/0").status_code == 404
    assert client.get("/api/events").json() == {"items": [], "unread_count": 0, "next_cursor": None}


def test_removed_hidden_from_results_and_history_kept(client, db):
    search, _ = seed(
        db["alice"],
        [
            car(source_listing_id="removed", status="REMOVED"),
            car(source_listing_id="unknown", status="UNKNOWN"),
        ],
        with_photos=False,
    )
    with engine().begin() as conn:
        conn.execute(t.clusters.update().values(favourite=True, notes=[{"text": "Keep inspection notes"}]))
        clusters = versions(conn, db["alice"])
    for view in ("all", "new", "changed", "favourites", "hidden"):
        response = client.get(
            "/api/vehicles", params={"search_id": str(search), "view": view, "include_unverified": True}
        )
        assert response.json()["total"] == 0
    detail = client.get(f"/api/vehicles/{next(iter(clusters))}").json()
    assert detail["notes"] and detail["snapshots"]


def test_active_source_survives_merge_without_removed_price(client, db):
    seed(
        db["alice"],
        [
            car(source_listing_id="removed", status="REMOVED", price="1000000"),
            car(source_listing_id="live", price="1900000"),
        ],
        with_photos=False,
    )
    with engine().begin() as conn:
        merge(conn, db["alice"], versions(conn, db["alice"]), manual=True, config=C)
    data = client.get("/api/vehicles").json()
    assert data["total"] == 1
    assert data["items"][0]["listing_count"] == 1
    assert Decimal(data["items"][0]["price_min"]) == Decimal("1900000")
    detail = client.get("/api/vehicles/" + data["items"][0]["cluster_id"]).json()
    assert len(detail["listings"]) == 2


def test_demo_events_do_not_leak_into_mixed_real_cluster(client, db, monkeypatch):
    _, ids = seed(
        db["alice"], [car(source_listing_id="demo"), car(source_listing_id="real")], with_photos=False
    )
    with engine().begin() as conn:
        merge(conn, db["alice"], versions(conn, db["alice"]), manual=True, config=C)
        conn.execute(t.listings.update().where(t.listings.c.id == ids[1]).values(source="drom"))
        event(
            conn,
            db["alice"],
            ids[0],
            "PRICE_DROP",
            "demo-drop",
            {"old_price": "2000000", "new_price": "1000000"},
        )
    production(monkeypatch)
    row = client.get("/api/vehicles").json()["items"][0]
    assert row["sources"] == ["drom"] and row["price_drop_amount"] is None
    detail = client.get("/api/vehicles/" + row["cluster_id"]).json()
    assert len(detail["listings"]) == 1
    assert all(str(s["listing_id"]) == str(ids[1]) for s in detail["snapshots"])


def test_cached_drom_card_cannot_revive_sold_offer(client, db):
    fixture = Path(__file__).parent / "fixtures/drom"
    removed = parser.detail(
        (fixture / "detail.html").read_text(encoding="utf8").replace("/InStock", "/OutOfStock"), "800000001"
    )
    active = parser.search((fixture / "search.html").read_text(encoding="utf8"), 1).items[0]
    now = t.now()
    with engine().begin() as conn:
        search_id = conn.execute(
            t.searches.insert()
            .values(user_id=db["alice"], name="Drom", filters={}, enabled_sources=["drom"])
            .returning(t.searches.c.id)
        ).scalar_one()
        search = dict(
            conn.execute(sa.select(t.searches).where(t.searches.c.id == search_id)).mappings().one()
        )
        ingest(conn, search, normalize(removed, now))
        ingest(conn, search, normalize(active, now + timedelta(seconds=1)))
        assert conn.execute(sa.select(t.listings.c.status)).scalar_one() == "REMOVED"
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(t.events).where(t.events.c.type == "LISTING_RETURNED")
            ).scalar_one()
            == 0
        )
        live_detail = parser.detail((fixture / "detail.html").read_text(encoding="utf8"), "800000001")
        ingest(conn, search, normalize(live_detail, now + timedelta(seconds=2)))
        assert conn.execute(sa.select(t.listings.c.status)).scalar_one() == "ACTIVE"
    assert client.get("/api/vehicles").json()["total"] == 1
