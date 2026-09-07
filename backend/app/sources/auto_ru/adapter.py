import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from app.domain.filtering import evaluate
from app.domain.models import NormalizedListing, Source, UnifiedSearchFilters
from app.sources.auto_ru import parser
from app.sources.auto_ru.normalizer import normalize
from app.sources.auto_ru.transport import AutoRuTransport
from app.sources.base import CarSourceAdapter, SourceCapabilities, SourceFailure, SourcePage


def search_url(filters: UnifiedSearchFilters, page: int) -> str:
    if not filters.make or not filters.model:
        raise SourceFailure("SEARCH_SCOPE_REQUIRED")
    if not all(re.fullmatch(r"[a-z0-9_-]{1,80}", x) for x in (filters.make, filters.model)):
        raise SourceFailure("UNSUPPORTED_FILTER")
    if filters.region not in (None, "москва"):
        raise SourceFailure("UNSUPPORTED_REGION")
    if page not in (1, 2):
        raise SourceFailure("PAGE_LIMIT")
    query = {
        key: str(getattr(filters, key))
        for key in ("price_from", "price_to", "year_from", "year_to")
        if getattr(filters, key) is not None
    }
    if page > 1:
        query["page"] = str(page)
    return (
        f"https://auto.ru/{'moskva/' if filters.region else ''}cars/{filters.make}/{filters.model}/used/"
        + ("?" + urlencode(query) if query else "")
    )


class AutoRuSourceAdapter(CarSourceAdapter):
    source = Source.AUTO_RU

    def __init__(self, transport: Any = None) -> None:
        self.transport = transport or AutoRuTransport()
        self.detail_count = 0

    async def search(self, filters: UnifiedSearchFilters, page: int = 1) -> SourcePage:
        result = parser.search(await self.transport.get(search_url(filters, page)), page)
        for raw in result.items:
            if evaluate(filters, normalize(raw, datetime.now(UTC)))[0] == "not_matching":
                continue
            if self.detail_count >= 3:
                result.warnings.append("DETAIL_LIMIT")
                continue
            self.detail_count += 1
            try:
                raw.update(await self.get_listing(raw["source_url"]))
            except SourceFailure as exc:
                result.warnings.append("DETAIL_" + exc.code)
                if exc.code in {"RATE_LIMITED", "AUTH_REQUIRED", "SOURCE_UNAVAILABLE"}:
                    break
        result.warnings = sorted(set(result.warnings))
        return result

    async def get_listing(self, listing_id: str) -> dict[str, Any]:
        identifier, make, model = parser.identity(listing_id)
        data = parser.detail(await self.transport.get(listing_id), identifier)
        data.update(source_listing_id=identifier, source_url=listing_id, make=make, model=model)
        return data

    def normalize(self, raw: dict[str, Any], observed_at: datetime) -> NormalizedListing:
        return normalize(raw, observed_at)

    async def health_check(self) -> str:
        try:
            parser.search(await self.transport.get("https://auto.ru/cars/porsche/panamera/used/"), 1)
            return "OK"
        except SourceFailure as exc:
            return exc.code

    def capabilities(self) -> SourceCapabilities:
        filters = {
            key: "local_or_unverified" for key in UnifiedSearchFilters.model_fields if key != "schema_version"
        }
        filters.update(
            {key: "remote" for key in ("make", "model", "price_from", "price_to", "year_from", "year_to")}
        )
        filters.update(
            region="remote_moscow_only",
            generation="unverified",
            radius_km="unverified",
            owners_max="detail_exact_only",
        )
        return SourceCapabilities(
            parser_version=parser.PARSER_VERSION,
            filters=filters,
            detail=True,
            images=True,
            max_pages=2,
            max_results=40,
        )
