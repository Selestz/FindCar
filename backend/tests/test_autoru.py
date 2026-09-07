import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.domain.models import UnifiedSearchFilters
from app.sources.auto_ru import parser
from app.sources.auto_ru.adapter import AutoRuSourceAdapter, search_url
from app.sources.auto_ru.normalizer import normalize
from app.sources.base import SourceFailure
from app.sources.transport import PublicTransport, _next_request

FIXTURES = Path(__file__).parent / "fixtures" / "auto_ru"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf8")


def test_autoru_search_and_detail_normalization():
    page = parser.search(fixture("search.html"), 1)
    assert len(page.items) == 1 and page.next_page == 2
    raw = page.items[0]
    raw.update(parser.detail(fixture("detail.html"), raw["source_listing_id"]))
    car = normalize(raw, datetime.now(UTC))
    assert car.source == "auto_ru" and car.currency == "RUB" and car.price == 8500000
    assert car.year == 2019 and car.mileage_km == 41000 and car.owners_count == 3
    assert car.power_hp == 460 and car.body_type == "wagon" and car.transmission == "robot"
    assert len(car.images) == 2


def test_autoru_missing_and_explicit_status():
    raw = parser.search(fixture("search.html"), 1).items[0]
    raw["offer"].pop("price")
    car = normalize(raw, datetime.now(UTC))
    assert car.price is None and car.owners_count is None
    raw.update(
        parser.detail(fixture("detail.html").replace("/InStock", "/SoldOut"), raw["source_listing_id"])
    )
    assert normalize(raw, datetime.now(UTC)).status == "REMOVED"
    raw["specs"]["владельцы"] = "4 и более"
    assert normalize(raw, datetime.now(UTC)).owners_count is None


def test_autoru_empty_schema_and_access():
    data = {"@type": "Product", "offers": {"offerCount": 0, "offers": []}}
    assert (
        parser.search('<script type="application/ld+json">' + json.dumps(data) + "</script>", 1).items == []
    )
    for body, code in [
        ("<title>Вход на сайт</title>", "AUTH_REQUIRED"),
        ("<title>Вы не робот?</title>", "SOURCE_UNAVAILABLE"),
        ("<html></html>", "PARSER_ERROR"),
    ]:
        with pytest.raises(SourceFailure, match=code):
            parser.search(body, 1)
    with pytest.raises(SourceFailure):
        parser.detail(fixture("detail.html"), "wrong-id")


def test_autoru_empty_catalog_does_not_import_recommendations():
    html = fixture("empty.html")
    assert parser.search(html, 1).items == []
    for broken in (
        html.replace("ListingEmptyOffers", "ChangedMarkup"),
        html.replace("ListingCarouselItem", "ListingItemUniversal"),
    ):
        with pytest.raises(SourceFailure, match="PARSER_ERROR"):
            parser.search(broken, 1)


def test_autoru_query_mapping_and_detail_budget():
    filters = UnifiedSearchFilters(
        make="porsche", model="panamera", region="москва", price_to="1900000", year_from=2010
    )
    assert (
        search_url(filters, 2)
        == "https://auto.ru/moskva/cars/porsche/panamera/used/?price_to=1900000&year_from=2010&page=2"
    )

    class Transport:
        async def get(self, url):
            return fixture("detail.html" if "/sale/" in url else "search.html")

    adapter = AutoRuSourceAdapter(Transport())
    raw = asyncio.run(adapter.search(UnifiedSearchFilters(make="porsche", model="panamera"))).items[0]
    assert normalize(raw, datetime.now(UTC)).owners_count == 3


def test_canonical_redirect_only_same_allowlisted_host():
    async def run(target):
        calls = []

        def respond(request):
            calls.append(str(request.url))
            return (
                httpx.Response(301, headers={"Location": target})
                if len(calls) == 1
                else httpx.Response(200, text="ok", headers={"Content-Type": "text/html"})
            )

        _next_request.clear()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await PublicTransport("auto.drom.ru", client, interval=0).get(
                "https://auto.drom.ru/search/"
            )
        assert len(calls) == 2
        return result

    assert asyncio.run(run("https://auto.drom.ru/canonical/")) == "ok"
    with pytest.raises(SourceFailure, match="INVALID_SOURCE_URL"):
        asyncio.run(run("https://localhost/private"))
