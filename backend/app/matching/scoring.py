import re
from typing import Any

from app.matching.config import MatchingConfig
from app.matching.photos import Fingerprint, photo_signal

SPEC_FIELDS = (
    "make",
    "model",
    "generation",
    "year",
    "body_type",
    "engine_type",
    "engine_volume",
    "power_hp",
    "transmission",
    "drive_type",
    "color",
)
VETO_FIELDS = ("make", "model", "generation", "engine_type", "body_type")


def conflicts(a: dict[str, Any], b: dict[str, Any], config: MatchingConfig) -> list[str]:
    result = [f for f in VETO_FIELDS if a.get(f) is not None and b.get(f) is not None and a[f] != b[f]]
    if (
        a.get("year") is not None
        and b.get("year") is not None
        and abs(a["year"] - b["year"]) > config.year_tolerance
    ):
        result.append("year")
    return result


def score_pair(
    a: dict[str, Any],
    b: dict[str, Any],
    left: list[Fingerprint],
    right: list[Fingerprint],
    config: MatchingConfig | None = None,
) -> dict[str, Any]:
    config = config or MatchingConfig()
    photos = photo_signal(left, right, config)
    known = [f for f in SPEC_FIELDS if a.get(f) is not None and b.get(f) is not None]
    specs = (
        sum(a[f] == b[f] or (f == "year" and abs(a[f] - b[f]) <= config.year_tolerance) for f in known)
        / len(known)
        if known
        else None
    )
    coverage = len(known) / len(SPEC_FIELDS)
    mileage = None
    difference = None
    if a.get("mileage_km") is not None and b.get("mileage_km") is not None:
        difference = abs(a["mileage_km"] - b["mileage_km"])
        tolerance = max(
            config.mileage_absolute_tolerance,
            config.mileage_relative_tolerance * max(a["mileage_km"], b["mileage_km"]),
        )
        mileage = max(0.0, 1 - difference / tolerance)

    def tokens(text: str | None) -> set[str]:
        clean = re.sub(r"[\w.+-]+@[\w.-]+|\+?\d[\d ()-]{8,}\d|https?://\S+", " ", text or "")
        return {word for word in re.findall(r"\w+", clean.lower()) if len(word) > 2}

    ta, tb = tokens(a.get("description")), tokens(b.get("description"))
    description = len(ta & tb) / len(ta | tb) if ta and tb else None
    price = None
    if a.get("price") and b.get("price") and a.get("currency") == b.get("currency"):
        price = float(min(a["price"], b["price"]) / max(a["price"], b["price"]))
    signals = {
        "photos": photos["score"],
        "specifications": specs,
        "mileage": mileage,
        "description": description,
        "price": price,
        "rare_features": None,
    }
    total = sum(config.weights[k] * (v or 0) for k, v in signals.items())
    veto = conflicts(a, b, config)
    core = all(a.get(f) is not None and b.get(f) is not None for f in ("make", "model", "year")) and not veto
    decision, reason = "insufficient_evidence", "insufficient_evidence"
    if total >= config.auto_merge_threshold and photos["strong"] and core:
        decision, reason = "auto_merge", "strong_photos"
    elif total >= config.possible_duplicate_threshold:
        decision, reason = "possible_duplicate", "conflicting_fields" if veto else "review_score"
    elif (
        not photos["sufficient"]
        and core
        and specs is not None
        and specs >= config.review_specs_threshold
        and coverage >= config.review_specs_coverage
        and mileage is not None
        and mileage > 0
    ):
        decision, reason = "possible_duplicate", "insufficient_photos"
    elif photos["sufficient"] or veto:
        decision, reason = "different", "conflicting_fields" if veto else "different_photos"
    return {
        "algorithm_version": "weighted-v1",
        "config_version": config.version,
        "config": config.model_dump(),
        "total_score": round(total, 6),
        "signals": signals,
        "photos": photos,
        "specification_coverage": coverage,
        "mileage_difference": difference,
        "conflicts": veto,
        "decision": decision,
        "reason": reason,
        "confidence": "high"
        if decision == "auto_merge"
        else "medium"
        if decision == "possible_duplicate"
        else "insufficient_evidence"
        if decision == "insufficient_evidence"
        else "low",
    }
