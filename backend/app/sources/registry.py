from app.config import settings
from app.domain.models import Source
from app.sources.auto_ru.adapter import AutoRuSourceAdapter
from app.sources.base import CarSourceAdapter, SourceFailure
from app.sources.drom.adapter import DromSourceAdapter
from app.sources.mock.adapter import MockSourceAdapter


def source_enabled(source: str) -> bool:
    return (
        source == Source.MOCK
        or (source == Source.DROM and settings().drom_enabled)
        or (source == Source.AUTO_RU and settings().auto_ru_enabled)
    )


def adapter_for(source: str) -> CarSourceAdapter:
    if source == Source.MOCK:
        return MockSourceAdapter(settings().mock_scenario)
    if source == Source.DROM and source_enabled(source):
        return DromSourceAdapter()
    if source == Source.AUTO_RU and source_enabled(source):
        return AutoRuSourceAdapter()
    raise SourceFailure("ACCESS_NOT_CONFIGURED")
