"""Redis-backed cache and counters with an in-memory fallback.

Every Redis call is bounded by a small connection pool and short timeouts, and
any Redis failure degrades to the in-process store instead of failing a request.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any

from . import config

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover
    aioredis = None

_MEMORY_MAX = 2000


class Store:
    def __init__(self) -> None:
        self._redis = None
        self._redis_down_until = 0.0
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._counters: dict[str, dict[str, int]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        if config.REDIS_URL and aioredis is not None:
            pool = aioredis.BlockingConnectionPool.from_url(
                config.REDIS_URL,
                max_connections=config.REDIS_MAX_CONNECTIONS,
                timeout=1.0,
                socket_timeout=1.5,
                socket_connect_timeout=1.5,
                decode_responses=True,
            )
            self._redis = aioredis.Redis(connection_pool=pool)

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def _redis_ok(self) -> bool:
        return self._redis is not None and time.monotonic() >= self._redis_down_until

    def _redis_failed(self) -> None:
        # Back off for a few seconds so a sick Redis does not add latency to every request.
        self._redis_down_until = time.monotonic() + 5.0

    async def get_json(self, key: str) -> Any | None:
        if self._redis_ok():
            try:
                raw = await self._redis.get(key)
                return json.loads(raw) if raw else None
            except Exception:
                self._redis_failed()
        entry = self._cache.get(key)
        if entry and entry[0] > time.time():
            return json.loads(entry[1])
        return None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        raw = json.dumps(value, separators=(",", ":"))
        if self._redis_ok():
            try:
                await self._redis.set(key, raw, ex=ttl)
                return
            except Exception:
                self._redis_failed()
        self._cache[key] = (time.time() + ttl, raw)
        self._cache.move_to_end(key)
        while len(self._cache) > _MEMORY_MAX:
            self._cache.popitem(last=False)

    async def incr(self, bucket: str, field: str, amount: int = 1) -> None:
        """Increment a counter in today's hash and in the all-time hash."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        keys = (f"axg2:{bucket}:total", f"axg2:{bucket}:{day}")
        if self._redis_ok():
            try:
                pipe = self._redis.pipeline(transaction=False)
                for key in keys:
                    pipe.hincrby(key, field, amount)
                pipe.expire(keys[1], 120 * 86400)
                await pipe.execute()
                return
            except Exception:
                self._redis_failed()
        for key in keys:
            counters = self._counters.setdefault(key, {})
            counters[field] = counters.get(field, 0) + amount

    async def read_counters(self, bucket: str, days: int = 14) -> dict[str, dict[str, int]]:
        names = ["total"] + [time.strftime("%Y-%m-%d", time.gmtime(time.time() - d * 86400)) for d in range(days)]
        out: dict[str, dict[str, int]] = {}
        for name in names:
            key = f"axg2:{bucket}:{name}"
            values: dict[str, Any] = {}
            if self._redis_ok():
                try:
                    values = await self._redis.hgetall(key)
                except Exception:
                    self._redis_failed()
                    values = self._counters.get(key, {})
            else:
                values = self._counters.get(key, {})
            if values:
                out[name] = {k: int(v) for k, v in values.items()}
        return out

    async def push_event(self, key: str, event: dict[str, Any], keep: int = 200) -> None:
        raw = json.dumps(event, separators=(",", ":"))
        if self._redis_ok():
            try:
                pipe = self._redis.pipeline(transaction=False)
                pipe.lpush(f"axg2:{key}", raw)
                pipe.ltrim(f"axg2:{key}", 0, keep - 1)
                await pipe.execute()
                return
            except Exception:
                self._redis_failed()
        events = self._events.setdefault(key, [])
        events.insert(0, event)
        del events[keep:]

    async def read_events(self, key: str, limit: int = 50) -> list[dict[str, Any]]:
        if self._redis_ok():
            try:
                return [json.loads(r) for r in await self._redis.lrange(f"axg2:{key}", 0, limit - 1)]
            except Exception:
                self._redis_failed()
        return self._events.get(key, [])[:limit]

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False


store = Store()


class RateLimiter:
    """Fixed-window per-key limiter kept in process memory (single replica)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._window = 0
        self._hits: dict[str, int] = {}

    def allow(self, key: str) -> bool:
        window = int(time.time() // 60)
        if window != self._window:
            self._window = window
            self._hits = {}
        count = self._hits.get(key, 0) + 1
        self._hits[key] = count
        return count <= self.per_minute
