import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_core import SEARCH, count, create, drain

from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.main import app
from app.services import monitoring
from app.services.searches import enqueue, owned
from app.sources.base import SourceFailure, SourcePage
from app.sources.mock.adapter import MockSourceAdapter
from app.sources.transport import PublicTransport, _next_request
from app.worker import runner


def due():
    with engine().begin() as conn:
        conn.execute(t.searches.update().values(next_refresh_at=t.now() - timedelta(seconds=1)))


def release_waits():
    # Only the isolated test database: move persisted delays without sleeping.
    with engine().begin() as conn:
        conn.execute(t.jobs.update().values(not_before=t.now() - timedelta(seconds=1)))
        conn.execute(t.source_limits.update().values(cooldown_until=None))


def test_scheduler_concurrency_manual_race_and_restart(client, db):
    ids = create(client)
    due()
    before = count(t.runs)

    def manual():
        with engine().begin() as conn:
            enqueue(conn, owned(conn, uuid.UUID(ids["search_id"]), db["alice"], lock=True))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(monitoring.schedule_due),
            pool.submit(monitoring.schedule_due),
            pool.submit(manual),
        ]
        for f in futures:
            f.result(timeout=10)
    assert count(t.runs) == before + 1
    assert monitoring.schedule_due() == 0
    drain()
    search = client.get("/api/searches/" + ids["search_id"]).json()
    with engine().connect() as conn:
        row = conn.execute(sa.select(t.searches)).mappings().one()
        seconds = (row["next_refresh_at"] - row["last_checked_at"]).total_seconds()
    assert 1500 <= seconds <= 1621
    assert search["enabled"]
    assert count(t.events) == 3


def test_pause_interval_disabled_user_and_access(client, db):
    ids = create(client)
    url = f"/api/searches/{ids['search_id']}/monitoring"
    assert client.patch(url, json={"enabled": False, "refresh_interval_seconds": 1800}).status_code == 200
    due()
    assert monitoring.schedule_due() == 0
    assert client.patch(url, json={"enabled": True, "refresh_interval_seconds": 10}).status_code == 422
    assert client.patch(url, json={"enabled": True, "refresh_interval_seconds": 1800}).status_code == 200
    with TestClient(app, headers={"Origin": "http://testserver"}) as bob:
        login = bob.post("/api/auth/login", json={"username": "bob", "password": "test-password-bob"})
        bob.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert bob.patch(url, json={"enabled": False, "refresh_interval_seconds": 1800}).status_code == 404
    due()
    with engine().begin() as conn:
        conn.execute(t.users.update().where(t.users.c.id == db["alice"]).values(enabled=False))
    assert monitoring.schedule_due() == 0


def test_global_source_concurrency_and_expired_lease(client):
    client.post("/api/searches", json=SEARCH)
    client.post("/api/searches", json=SEARCH | {"name": "second"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: runner.claim(), range(2)))
    claimed = [job for job in jobs if job]
    assert len(claimed) == 1
    with engine().begin() as conn:
        conn.execute(
            t.jobs.update()
            .where(t.jobs.c.id == claimed[0]["id"])
            .values(lease_until=t.now() - timedelta(seconds=1))
        )
    drain()
    assert count(t.runs) == 2 and count(t.clusters) == 3


def test_retry_backoff_persists_and_recovers(client, monkeypatch):
    monkeypatch.setenv("MOCK_SCENARIO", "unavailable")
    settings.cache_clear()
    ids = client.post("/api/searches", json=SEARCH).json()
    assert asyncio.run(runner.run_once())
    with engine().connect() as conn:
        job = conn.execute(sa.select(t.jobs)).mappings().one()
        pause = conn.execute(sa.select(t.source_limits)).mappings().one()
    assert job["state"] == "queued" and job["attempt"] == 1
    assert (job["not_before"] - t.now()).total_seconds() > 50
    assert pause["cooldown_until"] >= job["not_before"]
    assert runner.claim() is None
    settings.cache_clear()  # A new process reads the same persisted gate.
    assert runner.claim() is None
    release_waits()
    monkeypatch.setenv("MOCK_SCENARIO", "normal")
    settings.cache_clear()
    drain()
    assert client.get("/api/search-runs/" + ids["run_id"]).json()["outcome"] == "complete"
    assert count(t.events) == 3


