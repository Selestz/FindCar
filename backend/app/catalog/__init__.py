"""Local, versioned make/model directory built from public catalogue links."""

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache
def catalogue() -> dict[str, Any]:
    return json.loads(files(__package__).joinpath("data.json").read_text(encoding="utf8"))


@lru_cache
def makes() -> dict[str, Any]:
    return {make["id"]: make for make in catalogue()["makes"]}


@lru_cache
def models(make: str) -> dict[str, Any]:
    return {model["id"]: model for model in makes().get(make, {}).get("models", [])}


def source_scope(source: str, make: str, model: str | None) -> tuple[str, str | None]:
    from app.sources.base import SourceFailure

    entry = makes().get(make)
    if not entry:
        raise SourceFailure("UNSUPPORTED_FILTER")
    remote_make = entry["sources"].get(source)
    remote_model = models(make).get(model or "", {}).get("sources", {}).get(source)
    if not remote_make or (model and not remote_model):
        raise SourceFailure("UNSUPPORTED_FILTER")
    return remote_make, remote_model


@lru_cache
def reverse_names() -> dict[tuple[str, str, str | None], tuple[str, str | None]]:
    result: dict[tuple[str, str, str | None], tuple[str, str | None]] = {}
    for make in makes().values():
        for source, remote_make in make["sources"].items():
            result[source, remote_make, None] = make["id"], None
            for model in make["models"]:
                if source in model["sources"]:
                    result[source, remote_make, model["sources"][source]] = make["id"], model["id"]
    return result


def canonical_names(source: str, make: str, model: str) -> tuple[str, str]:
    pair = reverse_names().get((source, make, model))
    if pair:
        return pair[0], pair[1] or model
    return reverse_names().get((source, make, None), (make, None))[0], model


def search_name(filters: dict[str, Any]) -> str:
    make, model = filters.get("make"), filters.get("model")
    brand = makes().get(make or "", {}).get("label", make or "Автомобили")
    model_name = models(make or "").get(model or "", {}).get("label", model or "")
    parts = [(brand + " " + model_name).strip()]
    start, end = filters.get("year_from"), filters.get("year_to")
    if start or end:
        parts.append(
            f"{start}–{end}"
            if start and end and start != end
            else str(start or end)
            if start == end
            else f"{'от' if start else 'до'} {start or end}"
        )

    def price(value: Any) -> str:
        from decimal import Decimal

        number = Decimal(str(value))
        if number >= 1000000:
            millions = format(number / 1000000, "f")
            if "." in millions:
                millions = millions.rstrip("0").rstrip(".")
            return millions.replace(".", ",") + " млн ₽"
        return f"{number:,.0f}".replace(",", " ") + " ₽"

    lower, upper = filters.get("price_from"), filters.get("price_to")
    if lower and upper:
        parts.append(price(lower) + " – " + price(upper))
    elif lower or upper:
        parts.append(("от " if lower else "до ") + price(lower or upper))
    if filters.get("region"):
        parts.append(str(filters["region"]).title())
    return " · ".join(parts)[:150]
