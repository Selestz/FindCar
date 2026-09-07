"""Bounded public HTML reads with same-host canonical redirects, without credentials or retries."""

import asyncio
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from app.sources.base import SourceFailure

_next_request: dict[str, float] = {}


class CanonicalRedirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


def retry_seconds(value: str | None) -> float:
    try:
        return max(60.0, float(value or "600"))
    except ValueError:
        try:
            return max(60.0, (parsedate_to_datetime(value or "") - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 600.0


class PublicTransport:
    def __init__(self, host: str, client: httpx.AsyncClient | None = None, interval: float = 1.5) -> None:
        self.host, self.client, self.interval = host, client, interval

    async def get(self, url: str) -> str:
        for _ in range(3):
            try:
                return await asyncio.wait_for(self._get_once(url), timeout=12)
            except CanonicalRedirect as redirect:
                url = redirect.url
            except TimeoutError as exc:
                raise SourceFailure("SOURCE_UNAVAILABLE") from exc
        raise SourceFailure("SOURCE_UNAVAILABLE")

    async def _get_once(self, url: str) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.host
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise SourceFailure("INVALID_SOURCE_URL")
        from app.services.monitoring import request_source, reserve_request

        source = request_source.get()
        if source:
            await asyncio.sleep(reserve_request(source))
        delay = _next_request.get(self.host, 0) - time.monotonic()
        if delay > self.interval + 1:
            raise SourceFailure("RATE_LIMITED")
        if delay > 0:
            await asyncio.sleep(delay)
        _next_request[self.host] = time.monotonic() + self.interval
        if self.client is not None:
            return await self._read(self.client, url)
        async with httpx.AsyncClient(
            timeout=8,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "FindCar/0.1 (private vehicle search)", "Accept": "text/html"},
        ) as client:
            return await self._read(client, url)

    async def _read(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            async with client.stream("GET", url, follow_redirects=False) as response:
                code = response.status_code
                if code == 429:
                    seconds = retry_seconds(response.headers.get("retry-after"))
                    _next_request[self.host] = time.monotonic() + seconds
                    from app.services.monitoring import cooldown, request_source

                    if source := request_source.get():
                        cooldown(source, seconds)
                    raise SourceFailure("RATE_LIMITED")
                if code in {401, 403}:
                    raise SourceFailure("AUTH_REQUIRED" if code == 401 else "SOURCE_UNAVAILABLE")
                if 300 <= code < 400:
                    location = response.headers.get("location", "").lower()
                    if (
                        code in {301, 308}
                        and location
                        and not any(x in location for x in ("auth.", "/login", "/sign", "captcha"))
                    ):
                        raise CanonicalRedirect(urljoin(url, response.headers["location"]))
                    raise SourceFailure(
                        "AUTH_REQUIRED"
                        if any(x in location for x in ("auth.", "/login", "/sign"))
                        else "SOURCE_UNAVAILABLE"
                    )
                if code != 200:
                    # 404/410 alone do not prove an individual advert was removed.
                    raise SourceFailure("SOURCE_UNAVAILABLE")
                if "text/html" not in response.headers.get("content-type", "").lower():
                    raise SourceFailure("PARSER_ERROR")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise SourceFailure("RESPONSE_TOO_LARGE")
                try:
                    return body.decode(response.encoding or "utf-8", errors="strict")
                except (UnicodeError, LookupError) as exc:
                    raise SourceFailure("PARSER_ERROR") from exc
        except httpx.HTTPError as exc:
            raise SourceFailure("SOURCE_UNAVAILABLE") from exc
