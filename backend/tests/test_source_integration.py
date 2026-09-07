import asyncio
import uuid

from test_core import create
from test_sources import fixture

from app.sources.base import SourceFailure
from app.sources.drom.adapter import DromSourceAdapter
from app.worker import runner


class FixtureTransport:
    async def get(self, url):
        if "page2" in url:
            raise SourceFailure("SOURCE_UNAVAILABLE")
        return fixture("detail.html" if url.endswith(".html") else "search.html")


def test_real_adapter_ingestion_partial_health_and_owned_thumbnail(client, db, monkeypatch):
    from app.config import settings

    monkeypatch.setenv("MAX_JOB_ATTEMPTS", "1")
    settings.cache_clear()
    adapter = DromSourceAdapter(FixtureTransport())
    monkeypatch.setattr(runner, "adapter_for", lambda _: adapter)
    ids = create(client, filters={"make": "porsche", "model": "panamera"}, enabled_sources=["drom"])
    run = client.get("/api/search-runs/" + ids["run_id"]).json()
    assert run["outcome"] == "partial"
    assert run["sources"][0]["error_code"] == "SOURCE_UNAVAILABLE"
    result = client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()
    assert result["total"] == 1
    car = result["items"][0]
    assert car["source"] == "drom" and car["owners_count"] is None
    monkeypatch.setattr("app.sources.images.thumbnail", lambda *args: b"test-image")
    path = "/api/listings/" + car["id"] + "/image/0"
    image_response = client.get(path)
    assert image_response.content == b"test-image", (image_response.text, car)
    assert client.get("/api/listings/" + str(uuid.uuid4()) + "/image/0").status_code == 404
    assert client.get(path[:-1] + "6").status_code == 404
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "bob", "password": "test-password-bob"})
    assert client.get(path).status_code == 404


def test_source_selection_change_invalidates_old_search_results(client):
    ids = create(client)
    current = client.get("/api/searches/" + ids["search_id"]).json()
    response = client.put(
        "/api/searches/" + ids["search_id"],
        json={"name": current["name"], "filters": current["filters"], "enabled_sources": ["drom"]},
    )
    assert response.status_code == 200
    assert client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()["total"] == 0


def test_photo_processing_does_not_generate_synthetic_live_fingerprints(monkeypatch):
    from datetime import UTC, datetime

    from app.sources.drom import parser
    from app.sources.drom.normalizer import normalize

    record = normalize(parser.search(fixture("search.html"), 1).items[0], datetime.now(UTC))

    async def fetch(*args):
        return [record], []

    monkeypatch.setattr(runner, "fetch", fetch)
    monkeypatch.setattr(
        runner,
        "prepare_images",
        lambda *args: (_ for _ in ()).throw(AssertionError("live images must not become synthetic")),
    )
    _, _, prepared = asyncio.run(runner.collect("drom", {}))
    assert prepared == {}
