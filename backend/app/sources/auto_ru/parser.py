import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.sources.base import SourceFailure, SourcePage
from app.sources.html import Node, Tree

PARSER_VERSION = "autoru-html-1"


def classified(root: Node, prefix: str) -> list[Node]:
    return [
        n
        for n in root.walk()
        if any(c == prefix or c.startswith(prefix + "-") for c in n.attrs.get("class", "").split())
    ]


def identity(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    match = re.fullmatch(r"/cars/used/sale/([a-z0-9_-]+)/([a-z0-9_-]+)/(\d+-[a-z0-9]+)/", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "auto.ru" or not match or parsed.query or parsed.fragment:
        raise SourceFailure("PARSER_ERROR")
    return match[3], match[1], match[2]


def document(html: str) -> tuple[Node, dict[str, Any]]:
    root = Tree(html).root
    title = " ".join(n.text() for n in root.walk() if n.tag in {"title", "h1"}).casefold()
    if any(x in title for x in ("captcha", "проверка", "доступ ограничен", "вы не робот")):
        raise SourceFailure("SOURCE_UNAVAILABLE")
    if "вход" in title or "авторизация" in title:
        raise SourceFailure("AUTH_REQUIRED")
    for node in root.walk():
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            try:
                data = json.loads(node.text())
            except ValueError as exc:
                raise SourceFailure("PARSER_ERROR") from exc
            if isinstance(data, dict) and data.get("@type") == "Product":
                return root, data
    raise SourceFailure("PARSER_ERROR")


def search(html: str, page: int) -> SourcePage:
    root, product = document(html)
    cards = classified(root, "ListingItemUniversal")
    # A genuine empty catalog has Product metadata but no offers. Its separate
    # recommendation carousel must never be treated as matching search results.
    if "offers" not in product and classified(root, "ListingEmptyOffers") and not cards:
        return SourcePage(items=[])
    aggregate = product.get("offers", {})
    offers = aggregate.get("offers")
    if not isinstance(offers, list):
        raise SourceFailure("PARSER_ERROR")
    if not offers:
        if aggregate.get("offerCount") == 0 and not cards:
            return SourcePage(items=[])
        raise SourceFailure("PARSER_ERROR")
    by_url = {}
    for card in cards:
        links = classified(card, "ListingItemTitle__link")
        if links:
            by_url[links[0].attrs.get("href")] = (card, links[0])
    items = []
    for offer in offers:
        listing_id, make, model = identity(offer.get("url", ""))
        pair = by_url.get(offer["url"])
        if not pair:
            raise SourceFailure("PARSER_ERROR")
        card, link = pair
        specs = classified(card, "ListingItemUniversalSpecs__spec")
        city = classified(card, "MetroListPlace__regionName")
        image = offer.get("image", {})
        items.append(
            {
                "source_listing_id": listing_id,
                "source_url": offer["url"],
                "make": make,
                "model": model,
                "title": link.text(),
                "summary": " ".join(n.text() for n in specs),
                "city": city[0].text() if city else None,
                "offer": {k: offer[k] for k in ("price", "priceCurrency", "availability") if k in offer},
                "images": [image["contentUrl"]]
                if isinstance(image, dict) and image.get("contentUrl")
                else [],
                "specs": {},
            }
        )
    next_links = classified(root, "ListingPagination__next")
    next_page = None
    if next_links and next_links[0].attrs.get("href"):
        url = urlsplit(next_links[0].attrs["href"])
        if url.netloc != "auto.ru" or parse_qs(url.query).get("page") != [str(page + 1)]:
            raise SourceFailure("PARSER_ERROR")
        next_page = page + 1
    return SourcePage(items=items, next_page=next_page, coverage="bounded", warnings=["BOUNDED_SEARCH"])


def detail(html: str, expected_id: str) -> dict[str, Any]:
    root, product = document(html)
    offer = product.get("offers", {})
    if identity(offer.get("url", ""))[0] != expected_id:
        raise SourceFailure("PARSER_ERROR")
    if not classified(root, "CardInfoSummary"):
        raise SourceFailure("PARSER_ERROR")
    specs = {}
    for row in classified(root, "CardInfoSummarySimpleRow"):
        labels, values = (
            classified(row, "CardInfoSummarySimpleRow__label"),
            classified(row, "CardInfoSummarySimpleRow__content"),
        )
        if labels and values and labels[0].text().casefold() in {"год выпуска", "пробег", "владельцы"}:
            specs[labels[0].text().casefold()] = values[0].text()
    for row in classified(root, "CardInfoSummaryComplexRow"):
        labels = classified(row, "CardInfoSummaryComplexRow__cellTitle")
        if labels and labels[0].text().casefold() in {
            "двигатель",
            "коробка",
            "привод",
            "руль",
            "кузов",
            "цвет",
        }:
            specs[labels[0].text().casefold()] = row.text().removeprefix(labels[0].text()).strip()
    images = product.get("image", [])
    return {
        "title": product.get("name", ""),
        "offer": {k: offer[k] for k in ("price", "priceCurrency", "availability") if k in offer},
        "specs": specs,
        "description": product.get("description"),
        "detail": True,
        "images": [n["contentUrl"] for n in images if isinstance(n, dict) and n.get("contentUrl")],
        "production_year": product.get("productionDate"),
    }