def test_retry_exhaustion_and_partial_data_are_preserved(client, monkeypatch):
    class Partial(MockSourceAdapter):
        async def search(self, filters, page=1):
            if page == 2:
                raise SourceFailure("SOURCE_UNAVAILABLE")
            return await super().search(filters, page)

    monkeypatch.setattr(runner, "adapter_for", lambda _: Partial())
    ids = client.post("/api/searches", json=SEARCH).json()
    for attempt in range(3):
        release_waits()
        assert asyncio.run(runner.run_once())
    run = client.get("/api/search-runs/" + ids["run_id"]).json()
    assert run["outcome"] == "partial" and run["sources"][0]["attempt"] == 3
    assert count(t.listings) == 2 and count(t.events) == 2 and count(t.snapshots) == 2
    release_waits()
    assert not asyncio.run(runner.run_once())


def test_request_budget_and_retry_after_survive_process_state(db, monkeypatch):
    monkeypatch.setenv("SOURCE_REQUESTS_PER_HOUR", "10")
    settings.cache_clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: monitoring.reserve_request("drom"), range(10)))
    with pytest.raises(SourceFailure, match="RATE_LIMITED"):
        monitoring.reserve_request("drom")

    async def limited():
        _next_request.clear()
        token = monitoring.request_source.set("auto_ru")
        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "3600"}))
            ) as client:
                await PublicTransport("auto.ru", client, interval=0).get("https://auto.ru/catalog/")
        finally:
            monitoring.request_source.reset(token)

    with pytest.raises(SourceFailure, match="RATE_LIMITED"):
        asyncio.run(limited())
    _next_request.clear()
    with pytest.raises(SourceFailure, match="RATE_LIMITED"):
        monitoring.reserve_request("auto_ru")
    with engine().connect() as conn:
        row = (
            conn.execute(sa.select(t.source_limits).where(t.source_limits.c.source == "auto_ru"))
            .mappings()
            .one()
        )
    assert (row["cooldown_until"] - t.now()).total_seconds() > 3500


def test_known_detail_removal_return_price_and_sparse_fields(client, monkeypatch):
    ids = create(client)

    class Missing(MockSourceAdapter):
        phase = "removed"

        async def search(self, filters, page=1):
            return SourcePage(items=[])

        async def get_listing(self, listing_id):
            raw = await super().get_listing(listing_id)
            if listing_id == self.rows()[0]["source_listing_id"]:
                raw = {k: v for k, v in raw.items() if k not in {"region", "owners_count"}}
                raw.update(status="REMOVED" if self.phase == "removed" else "ACTIVE", price="1700000")
            return raw

    adapter = Missing()
    monkeypatch.setattr(runner, "adapter_for", lambda _: adapter)
    for phase in ("removed", "returned", "returned"):
        adapter.phase = phase
        due()
        assert monitoring.schedule_due() == 1
        drain()
    events = client.get("/api/events").json()["items"]
    for kind in ("PRICE_DROP", "LISTING_REMOVED", "LISTING_RETURNED"):
        assert sum(e["type"] == kind for e in events) == 1
    assert client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()["total"] == 2
    assert count(t.snapshots) == 6


def test_ingestion_crash_rolls_back_then_reclaims(client, monkeypatch):
    ids = client.post("/api/searches", json=SEARCH).json()
    original = runner.ingest

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(runner, "ingest", crash)
    with pytest.raises(RuntimeError):
        asyncio.run(runner.run_once())
    assert count(t.listings) == 0 and count(t.events) == 0
    with engine().begin() as conn:
        conn.execute(t.jobs.update().values(lease_until=t.now() - timedelta(seconds=1)))
    monkeypatch.setattr(runner, "ingest", original)
    drain()
    assert client.get("/api/search-runs/" + ids["run_id"]).json()["outcome"] == "complete"
    assert count(t.events) == 3


def test_stale_worker_cannot_commit(client, monkeypatch):
    client.post("/api/searches", json=SEARCH)
    original = runner.collect

    async def stolen(*args):
        result = await original(*args)
        with engine().begin() as conn:
            conn.execute(t.jobs.update().values(lease_token=uuid.uuid4()))
        return result

    monkeypatch.setattr(runner, "collect", stolen)
    assert asyncio.run(runner.run_once())
    assert count(t.listings) == 0 and count(t.events) == 0


