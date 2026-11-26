"""Thin async client for the Scryfall API.

Handles the things Scryfall asks of API consumers: identifying headers, a
delay between requests, and caching so repeat lookups don't hit their servers.
"""

from __future__ import annotations

import time
from typing import Any

import anyio
import httpx

BASE_URL = "https://api.scryfall.com"
IMAGE_HOST_SUFFIX = ".scryfall.io"
USER_AGENT = "scryfall-mcp/0.1.0"

# Scryfall asks for 50-100ms between requests (about 10 per second).
MIN_INTERVAL_SECONDS = 0.1
# Card data and prices change at most daily, so an hour is conservative.
CACHE_TTL_SECONDS = 3600
CACHE_MAX_ENTRIES = 512


class ScryfallError(Exception):
    """An error reported by Scryfall, or a failure to reach it."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def not_found(self) -> bool:
        return self.status == 404


class ScryfallClient:
    def __init__(
        self,
        *,
        min_interval: float = MIN_INTERVAL_SECONDS,
        cache_ttl: float = CACHE_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._min_interval = min_interval
        self._cache_ttl = cache_ttl
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._lock: anyio.Lock | None = None
        self._last_request = 0.0
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}

    async def get(self, path: str, params: dict[str, Any] | None = None, *, cache: bool = True) -> Any:
        """GET a Scryfall path (or a full Scryfall URL) and return the parsed JSON."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        key = ("GET", path, tuple(sorted(clean.items())))
        if cache:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        data = await self._request("GET", path, params=clean)
        if cache:
            self._cache_put(key, data)
        return data

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        return await self._request("POST", path, json=body)

    async def get_image(self, url: str) -> tuple[bytes, str]:
        """Download a card image. Returns the bytes and the image format ('jpeg' or 'png').

        Only Scryfall's image host is allowed, so a URL can't be used to make this
        server fetch arbitrary addresses. That host has no rate limit, so this
        skips the delay the API calls observe.
        """
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or not (parsed.host or "").endswith(IMAGE_HOST_SUFFIX):
            raise ScryfallError(f"Refusing to fetch an image from outside Scryfall: {parsed.host}")
        try:
            response = await self._client().get(url, headers={"Accept": "image/*"})
        except httpx.HTTPError as exc:
            raise ScryfallError(f"Could not download the card image: {exc!r}") from exc
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if not response.is_success or not content_type.startswith("image/"):
            raise ScryfallError(f"Scryfall returned HTTP {response.status_code} for the card image", status=response.status_code)
        return response.content, content_type.removeprefix("image/")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- internals ---------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=20.0,
                transport=self._transport,
            )
        return self._http

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._lock is None:
            self._lock = anyio.Lock()

        for attempt in range(2):
            # Holding the lock across the wait serialises requests, which is
            # what keeps us under the rate limit when tools run concurrently.
            async with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    await anyio.sleep(wait)
                try:
                    response = await self._client().request(method, path, **kwargs)
                except httpx.HTTPError as exc:
                    raise ScryfallError(f"Could not reach Scryfall: {exc!r}") from exc
                finally:
                    self._last_request = time.monotonic()

            if response.status_code == 429 and attempt == 0:
                await anyio.sleep(_retry_after(response))
                continue
            return _parse(response)

        raise ScryfallError("Scryfall rate limit hit; try again shortly.", status=429)

    def _cache_get(self, key: tuple[Any, ...]) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires, data = entry
        if expires < time.monotonic():
            del self._cache[key]
            return None
        return data

    def _cache_put(self, key: tuple[Any, ...], data: Any) -> None:
        if len(self._cache) >= CACHE_MAX_ENTRIES:
            # dicts keep insertion order, so the first key is the oldest.
            del self._cache[next(iter(self._cache))]
        self._cache[key] = (time.monotonic() + self._cache_ttl, data)


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(float(response.headers.get("Retry-After", "1")), 5.0)
    except ValueError:
        return 1.0


def _parse(response: httpx.Response) -> Any:
    try:
        data = response.json()
    except ValueError:
        data = None

    if response.is_success and data is not None:
        return data

    if isinstance(data, dict) and data.get("object") == "error":
        message = data.get("details") or "Unknown Scryfall error"
        warnings = data.get("warnings") or []
        if warnings:
            message += " " + " ".join(warnings)
        raise ScryfallError(message, status=response.status_code, code=data.get("code"))

    raise ScryfallError(f"Scryfall returned HTTP {response.status_code}", status=response.status_code)
