from __future__ import annotations

import asyncio
import hashlib
import json
import random
import zlib
from typing import Any, Callable, Coroutine

from pymemcache.client.base import PooledClient

from app.config import CACHE_ENABLED, MEMCACHE_HOST, MEMCACHE_PORT
from app.logger import get_logger

logger = get_logger(__name__)

_client: PooledClient | None = None
_client_failed: bool = False
_locks: dict[str, asyncio.Lock] = {}

TTL_HOME_DEFAULT    = 30 * 60
TTL_HOME_NONDEFAULT = 15 * 60
TTL_TRENDING        = 5 * 60
TTL_BOOK            = 6 * 60 * 60
TTL_AUTHOR_BIO      = 24 * 60 * 60
TTL_AUTHOR_CATALOG  = 1 * 60 * 60
TTL_SEARCH          = 5 * 60
TTL_LANG_OPTIONS    = 24 * 60 * 60
TTL_NOT_FOUND       = 2 * 60

LANG_OPTIONS_KEY = "opds:lang_options"

_COMPRESSION_THRESHOLD = 10240  # 10 KB


def _jitter(ttl: int, pct: float = 0.10) -> int:
    delta = int(ttl * pct)
    return ttl + random.randint(-delta, delta)


def make_key(endpoint: str, params: dict[str, Any]) -> str:
    """Build opds:{endpoint}:{sha1(sorted_params)} — always include `access` param for security."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha1(canonical.encode()).hexdigest()
    return f"opds:{endpoint}:{digest}"


def _get_client() -> PooledClient | None:
    global _client, _client_failed
    if not CACHE_ENABLED:
        return None
    if _client is not None:
        return _client
    try:
        _client = PooledClient(
            (MEMCACHE_HOST, MEMCACHE_PORT),
            max_pool_size=10,
            connect_timeout=1.0,
            timeout=1.0,
            ignore_exc=True,
        )
        return _client
    except Exception as exc:
        if not _client_failed:
            logger.warning(
                "Memcached unavailable (%s:%s): %s — running without cache",
                MEMCACHE_HOST, MEMCACHE_PORT, exc,
            )
            _client_failed = True
        return None


def _serialize(value: dict) -> bytes:
    raw = json.dumps(value, separators=(",", ":")).encode()
    if len(raw) > _COMPRESSION_THRESHOLD:
        return b"z:" + zlib.compress(raw, level=6)
    return b"j:" + raw


def _deserialize(data: bytes) -> dict | None:
    try:
        if data.startswith(b"z:"):
            return json.loads(zlib.decompress(data[2:]))
        if data.startswith(b"j:"):
            return json.loads(data[2:])
    except Exception:
        pass
    return None


def cache_get(key: str) -> dict | None:
    client = _get_client()
    if client is None:
        return None
    raw = client.get(key)
    if raw is None:
        logger.info("cache MISS key=%s", key)
        return None
    result = _deserialize(raw)
    if result is None:
        logger.info("cache MISS (decode error) key=%s", key)
        return None
    logger.info("cache HIT key=%s", key)
    return result


def cache_set(key: str, value: dict, ttl: int) -> None:
    client = _get_client()
    if client is None:
        return
    client.set(key, _serialize(value), expire=_jitter(ttl))


def _acquire_distributed_lock(key: str, expire: int = 30) -> bool:
    """Try to acquire a cross-process lock via Memcached add. Returns True if lock acquired."""
    client = _get_client()
    if client is None:
        return True  # no Memcached → act as if we own the lock (single-process fallback)
    try:
        return bool(client.add(f"{key}:lock", b"1", expire=expire))
    except Exception:
        return True  # add failed unexpectedly → fall through, accept possible double-fetch


def _release_distributed_lock(key: str) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.delete(f"{key}:lock")
    except Exception:
        pass  # TTL will clean it up


async def cached(
    key: str,
    ttl: int,
    fetch: Callable[[], Coroutine[Any, Any, dict]],
) -> dict:
    """Cache-aside with two-layer stampede protection.

    Layer 1 — asyncio.Lock: coalesces concurrent coroutines within one worker process.
    Layer 2 — Memcached add-lock: prevents multiple worker processes from
               simultaneously fetching the same key on expiry.

    On distributed lock contention: waits 100ms, re-checks cache (other worker
    likely filled it). If still cold (first-ever cold start), falls through and
    fetches anyway — acceptable single double-fetch on initial startup.

    Exceptions from fetch() propagate before cache_set() so errors (404s) are
    never cached.
    """
    result = cache_get(key)
    if result is not None:
        return result

    async with _locks.setdefault(key, asyncio.Lock()):
        result = cache_get(key)
        if result is not None:
            return result

        got_distributed_lock = _acquire_distributed_lock(key)
        if not got_distributed_lock:
            # Another worker holds the lock — poll until it fills the cache or times out.
            # 15 × 200ms = 3s max wait, covers typical OL API response times.
            for _ in range(15):
                await asyncio.sleep(0.2)
                result = cache_get(key)
                if result is not None:
                    return result
            # Lock holder took >3s or crashed (lock will TTL-expire at 30s).
            # Fall through and fetch — accepts one duplicate call per timed-out waiter.

        try:
            result = await fetch()
            cache_set(key, result, ttl)
            return result
        finally:
            if got_distributed_lock:
                _release_distributed_lock(key)
