from typing import Any

from app.domain.models import NormalizedListing, UnifiedSearchFilters


def evaluate(filters: UnifiedSearchFilters, listing: NormalizedListing) -> tuple[str, list[str]]:
    unknown: list[str] = []
    failed = False
    aliases = {"mileage": "mileage_km", "power": "power_hp"}
    for key, expected in filters.model_dump().items():
        if key == "schema_version" or expected is None or expected == []:
            continue
        actual: Any
        if key == "radius_km":
            unknown.append(key)
            continue
        if key == "body_types":
            actual = listing.body_type
            matches = actual in expected
        elif key == "owners_max":
            actual = listing.owners_count
            matches = actual is not None and actual <= expected
        elif key.endswith(("_from", "_to")):
            base, direction = key.rsplit("_", 1)
            actual = getattr(listing, aliases.get(base, base))
            if base == "price" and listing.currency != "RUB":
                actual = None
            matches = actual is not None and (
                actual >= expected if direction == "from" else actual <= expected
            )
        else:
            actual = getattr(listing, key)
            matches = actual == expected
        if actual is None:
            unknown.append(key)
        elif not matches:
            failed = True
    return ("not_matching" if failed else "unverified" if unknown else "confirmed"), unknown
