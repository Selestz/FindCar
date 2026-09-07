import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.catalog import canonical_names
from app.domain.models import NormalizedListing, Source
from app.sources.drom.parser import PARSER_VERSION

ENUMS = {
    "engine_type": {"бензин": "petrol", "дизель": "diesel", "гибрид": "hybrid", "электро": "electric"},
    "transmission": {"механика": "manual", "автомат": "automatic", "робот": "robot", "вариатор": "cvt"},
    "drive_type": {"4wd": "all", "передний": "front", "задний": "rear"},
    "steering_wheel": {"левый": "left", "правый": "right"},
    "body_type": {
        "лифтбек": "liftback",
        "седан": "sedan",
        "хэтчбек": "hatchback",
        "универсал": "wagon",
        "джип/suv": "suv",
        "купе": "coupe",
        "кабриолет": "convertible",
        "минивэн": "minivan",
        "пикап": "pickup",
    },
    "color": {
        "белый": "white",
        "черный": "black",
        "чёрный": "black",
        "серый": "grey",
        "серебристый": "silver",
        "синий": "blue",
        "красный": "red",
        "зеленый": "green",
        "зелёный": "green",
        "коричневый": "brown",
    },
}


def normalize(raw: dict[str, Any], observed_at: datetime) -> NormalizedListing:
    car, specs = raw["car"], raw.get("specs", {})
    offer = car["offers"]
    make, model = canonical_names("drom", raw["make"], raw["model"])
    values: dict[str, Any] = {
        "source": Source.DROM,
        "source_listing_id": raw["source_listing_id"],
        "source_url": raw["source_url"],
        "title": raw.get("title") or f"{car['name']}, {car.get('vehicleModelDate', '')}".strip(", "),
        "make": make,
        "model": model,
        "observed_at": observed_at,
        "status": "ACTIVE" if offer.get("availability", "").endswith("/InStock") else "UNKNOWN",
        "parser_version": PARSER_VERSION,
    }
    if offer.get("availability", "").endswith(("/SoldOut", "/OutOfStock")):
        values["status"] = "REMOVED"
    if car.get("vehicleModelDate"):
        values["year"] = int(car["vehicleModelDate"])
    if "price" in offer:
        values["price"] = Decimal(str(offer["price"])) if offer["price"] is not None else None
        values["currency"] = offer.get("priceCurrency", "XXX")
    mileage = car.get("mileageFromOdometer", {})
    if mileage.get("unitCode") == "KMT" and mileage.get("value") is not None:
        values["mileage_km"] = int(mileage["value"])
    summary = " ".join(raw.get("summary", []) + list(specs.values())).casefold()
    volume = re.search(r"(\d+(?:[.,]\d+)?)\s*л\b(?!\.\s*с)", summary)
    power = re.search(r"(\d+)\s*л\.с", summary)
    if volume:
        values["engine_volume"] = Decimal(volume[1].replace(",", "."))
    if power:
        values["power_hp"] = int(power[1])
    for key, translations in ENUMS.items():
        for term, value in translations.items():
            if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", summary):
                values[key] = value
                break
    owners = specs.get("владельцы", "")
    if "владельцы" in specs:
        values["owners_count"] = int(owners) if re.fullmatch(r"\d+", owners) else None
    if raw.get("city"):
        values["city"] = raw["city"]
        # No invented oblast mapping: only a directly observed city is available.
        values["region"] = raw["city"].casefold()
    if raw.get("description") is not None:
        values["description"] = raw["description"]
    # A generic “1 поколение” cannot be equated to a manufacturer's generation code.
    from app.sources.images import allowed_image

    image = car.get("image", {})
    image_url = image.get("url") if isinstance(image, dict) else None
    urls = list(dict.fromkeys(([image_url] if image_url else []) + raw.get("images", [])))
    unique: dict[str, str] = {}
    for url in urls:
        if allowed_image(Source.DROM, url):
            unique.setdefault(url.rsplit("/", 1)[0], url)
    urls = list(unique.values())[:6]
    if urls:
        values.update(main_image_url=urls[0], images=urls)
    values["field_presence"] = set(values) - {"observed_at", "parser_version"}
    # A cached search card cannot revive an offer confirmed removed on its detail page.
    if not raw.get("detail") and values["status"] != "REMOVED":
        values["field_presence"].discard("status")
    return NormalizedListing.model_validate(values)
