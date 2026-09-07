import json
import re
from typing import Any
from urllib.parse import urlsplit

from app.sources.base import SourceFailure, SourcePage
from app.sources.html import Node, Tree

PARSER_VERSION = "drom-html-1"


def identity(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    match = re.fullmatch(r"/(?:[a-z0-9_-]+/)?([a-z0-9_-]+)/([a-z0-9_-]+)/(\d+)\.html", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "auto.drom.ru"
        or not match
        or parsed.query
        or parsed.fragment
    ):
        raise SourceFailure("PARSER_ERROR")
    return match[3], match[1], match[2]


def document(html: str) -> tuple[Node, list[dict[str, Any]]]:
    root = Tree(html).root
    title = " ".join(n.text() for n in root.walk() if n.tag in {"title", "h1"}).casefold()
    if any(
        x in title for x in ("captcha", "капч", "проверка доступа", "подтвердите, что вы", "доступ ограничен")
    ):
        raise SourceFailure("SOURCE_UNAVAILABLE")
    if "вход на" in title or "авторизация" in title:
        raise SourceFailure("AUTH_REQUIRED")
    cars = []
    for node in root.walk():
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            try:
                value = json.loads("".join(x for x in node.children if isinstance(x, str)))
            except (ValueError, RecursionError) as exc:
                raise SourceFailure("PARSER_ERROR") from exc
            if isinstance(value, dict) and value.get("@type") == "Car":
                cars.append(value)
    return root, cars


def marked(node: Node, name: str) -> str | None:
    found = node.find(name)
    return found[0].text() if found else None


def base_car(car: dict[str, Any]) -> dict[str, Any]:
    offer = car.get("offers")
    if not isinstance(offer, dict) or not isinstance(offer.get("url"), str):
        raise SourceFailure("PARSER_ERROR")
    listing_id, make, model = identity(offer["url"])
    return {
        "car": car,
        "source_url": offer["url"],
        "source_listing_id": listing_id,
        "make": make,
        "model": model,
        "specs": {},
    }


def search(html: str, page: int) -> SourcePage:
    root, cars = document(html)
    if not root.find("sales__filter"):
        raise SourceFailure("PARSER_ERROR")
    cards = root.find("bulls-list_bull")
    if not cars:
        # Empty only when the site explicitly says so; a changed schema is not an empty search.
        text = root.text().casefold()
        if not cards and any(x in text for x in ("объявлений не найдено", "нет объявлений", "0 объявлений")):
            return SourcePage(items=[])
        raise SourceFailure("PARSER_ERROR")
    by_url = {n.find("bull_title")[0].attrs.get("href"): n for n in cards if n.find("bull_title")}
    items = []
    for car in cars:
        raw = base_car(car)
        card = by_url.get(raw["source_url"])
        if card is None:
            raise SourceFailure("PARSER_ERROR")
        raw["title"] = marked(card, "bull_title")
        raw["city"] = marked(card, "bull_location")
        raw["summary"] = [n.text() for n in card.find("bull_description-item")]
        items.append(raw)
    next_links = root.find("component_pagination-item-next")
    next_page = None
    if next_links:
        target = urlsplit(next_links[0].attrs.get("href", ""))
        match = re.search(r"/page(\d+)/$", target.path)
        if target.netloc != "auto.drom.ru" or not match or int(match[1]) != page + 1:
            raise SourceFailure("PARSER_ERROR")
        next_page = int(match[1])
    return SourcePage(items=items, next_page=next_page, coverage="bounded", warnings=["BOUNDED_SEARCH"])


def detail(html: str, expected_id: str) -> dict[str, Any]:
    root, cars = document(html)
    matches = [base_car(c) for c in cars if identity(c.get("offers", {}).get("url", ""))[0] == expected_id]
    if len(matches) != 1:
        raise SourceFailure("PARSER_ERROR")
    raw = matches[0]
    tables = root.find("bulletin-specifications")
    if not tables:
        raise SourceFailure("PARSER_ERROR")
    for row in tables[0].walk():
        if row.tag == "tr":
            name, value = marked(row, "property"), marked(row, "value")
            if name and value:
                raw["specs"][name.strip().casefold()] = value.strip()
    raw["description"] = marked(root, "bulletin-description")
    galleries = root.find("bull-page_bull-gallery_thumbnails")
    if galleries:
        raw["images"] = [
            n.attrs["src"] for n in galleries[0].walk() if n.tag == "img" and n.attrs.get("src")
        ][:6]
    raw["summary"] = []
    raw["detail"] = True
    return raw
