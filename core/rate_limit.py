from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.config import settings

_RATE_LIMIT_HEADER = "X-RateLimit-Limit"
_RATE_LIMIT_REMAINING_HEADER = "X-RateLimit-Remaining"
_RATE_LIMIT_RESET_HEADER = "X-RateLimit-Reset"
_RETRY_AFTER_HEADER = "Retry-After"

# Scope resolution happens on the raw path prefix before any routing so the
# limiter stays independent of route declarations.
_AUTH_PREFIXES   = ("/api/v1/auth",)
_IOT_PREFIXES    = ("/api/v1/iot", "/api/v1/nozzles")
_WRITE_METHODS   = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class SlidingWindowLimiter:
    """
    Sliding-window rate limiter backed by per-key deques of timestamps.

    Single-process memory only — correct for the single Railway service this
    API deploys as. Horizontal scaling would need a shared store (Redis) in
    place of the in-memory windows.
    """

    def __init__(self) -> None:
        self._windows: Dict[Tuple[str, int], Deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def hit(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int, int]:
        """
        Record one request. Returns (allowed, remaining, reset_epoch_seconds).
        Expired entries are pruned lazily; a global sweep every 60 cycles
        evicts keys that have gone silent so memory stays bounded.
        """
        now   = time.time()
        win   = self._windows[(key, window_seconds)]
        edge  = now - window_seconds

        while win and win[0] <= edge:
            win.popleft()

        allowed = len(win) < limit
        if allowed:
            win.append(now)

        remaining = max(0, limit - len(win))
        reset_at  = int(win[0] + window_seconds) if win else int(now + window_seconds)

        if now - self._last_sweep > 60:
            self._sweep(edge)
            self._last_sweep = now

        return allowed, remaining, reset_at

    def _sweep(self, edge: float) -> None:
        stale = [
            k for k, w in self._windows.items()
            if not w or w[-1] <= edge
        ]
        for k in stale:
            del self._windows[k]


_limiter = SlidingWindowLimiter()


def _scope_for(path: str, method: str) -> Tuple[str, int]:
    if path.startswith(_AUTH_PREFIXES):
        return "auth", settings.RATE_LIMIT_AUTH_PER_MINUTE
    if path.startswith(_IOT_PREFIXES):
        return "iot", settings.RATE_LIMIT_IOT_PER_MINUTE
    if method in _WRITE_METHODS:
        return "write", settings.RATE_LIMIT_WRITE_PER_MINUTE
    return "default", settings.RATE_LIMIT_DEFAULT_PER_MINUTE


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Proxies (Railway included) append the real client address first.
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def register_rate_limiter(app: FastAPI) -> None:
    """Attach the middleware to the app. No-op when RATE_LIMIT_ENABLED is false."""

    if not settings.RATE_LIMIT_ENABLED:
        return

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path, method = request.url.path, request.method

        # Health probes and docs are infrastructure surfaces, not API traffic.
        if path in ("/", "/health", "/docs", "/redoc", "/openapi.json"):
            return await call_next(request)

        scope, limit = _scope_for(path, method)
        allowed, remaining, reset_at = _limiter.hit(
            f"{_client_ip(request)}:{scope}", limit, 60
        )

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Slow down and try again shortly.",
                    "scope": scope,
                },
                headers={
                    _RETRY_AFTER_HEADER: str(max(1, reset_at - int(time.time()))),
                    _RATE_LIMIT_HEADER: str(limit),
                    _RATE_LIMIT_REMAINING_HEADER: "0",
                    _RATE_LIMIT_RESET_HEADER: str(reset_at),
                },
            )

        response = await call_next(request)
        response.headers[_RATE_LIMIT_HEADER] = str(limit)
        response.headers[_RATE_LIMIT_REMAINING_HEADER] = str(remaining)
        response.headers[_RATE_LIMIT_RESET_HEADER] = str(reset_at)
        return response
