import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.models import NormalizedListing, Source
from app.sources.auto_ru.parser import PARSER_VERSION
from app.sources.images import allowed_image


def normalize(raw: dict[str, Any], observed_at: datetime) -> NormalizedListing:
    offer, specs = raw["offer"], raw.get("specs", {})
    title = raw["title"].split(",")[0]
    values: dict[str, Any] = {
        "source": Source.AUTO_RU,
        "source_listing_id": raw["source_listing_id"],
        "source_url": raw["source_url"],
        "title": title,
        "make": raw["make"],
        "model": raw["model"],
        "observed_at": observed_at,
        "parser_version": PARSER_VERSION,
        "status": "ACTIVE" if offer.get("availability", "").endswith("/InStock") else "UNKNOWN",
    }
    if raw.get("detail") and offer.get("availability", "").endswith(("/SoldOut", "/OutOfStock")):
        values["status"] = "REMOVED"
    if "price" in offer:
        values["price"] = Decimal(str(offer["price"])) if offer["price"] is not None else None
        values["currency"] = (
            "RUB" if offer.get("priceCurrency") in {"RUB", "RUR"} else offer.get("priceCurrency", "XXX")
        )
    year = raw.get("production_year") or specs.get("год выпуска")
    if not year:
        match = re.search(r",\s*((?:19|20)\d{2})\b", raw["title"])
        year = match[1] if match else None
    if year:
        values["year"] = int(year)
    mileage = re.search(r"([\d\s\u00a0]+)\s*км\b", specs.get("пробег", raw["title"]))
    if mileage:
        values["mileage_km"] = int(re.sub(r"\D", "", mileage[1]))
    summary = (raw.get("summary", "") + " " + " ".join(specs.values())).casefold().replace("\u00a0", " ")
    volume = re.search(r"(\d+(?:[.,]\d+)?)\s*л\b(?!\.\s*с)", summary)
    power = re.search(r"(\d+)\s*л\.с", summary)
    if volume:
        values["engine_volume"] = Decimal(volume[1].replace(",", "."))
    if power:
        values["power_hp"] = int(power[1])
    enums = {
        "engine_type": {"бензин": "petrol", "дизель": "diesel", "гибрид": "hybrid", "электро": "electric"},
        "transmission": {"робот": "robot", "автомат": "automatic", "вариатор": "cvt", "механик": "manual"},
        "drive_type": {"полный": "all", "передний": "front", "задний": "rear"},
        "steering_wheel": {"левый": "left", "правый": "right"},
        "body_type": {
            "универсал": "wagon",
            "лифтбек": "liftback",
            "седан": "sedan",
            "хэтчбек": "hatchback",
            "внедорожник": "suv",
            "купе": "coupe",
            "кабриолет": "convertible",
        },
        "color": {
            "чёрный": "black",
            "черный": "black",
            "белый": "white",
            "серый": "grey",
            "серебристый": "silver",
            "синий": "blue",
        },
    }
    for field, variants in enums.items():
        for text, value in variants.items():
            if text in summary:
                values[field] = value
                break
    if "владельцы" in specs:
        match = re.fullmatch(r"(\d+)\s*(?:владелец|владельца|владельцев)?", specs["владельцы"].strip())
        values["owners_count"] = int(match[1]) if match else None
    if raw.get("city"):
        values.update(city=raw["city"], region=raw["city"].casefold())
    if raw.get("description"):
        values["description"] = raw["description"][:30000]
    photos: dict[str, str] = {}
    for url in raw.get("images", []):
        if allowed_image(Source.AUTO_RU, url):
            photos.setdefault(url.rsplit("/", 1)[0], url)
    if photos:
        urls = list(photos.values())[:6]
        values.update(images=urls, main_image_url=urls[0])
    values["field_presence"] = set(values) - {"observed_at", "parser_version"}
    return NormalizedListing.model_validate(values)
