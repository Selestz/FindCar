from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.matching.config import MatchingConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    app_origin: str = "http://localhost:8080"
    cookie_secure: bool = False
    session_hours: int = Field(default=24, ge=1, le=168)
    manual_refresh_cooldown: int = Field(default=300, ge=0)
    mock_scenario: str = "normal"
    drom_enabled: bool = False
    auto_ru_enabled: bool = False
    worker_poll_seconds: float = Field(default=1, ge=0.1)
    job_timeout_seconds: int = Field(default=30, ge=1, le=180)
    scheduler_enabled: bool = True
    scheduler_jitter_seconds: int = Field(default=120, ge=0, le=300)
    source_request_interval_seconds: float = Field(default=2, ge=1.5, le=10)
    source_requests_per_hour: int = Field(default=120, ge=10, le=1000)
    retry_base_seconds: int = Field(default=60, ge=1, le=600)
    max_job_attempts: int = Field(default=3, ge=1, le=5)
    known_detail_limit: int = Field(default=3, ge=1, le=10)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)


@lru_cache
def settings() -> Settings:
    return Settings()
