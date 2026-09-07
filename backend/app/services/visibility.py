from typing import Any

import sqlalchemy as sa

from app.config import settings


def visible_source(column: Any) -> Any:
    """Fixtures are accessible only in explicitly enabled isolated test environments."""
    return sa.true() if settings().test_fixtures_enabled else column != "mock"