def test_scheduled_relisting_keeps_history_and_personal_state(client, monkeypatch):
    class Relist(MockSourceAdapter):
        phase = 0

        def rows(self):
            raw = super().rows()[0] | {"images": [f"mock://panamera/{i}" for i in range(5)]}
            if self.phase == 0:
                return [raw | {"source_listing_id": "a-old", "status": "REMOVED"}]
            return [raw | {"source_listing_id": "b-new", "status": "ACTIVE", "price": "1690000"}]

        async def get_listing(self, listing_id):
            if listing_id == "a-old":
                raw = MockSourceAdapter().rows()[0]
                return raw | {"source_listing_id": "a-old", "status": "REMOVED"}
            return await super().get_listing(listing_id)

    adapter = Relist()
    monkeypatch.setattr(runner, "adapter_for", lambda _: adapter)
    ids = create(client)
    with engine().begin() as conn:
        conn.execute(t.clusters.update().values(favourite=True, notes=[{"text": "inspect engine"}]))
        # Model a previously observed removed advertisement.
        conn.execute(t.listings.update().values(first_seen_at=t.now() - timedelta(days=30)))
    adapter.phase = 1
    for _ in range(2):
        due()
        assert monitoring.schedule_due() == 1
        drain()
    assert count(t.relist_links) == 1
    events = client.get("/api/events").json()["items"]
    assert sum(e["type"] == "PROBABLE_RELIST" for e in events) == 1
    result = client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()
    assert result["total"] == 1 and result["items"][0]["favourite"]
    detail = client.get("/api/vehicles/" + result["items"][0]["cluster_id"]).json()
    assert detail["notes"] and len(detail["listings"]) == 2


def test_detail_timeout_keeps_already_fetched_records(client, monkeypatch):
    from app.domain.models import UnifiedSearchFilters

    adapter = MockSourceAdapter()

    async def timeout(job, run, progress):
        page = await adapter.search(UnifiedSearchFilters())
        progress.update(
            records=[adapter.normalize(raw, t.now()) for raw in page.items],
            warnings=[],
            prepared={},
            attempted=[],
        )
        raise TimeoutError

    monkeypatch.setattr(runner, "collect_job", timeout)
    monkeypatch.setenv("MAX_JOB_ATTEMPTS", "1")
    settings.cache_clear()
    ids = client.post("/api/searches", json=SEARCH).json()
    drain()
    assert client.get("/api/search-runs/" + ids["run_id"]).json()["outcome"] == "partial"
    assert count(t.listings) == 2


def test_missing_detail_error_does_not_mark_removed(client, monkeypatch):
    ids = create(client)

    class Unreachable(MockSourceAdapter):
        async def search(self, filters, page=1):
            return SourcePage(items=[])

        async def get_listing(self, listing_id):
            raise SourceFailure("SOURCE_UNAVAILABLE")

    monkeypatch.setattr(runner, "adapter_for", lambda _: Unreachable())
    monkeypatch.setenv("MAX_JOB_ATTEMPTS", "1")
    settings.cache_clear()
    due()
    monitoring.schedule_due()
    drain()
    assert count(t.snapshots) == 4
    assert client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()["total"] == 2
    assert not any(e["type"] == "LISTING_REMOVED" for e in client.get("/api/events").json()["items"])


def test_known_card_still_in_catalog_is_checked_without_false_return_events(client, monkeypatch):
    ids = create(client)

    class StaleCatalog(MockSourceAdapter):
        async def get_listing(self, listing_id):
            raw = await super().get_listing(listing_id)
            return raw | {"status": "REMOVED"}

    monkeypatch.setattr(runner, "adapter_for", lambda _: StaleCatalog())
    for _ in range(2):
        due()
        monitoring.schedule_due()
        drain()
    events = client.get("/api/events").json()["items"]
    assert sum(e["type"] == "LISTING_REMOVED" for e in events) == 3
    assert not any(e["type"] == "LISTING_RETURNED" for e in events)
    assert client.get("/api/searches/" + ids["search_id"]).json()["enabled"]
