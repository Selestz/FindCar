"""Owned deterministic test artwork, not photographs of real cars or fetched content."""

import io
import random
from functools import lru_cache

from PIL import Image, ImageDraw

from app.matching.photos import Fingerprint, PHashProvider


@lru_cache(maxsize=32)
def image_bytes(key: str) -> bytes:
    rng = random.Random(key)
    image = Image.new("RGB", (480, 320), "#ced8dc")
    draw = ImageDraw.Draw(image)
    for _ in range(24):
        x, y = rng.randrange(430), rng.randrange(270)
        draw.rectangle(
            (x, y, x + rng.randrange(20, 120), y + rng.randrange(20, 100)),
            fill=tuple(rng.randrange(256) for _ in range(3)),
        )
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def prepare_images(urls: list[str]) -> list[Fingerprint]:
    # DTO permits only this finite mock namespace. Live acquisition belongs to Phase 4 adapters.
    return [PHashProvider().fingerprint(url, image_bytes(url)) for url in urls[:6]]
