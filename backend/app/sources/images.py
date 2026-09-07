"""Allowlisted raster thumbnails; bounded memory cache, no cookies or external redirects."""

import io
import threading
import time
import warnings
from collections import OrderedDict
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.sources.base import SourceFailure

_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_lock = threading.Lock()


def allowed_image(source: str, url: str) -> bool:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
        if not valid:
            return False
        if source == "drom":
            return (
                parsed.hostname == "s31.auto.drom.ru"
                and parsed.path.startswith("/photo/")
                and parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            )
        if source == "auto_ru":
            return (
                (parsed.hostname == "photo.auto.ru" and parsed.path.startswith("/photo/get-autoru-"))
                or (parsed.hostname == "avatars.avto.ru" and parsed.path.startswith("/get-autoru-"))
            ) and parsed.path.rsplit("/", 1)[-1] in {"320x240", "456x342", "1200x900"}
        return False
    except ValueError:
        return False


def sanitize(data: bytes) -> bytes:
    if len(data) > 2_000_000:
        raise SourceFailure("IMAGE_UNAVAILABLE")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as original:
                if original.width * original.height > 16_000_000:
                    raise SourceFailure("IMAGE_UNAVAILABLE")
                picture = ImageOps.exif_transpose(original).convert("RGB")
                picture.thumbnail((1000, 800))
                out = io.BytesIO()
                picture.save(out, format="JPEG", quality=82)
                return out.getvalue()
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise SourceFailure("IMAGE_UNAVAILABLE") from exc


def thumbnail(source: str, url: str) -> bytes:
    if not allowed_image(source, url):
        raise SourceFailure("IMAGE_UNAVAILABLE")
    # Serialize cold loads to avoid a stampede from concurrent browser image requests.
    with _lock:
        cached = _cache.get(url)
        if cached and cached[0] > time.monotonic():
            _cache.move_to_end(url)
            return cached[1]
        try:
            from app.services.monitoring import reserve_request

            time.sleep(reserve_request(source))
            deadline = time.monotonic() + 10
            with httpx.stream(
                "GET",
                url,
                timeout=5,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "FindCar/0.1 (private vehicle search)"},
            ) as response:
                if response.status_code == 429:
                    from app.services.monitoring import cooldown
                    from app.sources.transport import retry_seconds

                    cooldown(source, retry_seconds(response.headers.get("retry-after")))
                if response.status_code != 200 or response.headers.get("content-type", "").split(";")[
                    0
                ] not in {"image/jpeg", "image/png", "image/webp"}:
                    raise SourceFailure("IMAGE_UNAVAILABLE")
                data = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise SourceFailure("IMAGE_UNAVAILABLE")
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise SourceFailure("IMAGE_UNAVAILABLE")
                result = sanitize(bytes(data))
        except httpx.HTTPError as exc:
            raise SourceFailure("IMAGE_UNAVAILABLE") from exc
        _cache[url] = (time.monotonic() + 86400, result)
        while len(_cache) > 64:
            _cache.popitem(last=False)
        return result
