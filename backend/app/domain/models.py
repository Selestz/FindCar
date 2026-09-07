from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Source(StrEnum):
    MOCK = "mock"
    AVITO = "avito"
    AUTO_RU = "auto_ru"
    DROM = "drom"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UnifiedSearchFilters(Model):
    schema_version: Literal[1] = 1
    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    generation: str | None = Field(default=None, max_length=80)
    year_from: int | None = Field(default=None, ge=1886)
    year_to: int | None = Field(default=None, ge=1886)
    price_from: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    price_to: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    mileage_from: int | None = Field(default=None, ge=0)
    mileage_to: int | None = Field(default=None, ge=0)
    region: str | None = Field(default=None, max_length=100)
    radius_km: int | None = Field(default=None, ge=0, le=1000)
    body_types: list[str] = Field(default_factory=list, max_length=20)
    transmission: Literal["manual", "automatic", "robot", "cvt"] | None = None
    engine_type: Literal["petrol", "diesel", "hybrid", "electric", "gas", "other"] | None = None
    engine_volume_from: Decimal | None = Field(default=None, gt=0)
    engine_volume_to: Decimal | None = Field(default=None, gt=0)
    power_from: int | None = Field(default=None, gt=0)
    power_to: int | None = Field(default=None, gt=0)
    drive_type: Literal["front", "rear", "all"] | None = None
    steering_wheel: Literal["left", "right"] | None = None
    owners_max: int | None = Field(default=None, ge=1)

    @field_validator("make", "model", "generation", "region", mode="before")
    @classmethod
    def canonical(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().casefold() or None
        return value

    @field_validator("body_types")
    @classmethod
    def bodies(cls, values: list[str]) -> list[str]:
        allowed = {
            "sedan",
            "hatchback",
            "liftback",
            "wagon",
            "suv",
            "coupe",
            "convertible",
            "pickup",
            "minivan",
            "van",
            "other",
        }
        if not set(values) <= allowed:
            raise ValueError("Неизвестный тип кузова")
        return sorted(set(values))

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        for prefix in ("year", "price", "mileage", "engine_volume", "power"):
            lower, upper = getattr(self, prefix + "_from"), getattr(self, prefix + "_to")
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"{prefix}: нижняя граница больше верхней")
        if any(v is not None and v > datetime.now(UTC).year + 1 for v in (self.year_from, self.year_to)):
            raise ValueError("Год выходит за допустимый диапазон")
        if self.model and not self.make:
            raise ValueError("Для модели нужна марка")
        if self.generation and not self.model:
            raise ValueError("Для поколения нужна модель")
        if self.radius_km is not None and not self.region:
            raise ValueError("Для радиуса нужен регион")
        return self


class NormalizedListing(Model):
    source: Source
    source_listing_id: str = Field(min_length=1, max_length=128)
    source_url: str
    title: str = Field(min_length=1, max_length=500)
    make: str | None = None
    model: str | None = None
    generation: str | None = None
    year: int | None = Field(default=None, ge=1886)
    body_type: str | None = None
    engine_type: str | None = None
    engine_volume: Decimal | None = Field(default=None, gt=0)
    power_hp: int | None = Field(default=None, gt=0)
    transmission: str | None = None
    drive_type: str | None = None
    steering_wheel: str | None = None
    mileage_km: int | None = Field(default=None, ge=0)
    color: str | None = None
    price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    region: str | None = None
    city: str | None = None
    owners_count: int | None = Field(default=None, ge=0)
    seller_type: Literal["private", "dealer", "unknown"] = "unknown"
    description: str | None = Field(default=None, max_length=30000)
    published_at: datetime | None = None
    observed_at: datetime
    status: Literal["ACTIVE", "REMOVED", "UNKNOWN"] = "UNKNOWN"
    main_image_url: str | None = None
    images: list[str] = Field(default_factory=list, max_length=6)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    field_presence: set[str]
    parser_version: str = "1"
    normalization_version: str = "1"

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        hosts = {
            Source.MOCK: {"example.invalid"},
            Source.AVITO: {"www.avito.ru", "avito.ru"},
            Source.AUTO_RU: {"auto.ru"},
            Source.DROM: {"auto.drom.ru"},
        }
        url = urlsplit(self.source_url)
        if (
            url.scheme != "https"
            or url.hostname not in hosts[self.source]
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise ValueError("Недопустимый URL объявления")
        if self.observed_at.tzinfo is None or (self.published_at and self.published_at.tzinfo is None):
            raise ValueError("Время должно содержать часовой пояс")
        if not self.field_presence <= type(self).model_fields.keys():
            raise ValueError("Неизвестное поле в field_presence")
        allowed_images = {f"mock://{group}/{i}" for group in ("panamera", "bmw", "other") for i in range(6)}
        from app.sources.images import allowed_image

        if any(
            not ((url in allowed_images and self.source == Source.MOCK) or allowed_image(self.source, url))
            for url in self.images + ([self.main_image_url] if self.main_image_url else [])
        ):
            raise ValueError("Недопустимый источник изображения")
        if self.source_metadata:
            raise ValueError("source_metadata должен быть пустым")
        return self


class SearchInput(Model):
    name: str | None = Field(default=None, max_length=150)
    filters: UnifiedSearchFilters
    enabled_sources: list[Source] = Field(
        default_factory=lambda: [Source.DROM, Source.AUTO_RU], min_length=1, max_length=4
    )

    @field_validator("enabled_sources")
    @classmethod
    def unique_sources(cls, value: list[Source]) -> list[Source]:
        return list(dict.fromkeys(value))
