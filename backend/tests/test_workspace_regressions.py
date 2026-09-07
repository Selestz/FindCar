from decimal import Decimal

from test_core import create
from test_matching import C, car, seed, versions

from app.db import schema as t
from app.db.connection import engine
from app.services.matching import merge


def test_active_price_wins_over_removed_price_and_missing_stays_unknown(client, db):
    search, _ = seed(
        db["alice"],
        [
            car(source_listing_id="old", status="REMOVED", price="1000000"),
            car(source_listing_id="active", price="1900000"),
        ],
        with_photos=False,
    )
    with engine().begin() as conn:
        merge(conn, db["alice"], versions(conn, db["alice"]), manual=True, config=C)
    result = client.get("/api/vehicles", params={"search_id": str(search)}).json()["items"][0]
    assert Decimal(result["price_min"]) == Decimal("1900000")
    with engine().begin() as conn:
        conn.execute(t.listings.update().where(t.listings.c.source_listing_id == "active").values(price=None))
    result = client.get("/api/vehicles", params={"search_id": str(search)}).json()["items"][0]
    assert result["price_min"] is None and result["price"] is None


def test_renaming_search_preserves_current_results(client):
    search = create(client)["search_id"]
    current = client.get("/api/searches/" + search).json()
    response = client.put(
        "/api/searches/" + search,
        json={
            "name": "Новое название",
            "filters": current["filters"],
            "enabled_sources": current["enabled_sources"],
        },
    )
    assert response.status_code == 200
    assert client.get("/api/vehicles", params={"search_id": search}).json()["total"] == 2
