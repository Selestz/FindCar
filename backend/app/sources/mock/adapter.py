import json
import re
from datetime import datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any

from app.domain.models import NormalizedListing, Source, UnifiedSearchFilters
from app.sources.base import CarSourceAdapter, SourceCapabilities, SourceFailure, SourcePage


class MockSourceAdapter(CarSourceAdapter):
    source = Source.MOCK

    def __init__(self, scenario: str = "normal") -> None:
        self.scenario = scenario

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = json.loads(
            files("app.sources.mock").joinpath("listings.json").read_text("utf-8")
        )
        if self.scenario == "price_drop":
            rows[0]["price"] = str(Decimal(rows[0]["price"]) - 100000)
        if self.scenario == "removed":
            rows[0]["status"] = "REMOVED"
        if self.scenario == "dedup":
            rows[0]["images"] = [f"mock://panamera/{i}" for i in range(5)]
            rows.append(
                rows[0]
                | {"source_listing_id": "panamera-duplicate", "mileage_km": rows[0]["mileage_km"] + 1000}
            )
            rows.append(
                rows[1] | {"source_listing_id": "panamera-review", "mileage_km": rows[1]["mileage_km"] + 500}
            )
        return rows

    async def search(self, filters: UnifiedSearchFilters, page: int = 1) -> SourcePage:
        if self.scenario in {"unavailable", "rate_limited", "auth_required", "parser_error"}:
            raise SourceFailure(
                {
                    "unavailable": "SOURCE_UNAVAILABLE",
                    "rate_limited": "RATE_LIMITED",
                    "auth_required": "AUTH_REQUIRED",
                    "parser_error": "PARSER_ERROR",
                }[self.scenario]
            )
        if self.scenario == "empty":
            return SourcePage(items=[])
        if page < 1:
            raise SourceFailure("PARSER_ERROR")
        # Intentionally leaves filtering to the shared three-valued evaluator.
        rows = self.rows()
        start = (page - 1) * 2
        return SourcePage(
            items=rows[start : start + 2], next_page=page + 1 if start + 2 < len(rows) else None
        )

    async def get_listing(self, listing_id: str) -> dict[str, Any]:
        for row in self.rows():
            if row["source_listing_id"] == listing_id:
                return row
        raise SourceFailure("SOURCE_UNAVAILABLE")

    def normalize(self, raw: dict[str, Any], observed_at: datetime) -> NormalizedListing:
        payload = dict(raw)
        if payload.get("description"):
            payload["description"] = re.sub(
                r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+?\d[\d ()-]{8,}\d",
                "[контакт удалён]",
                payload["description"],
            )
        payload.update(
            source=self.source,
            source_url=f"https://example.invalid/listings/{raw['source_listing_id']}",
            observed_at=observed_at,
            field_presence=set(payload),
        )
        return NormalizedListing.model_validate(payload)

    async def health_check(self) -> str:
        return (
            "OK"
            if self.scenario in {"normal", "price_drop", "removed", "empty", "dedup"}
            else "SOURCE_UNAVAILABLE"
        )

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            parser_version="mock-json-1",
            filters={
                name: "unsupported" if name == "radius_km" else "local"
                for name in UnifiedSearchFilters.model_fields
                if name != "schema_version"
            },
            detail=True,
        )
