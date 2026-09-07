import hashlib
import json
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MatchingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    auto_merge_threshold: float = Field(default=0.88, ge=0, le=1)
    possible_duplicate_threshold: float = Field(default=0.68, ge=0, le=1)
    mileage_absolute_tolerance: int = Field(default=10000, gt=0)
    mileage_relative_tolerance: float = Field(default=0.1, gt=0, le=1)
    year_tolerance: int = Field(default=1, ge=0, le=3)
    phash_distance_threshold: int = Field(default=8, ge=0, le=16)
    minimum_matching_photos: int = Field(default=3, ge=3, le=6)
    maximum_processed_photos: int = Field(default=6, ge=3, le=6)
    candidate_limit: int = Field(default=100, ge=1, le=1000)
    relist_window_days: int = Field(default=365, ge=1, le=730)
    review_specs_threshold: float = Field(default=0.9, ge=0, le=1)
    review_specs_coverage: float = Field(default=0.7, ge=0, le=1)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "photos": 0.60,
            "specifications": 0.22,
            "mileage": 0.10,
            "description": 0.05,
            "rare_features": 0.01,
            "price": 0.02,
        }
    )

    @model_validator(mode="after")
    def valid(self) -> Self:
        expected = {"photos", "specifications", "mileage", "description", "rare_features", "price"}
        if set(self.weights) != expected or any(not 0 <= v <= 1 for v in self.weights.values()):
            raise ValueError("Invalid signal weights")
        if abs(sum(self.weights.values()) - 1) > 1e-9:
            raise ValueError("Weights must sum to one")
        if self.possible_duplicate_threshold > self.auto_merge_threshold:
            raise ValueError("Review threshold must not exceed auto threshold")
        if self.minimum_matching_photos > self.maximum_processed_photos:
            raise ValueError("Not enough processed photos")
        return self

    @property
    def version(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()[:16]
