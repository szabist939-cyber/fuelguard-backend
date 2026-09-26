"""
In-process TTL cache with single-flight deduplication.

Purpose-built for this deployment (single uvicorn process on Railway) instead
of bolting on a Redis server: every cached value is one Firestore round-trip
saved on the hot paths — station search, dashboard KPIs, nozzle monitoring and
the report charts are all read several times per page view.

Design notes
------------
- Sliding TTL per key with jitter so a mass of keys written together never
  expire together (avoids thundering-herd refills after quiet periods).
- get_or_set is single-flight: concurrent requests for a cold key share one
  loader invocation instead of stampeding Firestore with identical queries.
- Invalidation is tag-based: mutations call invalidate("transactions") after a
  write, and every key registered under that tag drops immediately.
- The interface mirrors a Redis-style get/set/delete surface so swapping the
  backing store for a shared Redis in a horizontally scaled deployment is a
  contained change inside this module.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple


@dataclass
class _Entry:
    value: Any
    expires_at: float
    tag: str


class TTLCache:
    def __init__(self) -> None:
        self._store: Dict[str, _Entry] = {}
        self._inflight: Dict[str, float] = {}  # key -> start time (single-flight)
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    # ── Core operations ───────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        if time.monotonic() >= entry.expires_at:
            self._store.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return entry.value

    def set(self, key: str, value: Any, ttl_seconds: float, tag: str = "") -> None:
        # Jitter spreads expiry over ±10% so bulk writes never expire together.
        jittered = ttl_seconds * random.uniform(0.9, 1.1)
        self._store[key] = _Entry(
            value=value,
            expires_at=time.monotonic() + max(0.5, jittered),
            tag=tag,
        )

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def invalidate_tag(self, tag: str) -> int:
        """Drop every key registered under a tag. Returns the number dropped."""
        doomed = [k for k, e in self._store.items() if e.tag == tag]
        for k in doomed:
            self._store.pop(k, None)
        return len(doomed)

    def clear(self) -> None:
        self._store.clear()

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "entries": len(self._store),
            "inflight": len(self._inflight),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }

    def sweep(self) -> int:
        """Drop expired entries. Cheap, called opportunistically on miss."""
        now = time.monotonic()
        expired = [k for k, e in self._store.items() if now >= e.expires_at]
        for k in expired:
            self._store.pop(k, None)
        return len(expired)

    # ── Single-flight loader ──────────────────────────────────────────────────

    async def get_or_set(
        self,
        key: str,
        ttl_seconds: float,
        loader: Callable[[], Awaitable[Any]],
        tag: str = "",
    ) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        self.sweep()

        async with self._lock:
            # Another request may have populated the key while we waited.
            cached = self.get(key)
            if cached is not None:
                return cached
            already_running = key in self._inflight
            if not already_running:
                self._inflight[key] = time.monotonic()

        if already_running:
            # Piggyback: poll briefly for the winner's result instead of
            # issuing a duplicate loader.
            for _ in range(40):
                await asyncio.sleep(0.05)
                cached = self.get(key)
                if cached is not None:
                    return cached
            # Winner failed or is slow — fall through and load ourselves.
            async with self._lock:
                self._inflight.pop(key, None)

        try:
            value = await loader()
        finally:
            async with self._lock:
                self._inflight.pop(key, None)

        self.set(key, value, ttl_seconds, tag=tag)
        return value


cache = TTLCache()


# ── Tag vocabulary (one place so invalidation stays consistent) ───────────────

TAG_STATIONS     = "stations"
TAG_PRICES       = "prices"
TAG_TRANSACTIONS = "transactions"
TAG_FRAUD        = "fraud"
TAG_NOZZLES      = "nozzles"
TAG_DASHBOARD    = "dashboard"


def invalidate(*tags: str) -> int:
    """Invalidate cache tags after a write. Returns total keys dropped."""
    return sum(cache.invalidate_tag(t) for t in tags)
