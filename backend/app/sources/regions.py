"""City routes observed in the sources' public navigation, 2026-09-07."""

from typing import Any

DROM_REGIONS = {
    "москва": "moscow",
    "санкт-петербург": "spb",
    "новосибирск": "novosibirsk",
    "екатеринбург": "ekaterinburg",
    "казань": "kazan",
    "краснодар": "krasnodar",
    "красноярск": "krasnoyarsk",
    "ростов-на-дону": "rostov-na-donu",
    "самара": "samara",
    "уфа": "ufa",
    "омск": "omsk",
    "челябинск": "chelyabinsk",
    "иркутск": "irkutsk",
    "тюмень": "tyumen",
}
AUTO_RU_REGIONS = {"москва": "moskva"}


def region_catalog() -> list[dict[str, Any]]:
    return [{"id": "", "label": "Вся Россия", "sources": ["drom", "auto_ru"]}] + [
        {
            "id": city,
            "label": ("Санкт-Петербург" if city == "санкт-петербург" else city.capitalize()),
            "sources": ["drom"] + (["auto_ru"] if city in AUTO_RU_REGIONS else []),
        }
        for city in DROM_REGIONS
    ]
