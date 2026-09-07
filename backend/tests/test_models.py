import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.filtering import evaluate
from app.domain.models import UnifiedSearchFilters
from app.sources.base import SourceFailure
from app.sources.mock.adapter import MockSourceAdapter

NOW = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"year_from": 2020, "year_to": 2010},
        {"price_from": 200, "price_to": 100},
        {"model": "panamera"},
        {"radius_km": 50},
        {"body_types": ["fake"]},
        {"year_to": 3000},
        {"price_to": "NaN"},
        {"price_to": -1},
        {"oops": 1},
    ],
)
def test_invalid_filters(kwargs):
    with pytest.raises(ValidationError):
        UnifiedSearchFilters(**kwargs)


def test_mock_normalization_and_unknown_filter():
    adapter = MockSourceAdapter()
    raw = adapter.rows()[2]
    normalized = adapter.normalize(raw, NOW)
    assert normalized.observed_at == NOW
    assert normalized.owners_count is None
    assert normalized.source == "mock"
    filters = UnifiedSearchFilters(make=" PORSCHE ", model="Panamera", price_to="1900000", owners_max=5)
    assert evaluate(filters, normalized) == ("unverified", ["owners_max"])
    assert evaluate(filters, adapter.normalize(adapter.rows()[0], NOW)) == ("confirmed", [])
    assert evaluate(filters, adapter.normalize(adapter.rows()[3], NOW))[0] == "not_matching"


def test_missing_does_not_equal_zero_and_currency_not_converted():
    adapter = MockSourceAdapter()
    raw = adapter.rows()[0] | {"mileage_km": None, "currency": "USD"}
    listing = adapter.normalize(raw, NOW)
    state, unknown = evaluate(UnifiedSearchFilters(mileage_to=150000, price_to=1900000), listing)
    assert state == "unverified"
    assert set(unknown) == {"price_to", "mileage_to"}


def test_adapter_has_pagination_and_explicit_empty():
    adapter = MockSourceAdapter()
    assert asyncio.run(adapter.search(UnifiedSearchFilters())).next_page == 2
    assert asyncio.run(adapter.search(UnifiedSearchFilters(), 2)).next_page is None
    assert asyncio.run(MockSourceAdapter("empty").search(UnifiedSearchFilters())).items == []
    with pytest.raises(SourceFailure, match="PARSER_ERROR"):
        asyncio.run(MockSourceAdapter("parser_error").search(UnifiedSearchFilters()))


def test_url_and_contacts():
    adapter = MockSourceAdapter()
    normalized = adapter.normalize(
        adapter.rows()[0] | {"description": "Пишите test@example.com, +7 (900) 123-45-67"}, NOW
    )
    assert "example.com" not in normalized.description
    assert "123-45" not in normalized.description
    with pytest.raises(ValidationError):
        type(normalized).model_validate(normalized.model_dump() | {"source_url": "http://127.0.0.1/private"})
