import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa
from test_core import SEARCH, create, drain
from test_monitoring import due

from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.domain.models import NormalizedListing, UnifiedSearchFilters
from app.services import monitoring
from app.sources.base import SourceFailure, SourcePage
from app.sources.drom.adapter import search_url
from app.sources.mock.adapter import MockSourceAdapter
from app.worker import runner


class Pages(MockSourceAdapter):
    pages = []
    total = 8

    def capabilities(self):
        return super().capabilities().model_copy(update={"max_pages": 3, "detail": False})

    async def search(self, filters, page=1):
        self.pages.append(page)
        raw = self.rows()[0] | {"source_listing_id": f"car-{page}"}
        return SourcePage(items=[raw], next_page=page + 1 if page < self.total else None)


def test_cursor_continues_after_restart_and_resets_for_new_filters(client, monkeypatch):
    adapter = Pages()
    adapter.pages = []
    monkeypatch.setattr(runner, "adapter_for", lambda _: adapter)
    ids = create(client)
    assert adapter.pages == [1, 2, 3]
    run = client.get("/api/search-runs/" + ids["run_id"]).json()["sources"][0]
    assert run["pages_checked"] == [1, 2, 3] and not run["catalog_complete"]
    for expected in ([1, 4, 5], [1, 6, 7], [1, 8]):
        adapter.pages = []
        settings.cache_clear()
        due()
        monitoring.schedule_due()
        drain()
        assert adapter.pages == expected
    with engine().connect() as conn:
        assert conn.execute(sa.select(t.source_states.c.scan_next_page)).scalar_one() == 2
        assert conn.execute(sa.select(sa.func.count()).select_from(t.listings)).scalar_one() == 8
    with engine().begin() as conn:
        conn.execute(t.source_states.update().values(scan_next_page=7))
        conn.execute(t.searches.update().values(filters_version=t.searches.c.filters_version + 1))
    adapter.pages = []
    due()
    monitoring.schedule_due()
    drain()
    assert adapter.pages == [1, 2, 3]


def test_partial_page_retry_position_and_empty_head(client, monkeypatch):
    class Failure(Pages):
        async def search(self, filters, page=1):
            if page == 3:
                raise SourceFailure("RATE_LIMITED")
            return await super().search(filters, page)

    monkeypatch.setattr(runner, "adapter_for", lambda _: Failure())
    progress = {}
    records, warnings = asyncio.run(runner.fetch("mock", {}, progress))
    assert len(records) == 2 and progress["scan_next_page"] == 3
    assert warnings == ["PARTIAL_RATE_LIMITED"] and not progress["catalog_complete"]

    class Empty(Pages):
        async def search(self, filters, page=1):
            return SourcePage(items=[])

    monkeypatch.setattr(runner, "adapter_for", lambda _: Empty())
    progress = {"scan_next_page": 25}
    asyncio.run(runner.fetch("mock", {}, progress))
    assert progress["catalog_complete"] and progress["scan_next_page"] == 2


def test_repeated_page_never_claims_full_coverage(client, monkeypatch):
    class Repeated(Pages):
        async def search(self, filters, page=1):
            return SourcePage(items=self.rows()[:1], next_page=page + 1)

    monkeypatch.setattr(runner, "adapter_for", lambda _: Repeated())
    progress = {}
    records, warnings = asyncio.run(runner.fetch("mock", {}, progress))
    assert len(records) == 1 and warnings == ["REPEATED_PAGE"]
    assert not progress["catalog_complete"] and progress["scan_next_page"] == 2


