from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from app.catalog import source_scope
from app.domain.models import NormalizedListing, Source, UnifiedSearchFilters
from app.sources.base import CarSourceAdapter, SourceCapabilities, SourceFailure, SourcePage
from app.sources.drom import parser
from app.sources.drom.normalizer import normalize
from app.sources.drom.transport import DromTransport

REGIONS = {"москва": "moscow", "санкт-петербург": "spb", "новосибирск": "novosibirsk"}


def search_url(filters: UnifiedSearchFilters, page: int) -> str:
    if not filters.make:
        raise SourceFailure("SEARCH_SCOPE_REQUIRED")
    make, model = source_scope("drom", filters.make, filters.model)
    region = REGIONS.get(filters.region or "")
    if filters.region and not region:
        raise SourceFailure("UNSUPPORTED_REGION")
    if page not in (1, 2):
        raise SourceFailure("PAGE_LIMIT")
    path = "/".join(([region] if region else []) + [make] + ([model] if model else []))
    query = {
        name: str(value)
        for name, value in (("minprice", filters.price_from), ("maxprice", filters.price_to))
        if value is not None
    }
    return (
        f"https://auto.drom.ru/{path}/"
        + (f"page{page}/" if page > 1 else "")
        + ("?" + urlencode(query) if query else "")
    )


class DromSourceAdapter(CarSourceAdapter):
    source = Source.DROM

    def __init__(self, transport: Any = None) -> None:
        self.transport = transport or DromTransport()
        self.detail_count = 0

    async def search(self, filters: UnifiedSearchFilters, page: int = 1) -> SourcePage:
        data = parser.search(await self.transport.get(search_url(filters, page)), page)
        # Enrich only potentially matching cards, within a hard per-run detail budget.
        from app.domain.filtering import evaluate

        for raw in data.items:
            listing = normalize(raw, datetime.now().astimezone())
            state, _ = evaluate(filters, listing)
            if state == "not_matching":
                continue
            if self.detail_count >= 3:
                data.warnings.append("DETAIL_LIMIT")
                continue
            self.detail_count += 1
            try:
                enriched = await self.get_listing(raw["source_url"])
                raw.update(enriched)
            except SourceFailure as exc:
                data.warnings.append("DETAIL_" + exc.code)
                if exc.code in {"RATE_LIMITED", "AUTH_REQUIRED", "SOURCE_UNAVAILABLE"}:
                    break
        data.warnings = sorted(set(data.warnings))
        return data

    async def get_listing(self, listing_id: str) -> dict[str, Any]:
        identifier, _, _ = parser.identity(listing_id)
        return parser.detail(await self.transport.get(listing_id), identifier)

    def normalize(self, raw: dict[str, Any], observed_at: datetime) -> NormalizedListing:
        return normalize(raw, observed_at)

    async def health_check(self) -> str:
        try:
            parser.search(await self.transport.get("https://auto.drom.ru/porsche/panamera/"), 1)
            return "OK"
        except SourceFailure as exc:
            return exc.code

    def capabilities(self) -> SourceCapabilities:
        filters = {
            name: "local_or_unverified"
            for name in UnifiedSearchFilters.model_fields
            if name != "schema_version"
        }
        filters.update({name: "remote" for name in ("make", "model", "price_from", "price_to")})
        filters.update(
            region="remote_limited_cities",
            radius_km="unverified",
            generation="unverified",
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
