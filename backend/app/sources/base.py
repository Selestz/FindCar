from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.models import NormalizedListing, Source, UnifiedSearchFilters


class SourceFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SourcePage(BaseModel):
    items: list[dict[str, Any]]
    next_page: int | None = None
    coverage: str = "complete"
    warnings: list[str] = Field(default_factory=list)


class SourceCapabilities(BaseModel):
    parser_version: str = "unknown"
    filters: dict[str, str]
    detail: bool = False
    images: bool = False
    max_pages: int = 3
    max_results: int = 100


class CarSourceAdapter(ABC):
    source: Source
    enrich_details = True

    @abstractmethod
    async def search(self, filters: UnifiedSearchFilters, page: int = 1) -> SourcePage: ...

    @abstractmethod
    async def get_listing(self, listing_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def normalize(self, raw: dict[str, Any], observed_at: datetime) -> NormalizedListing: ...

    @abstractmethod
    async def health_check(self) -> str: ...

    @abstractmethod
    def capabilities(self) -> SourceCapabilities: ...
