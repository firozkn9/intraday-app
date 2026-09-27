"""Rate-limited, retrying HTTP client for Alpaca REST APIs.

Free (Basic) plan: 200 API calls/min (docs.alpaca.markets "About Market Data
API", verified 2026-09-26). We budget 90% of the configured limit.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from intraday.core.logging import get_logger

log = get_logger(__name__)

DATA_BASE_URL = "https://data.alpaca.markets"
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Credentials:
    key_id: str
    secret_key: str

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret_key}

    def __repr__(self) -> str:  # never leak secrets into logs
        return f"Credentials(key_id={self.key_id[:4]}…)"


class AlpacaError(Exception):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"Alpaca HTTP {status}: {message}")
        self.status, self.body = status, body


class TokenBucket:
    """Async token bucket: ``per_minute`` requests per rolling minute, evenly refilled."""

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Sleep = asyncio.sleep) -> None:
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await self._sleep((1 - self.tokens) / self.rate)


class AlpacaHttp:
    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(self, base_url: str, credentials: Credentials, *, limiter: TokenBucket,
                 client: httpx.AsyncClient | None = None, max_retries: int = 4,
                 timeout: float = 10.0, sleep: Sleep = asyncio.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self._creds = credentials
        self._limiter = limiter
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._max_retries = max_retries
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        base = min(30.0, 0.5 * 2.0 ** attempt)
        return float(base * (0.8 + 0.4 * random.random()))  # noqa: S311 - jitter, not crypto

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: dict[str, Any], *, retry: bool = False) -> Any:
        """Writes don't retry by default: the caller decides (order idempotency)."""
        return await self.request("POST", path, json=body, retries=None if retry else 0)

    async def patch(self, path: str, body: dict[str, Any], *, retry: bool = False) -> Any:
        return await self.request("PATCH", path, json=body, retries=None if retry else 0)

    async def delete(self, path: str, params: dict[str, Any] | None = None, *,
                     retry: bool = True) -> Any:
        return await self.request("DELETE", path, params=params, retries=None if retry else 0)

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                      json: dict[str, Any] | None = None, retries: int | None = None) -> Any:
        url = f"{self.base_url}{path}"
        max_retries = self._max_retries if retries is None else retries
        attempt = 0
        while True:
            await self._limiter.acquire()
            try:
                resp = await self._client.request(method, url, params=params, json=json,
                                                  headers=self._creds.headers())
            except httpx.TransportError as exc:
                if attempt >= max_retries:
                    raise AlpacaError(0, f"network error: {type(exc).__name__}") from exc
                delay = self._backoff(attempt, None)
                log.warning("alpaca_network_retry", method=method, path=path,
                            error=type(exc).__name__,
                            retry_in=round(delay, 2))
            else:
                if resp.status_code < 400:
                    return None if resp.status_code == 204 or not resp.content else resp.json()
                if resp.status_code not in self.RETRY_STATUSES or attempt >= max_retries:
                    body = _safe_json(resp)
                    message = body.get("message", resp.reason_phrase) if isinstance(body, dict) \
                        else resp.reason_phrase
                    raise AlpacaError(resp.status_code, str(message), body)
                delay = self._backoff(attempt, resp.headers.get("retry-after"))
                log.warning("alpaca_http_retry", path=path, status=resp.status_code,
                            retry_in=round(delay, 2))
            attempt += 1
            await self._sleep(delay)


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None
