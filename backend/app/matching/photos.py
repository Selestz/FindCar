"""Bounded byte processing. Source adapters own image acquisition; this module never fetches URLs."""

import io
import math
import statistics
import warnings
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Protocol, cast

from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

from app.matching.config import MatchingConfig


@dataclass(frozen=True)
class Fingerprint:
    image_id: str
    value: str | None
    quality: str
    algorithm: str = "phash64-dct32-pillow12.3-v1"

    def json(self) -> dict[str, Any]:
        return asdict(self)


class FingerprintProvider(Protocol):
    def fingerprint(self, image_id: str, data: bytes, *, stock: bool = False) -> Fingerprint: ...
    def distance(self, a: Fingerprint, b: Fingerprint) -> int | None: ...


class PHashProvider:
    def fingerprint(self, image_id: str, data: bytes, *, stock: bool = False) -> Fingerprint:
        if stock:
            return Fingerprint(image_id, None, "stock")
        if len(data) > 8 * 1024 * 1024:
            return Fingerprint(image_id, None, "too_large")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as original:
                    if original.width * original.height > 16_000_000:
                        return Fingerprint(image_id, None, "too_large")
                    if min(original.size) < 64:
                        return Fingerprint(image_id, None, "too_small")
                    gray = (
                        ImageOps.exif_transpose(original)
                        .convert("L")
                        .resize((32, 32), Image.Resampling.LANCZOS)
                    )
                    if ImageStat.Stat(gray).stddev[0] < 8:
                        return Fingerprint(image_id, None, "flat")
                    pixels = cast(list[int], list(gray.get_flattened_data()))
            cosines = [[math.cos(math.pi * (2 * x + 1) * u / 64) for x in range(32)] for u in range(8)]
            horizontal = [
                [sum(pixels[y * 32 + x] * cosines[u][x] for x in range(32)) for u in range(8)]
                for y in range(32)
            ]
            coefficients = [
                sum(horizontal[y][u] * cosines[v][y] for y in range(32)) for v in range(8) for u in range(8)
            ]
            median = statistics.median(coefficients[1:])
            value = sum((1 << i) for i, c in enumerate(coefficients) if c > median)
            return Fingerprint(image_id, f"{value:016x}", "ok")
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            return Fingerprint(image_id, None, "invalid")

    def distance(self, a: Fingerprint, b: Fingerprint) -> int | None:
        if a.algorithm != b.algorithm or a.value is None or b.value is None:
            return None
        return (int(a.value, 16) ^ int(b.value, 16)).bit_count()


def photo_signal(left: list[Fingerprint], right: list[Fingerprint], config: MatchingConfig) -> dict[str, Any]:
    provider = PHashProvider()

    def unique(photos: list[Fingerprint]) -> list[Fingerprint]:
        result: list[Fingerprint] = []
        for photo in sorted(photos, key=lambda p: p.image_id)[: config.maximum_processed_photos]:
            if photo.quality != "ok" or photo.value is None:
                continue
            if not any(
                (provider.distance(photo, p) or 0) <= config.phash_distance_threshold
                for p in result
                if p.algorithm == photo.algorithm
            ):
                result.append(photo)
        return result

    a, b = unique(left), unique(right)
    distances = [[provider.distance(x, y) for y in b] for x in a]

    @lru_cache(None)
    def assign(i: int, used: int) -> tuple[tuple[int, int, int], ...]:
        if i == len(a):
            return ()
        choices = [assign(i + 1, used)]
        for j, distance in enumerate(distances[i]):
            if not used & (1 << j) and distance is not None and distance <= config.phash_distance_threshold:
                choices.append(((i, j, distance),) + assign(i + 1, used | (1 << j)))
        return max(choices, key=lambda pairs: (len(pairs), -sum(p[2] for p in pairs)))

    pairs = assign(0, 0)
    comparable = any(d is not None for row in distances for d in row)
    score = (
        (
            sum(1 - d / 64 for _, _, d in pairs)
            / len(pairs)
            * min(1, len(pairs) / config.minimum_matching_photos)
        )
        if pairs
        else (0.0 if comparable else None)
    )
    return {
        "score": score,
        "matching_count": len(pairs),
        "unique_counts": [len(a), len(b)],
        "strong": len(pairs) >= config.minimum_matching_photos,
        "sufficient": comparable and min(len(a), len(b)) >= config.minimum_matching_photos,
        "pairs": [{"left": a[i].image_id, "right": b[j].image_id, "distance": d} for i, j, d in pairs],
        "quality_flags": sorted({p.quality for p in left + right if p.quality != "ok"}),
        "unavailable_reason": None if comparable else "missing_or_incompatible_fingerprints",
    }