class LiveShape(MockSourceAdapter):
    source = "drom"
    checked = []
    failure = False
    total = 8

    def rows(self):
        base = MockSourceAdapter().rows()[0]
        return [
            base | {"source_listing_id": str(800000001 + i), "images": [], "main_image_url": None}
            for i in range(self.total)
        ]

    def normalize(self, raw, observed_at):
        return NormalizedListing.model_validate(
            raw
            | {
                "source": "drom",
                "source_url": "https://auto.drom.ru/porsche/panamera/" + raw["source_listing_id"] + ".html",
                "observed_at": observed_at,
                "field_presence": set(raw),
                "images": [],
                "main_image_url": None,
            }
        )

    async def search(self, filters, page=1):
        return SourcePage(items=self.rows())

    async def get_listing(self, url):
        identifier = url.rsplit("/", 1)[1].split(".")[0]
        self.checked.append(identifier)
        if self.failure:
            raise SourceFailure("PARSER_ERROR")
        return next(row for row in self.rows() if row["source_listing_id"] == identifier)


def test_favourites_priority_fairness_and_actual_check_time(client, monkeypatch):
    adapter = LiveShape()
    adapter.checked = []
    monkeypatch.setattr(runner, "adapter_for", lambda _: adapter)
    # Initially discover without enrichment so all candidates have equal age.
    cap = adapter.capabilities
    monkeypatch.setattr(adapter, "capabilities", lambda: cap().model_copy(update={"detail": False}))
    ids = create(client, enabled_sources=["drom"])
    with engine().begin() as conn:
        all_ids = conn.execute(sa.select(t.listings.c.id, t.listings.c.source_listing_id)).all()
        favorite_ids = [row.id for row in all_ids if int(row.source_listing_id) >= 800000005]
        conn.execute(
            t.clusters.update()
            .where(
                t.clusters.c.id.in_(
                    sa.select(t.memberships.c.cluster_id).where(t.memberships.c.listing_id.in_(favorite_ids))
                )
            )
            .values(favourite=True)
        )
    monkeypatch.setattr(adapter, "capabilities", cap)
    due()
    monkeypatch.setattr(monitoring, "source_enabled", lambda _: True)
    monitoring.schedule_due()
    drain()
    assert len(adapter.checked) == 6
    assert all(int(value) >= 800000005 for value in adapter.checked[:3])
    assert all(int(value) < 800000005 for value in adapter.checked[3:])
    response = client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()
    assert sum(bool(row["detail_checked_at"]) for row in response["items"]) == 6
    # Immediate next pass checks only the remaining cards, not the same six again.
    adapter.checked = []
    due()
    monitoring.schedule_due()
    drain()
    assert len(adapter.checked) == 2
    with engine().connect() as conn:
        before = {row.id: row.detail_checked_at for row in conn.execute(sa.select(t.listings))}
    # A failed detail check updates attempts but never manufactures successful verification.
    adapter.failure = True
    with engine().begin() as conn:
        conn.execute(t.listings.update().values(detail_attempted_at=t.now() - timedelta(hours=3)))
    due()
    monitoring.schedule_due()
    drain()
    with engine().connect() as conn:
        rows = conn.execute(sa.select(t.listings)).all()
        assert {row.id: row.detail_checked_at for row in rows} == before
        assert all(row.status == "ACTIVE" for row in rows)
        assert any(row.detail_attempted_at > row.detail_checked_at for row in rows)


def test_drom_years_extended_city_and_validation(client, monkeypatch):
    url = search_url(
        UnifiedSearchFilters(make="porsche", model="panamera", region="казань", year_from=2010, year_to=2015),
        8,
    )
    assert "/kazan/porsche/panamera/page8/" in url
    assert "minyear=2010" in url and "maxyear=2015" in url
    with pytest.raises(SourceFailure, match="PAGE_LIMIT"):
        search_url(UnifiedSearchFilters(make="porsche"), 51)
    cities = client.get("/api/regions").json()["items"]
    assert len(cities) == 15
    assert next(c for c in cities if c["id"] == "казань")["sources"] == ["drom"]
    monkeypatch.setenv("TEST_FIXTURES_ENABLED", "false")
    monkeypatch.setenv("AUTO_RU_ENABLED", "true")
    settings.cache_clear()
    body = SEARCH | {"enabled_sources": ["auto_ru"], "filters": {"make": "porsche", "region": "казань"}}
    assert client.post("/api/searches", json=body).status_code == 422
