import asyncio
import copy
import io
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.domain.filtering import evaluate
from app.domain.models import UnifiedSearchFilters
from app.sources.base import SourceFailure, SourcePage
from app.sources.drom import parser
from app.sources.drom.adapter import DromSourceAdapter, search_url
from app.sources.drom.normalizer import normalize
from app.sources.images import allowed_image, sanitize
from app.sources.transport import PublicTransport, _next_request, retry_seconds

FIXTURES = Path(__file__).parent / "fixtures" / "drom"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf8")


def test_drom_search_detail_normalization():
    page = parser.search(fixture("search.html"), 1)
    assert len(page.items) == 1 and page.next_page == 2
    raw = page.items[0]
    raw.update(parser.detail(fixture("detail.html"), "800000001"))
    car = normalize(raw, datetime.now(UTC))
    assert car.year == 2009 and car.mileage_km == 200000 and car.price == 1550000
    assert car.engine_volume == 4.8 or str(car.engine_volume) == "4.8"
    assert car.power_hp == 500 and car.transmission == "robot" and car.drive_type == "all"
    assert car.owners_count is None and "owners_count" in car.field_presence
    assert evaluate(UnifiedSearchFilters(owners_max=5), car)[0] == "unverified"
    raw["specs"]["владельцы"] = "2"
    assert evaluate(UnifiedSearchFilters(owners_max=5), normalize(raw, datetime.now(UTC)))[0] == "confirmed"


def test_empty_differs_from_schema_change():
    assert parser.search(fixture("empty.html"), 1).items == []
    for html in ("<html></html>", fixture("search.html").replace('"@type": "Car"', '"@type": "Boat"')):
        with pytest.raises(SourceFailure, match="PARSER_ERROR"):
            parser.search(html, 1)


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Подтвердите, что вы человек", "SOURCE_UNAVAILABLE"),
        ("Вход на сайт", "AUTH_REQUIRED"),
        ("Доступ ограничен", "SOURCE_UNAVAILABLE"),
    ],
)
def test_access_pages_are_not_empty(title, expected):
    with pytest.raises(SourceFailure, match=expected):
        parser.search(f"<title>{title}</title>", 1)


def test_unknowns_currency_and_explicit_removal():
    raw = parser.search(fixture("search.html"), 1).items[0]
    raw["car"]["offers"].pop("price")
    raw["car"]["mileageFromOdometer"]["unitCode"] = "SMI"
    car = normalize(raw, datetime.now(UTC))
    assert car.price is None and car.mileage_km is None and car.owners_count is None
    raw = parser.detail(fixture("detail.html").replace("/InStock", "/SoldOut"), "800000001")
    assert normalize(raw, datetime.now(UTC)).status == "REMOVED"
    raw["car"]["offers"]["availability"] = "https://schema.org/OutOfStock"
    assert normalize(raw, datetime.now(UTC)).status == "REMOVED"
    raw.pop("detail")
    assert normalize(raw, datetime.now(UTC)).status == "UNKNOWN"
    with pytest.raises(SourceFailure):
        parser.detail(fixture("detail.html"), "999999999")


def test_filter_url_scope_and_pagination():
    filters = UnifiedSearchFilters(make="porsche", model="panamera", region="москва", price_to="1900000")
    assert search_url(filters, 2) == "https://auto.drom.ru/moscow/porsche/panamera/page2/?maxprice=1900000"
    for changes, error in [
        ({"region": "неизвестный город"}, "UNSUPPORTED_REGION"),
        ({"model": "../foo"}, "UNSUPPORTED_FILTER"),
    ]:
        with pytest.raises(SourceFailure, match=error):
            search_url(filters.model_copy(update=changes), 1)
    with pytest.raises(SourceFailure, match="SEARCH_SCOPE_REQUIRED"):
        search_url(UnifiedSearchFilters(), 1)


@pytest.mark.parametrize(
    "status,headers,expected",
    [
        (429, {"Retry-After": "120"}, "RATE_LIMITED"),
        (503, {}, "SOURCE_UNAVAILABLE"),
        (403, {}, "SOURCE_UNAVAILABLE"),
        (401, {}, "AUTH_REQUIRED"),
        (302, {"Location": "https://auth.auto.ru/login/"}, "AUTH_REQUIRED"),
        (404, {}, "SOURCE_UNAVAILABLE"),
    ],
)
def test_transport_statuses_no_redirects_or_retries(status, headers, expected):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers=headers)

    async def run():
        _next_request.clear()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(SourceFailure, match=expected):
                await PublicTransport("auto.drom.ru", client, interval=0).get(
                    "https://auto.drom.ru/porsche/panamera/"
                )

    asyncio.run(run())
    assert len(calls) == 1
    _next_request.clear()


def test_transport_size_charset_and_host_validation():
    async def run():
        _next_request.clear()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    content="Привет".encode("cp1251"),
                    headers={"Content-Type": "text/html; charset=windows-1251"},
                )
            )
        ) as client:
            transport = PublicTransport("auto.drom.ru", client, interval=0)
            assert await transport.get("https://auto.drom.ru/") == "Привет"
            for url in (
                "http://auto.drom.ru/",
                "https://127.0.0.1/",
                "https://auto.drom.ru@localhost/",
                "https://auto.drom.ru:444/",
            ):
                with pytest.raises(SourceFailure, match="INVALID_SOURCE_URL"):
                    await transport.get(url)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"x" * 2000001, headers={"Content-Type": "text/html"})
            )
        ) as client:
            with pytest.raises(SourceFailure, match="RESPONSE_TOO_LARGE"):
                await PublicTransport("auto.drom.ru", client, interval=0).get("https://auto.drom.ru/")

    asyncio.run(run())
    assert retry_seconds("120") == 120 and retry_seconds("invalid") == 600


def test_images_allowlist_and_safe_reencoding():
    assert allowed_image("drom", "https://s31.auto.drom.ru/photo/v2/a.jpg")
    for url in (
        "https://localhost/photo/a.jpg",
        "https://s31.auto.drom.ru.evil.test/photo/a.jpg",
        "https://s31.auto.drom.ru/photo/a.svg",
        "https://s31.auto.drom.ru/photo/a.jpg?url=http://localhost",
    ):
        assert not allowed_image("drom", url)
    data = io.BytesIO()
    Image.new("RGB", (1200, 900)).save(data, "PNG")
    with Image.open(io.BytesIO(sanitize(data.getvalue()))) as image:
        assert image.format == "JPEG" and image.width <= 1000 and not image.getexif()
    with pytest.raises(SourceFailure):
        sanitize(b"<svg/>")


def test_worker_keeps_first_page_when_second_fails(monkeypatch):
    from app.worker import runner

    raw = parser.search(fixture("search.html"), 1).items[0]

    class Adapter(DromSourceAdapter):
        async def search(self, filters, page=1):
            if page == 2:
                raise SourceFailure("RATE_LIMITED")
            return SourcePage(items=[copy.deepcopy(raw)], next_page=2)

    monkeypatch.setattr(runner, "adapter_for", lambda _: Adapter())
    records, warnings = asyncio.run(runner.fetch("drom", {}))
    assert len(records) == 1 and "PARTIAL_RATE_LIMITED" in warnings
