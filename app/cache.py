from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import zlib
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

from pymemcache.client.base import PooledClient

from app.config import CACHE_ENABLED, MEMCACHE_HOST, MEMCACHE_PORT
from app.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# TTL constants (module-level — pure data)
# ---------------------------------------------------------------------------

TTL_HOME_DEFAULT_SECONDS    = 2 * 60
TTL_HOME_NONDEFAULT_SECONDS = 15 * 60
TTL_TRENDING_SECONDS        = 1 * 60
TTL_BOOK_SECONDS            = 6 * 60 * 60
TTL_AUTHOR_BIO_SECONDS      = 24 * 60 * 60
TTL_AUTHOR_CATALOG_SECONDS  = 1 * 60 * 60
TTL_LANG_OPTIONS_SECONDS    = 24 * 60 * 60

<<<<<<< HEAD
=======
# Stale-while-revalidate windows: after fresh_ttl elapses, served value is
# returned immediately and a background refresh kicks off. stale_ttl caps how
# long the stale value survives if no traffic triggers refresh.
TTL_HOME_DEFAULT_STALE_SECONDS = 30 * 60
TTL_TRENDING_STALE_SECONDS     = 10 * 60

>>>>>>> main
LANG_OPTIONS_KEY = "opds:lang_options"

_COMPRESSION_THRESHOLD = 10240  # 10 KB


# ---------------------------------------------------------------------------
# Pure helpers (module-level — no state)
# ---------------------------------------------------------------------------

def _jitter(ttl: int, pct: float = 0.10) -> int:
    delta = int(ttl * pct)
    return ttl + random.randint(-delta, delta)


def make_key(endpoint: str, params: dict[str, Any]) -> str:
    """Build opds:{endpoint}:{sha1(sorted_params)} — always include `access` param for security."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha1(canonical.encode()).hexdigest()
    return f"opds:{endpoint}:{digest}"


# ---------------------------------------------------------------------------
# CacheBackend Protocol (interface)
# ---------------------------------------------------------------------------

@runtime_checkable
class CacheBackend(Protocol):
    """Minimal interface all cache backends must satisfy."""

    def get(self, key: str) -> dict | None: ...

    def set(self, key: str, value: dict, ttl: int) -> None: ...

    async def cached(
        self,
        key: str,
        ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
    ) -> dict: ...

<<<<<<< HEAD
=======
    async def cached_swr(
        self,
        key: str,
        fresh_ttl: int,
        stale_ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
    ) -> dict: ...

>>>>>>> main

# ---------------------------------------------------------------------------
# NullCacheBackend — no-op, used when CACHE_ENABLED=false or in tests
# ---------------------------------------------------------------------------

class NullCacheBackend:
    """No-op cache backend. All reads miss; writes are discarded. fetch() always called."""

    def get(self, key: str) -> dict | None:
        return None

    def set(self, key: str, value: dict, ttl: int) -> None:
        return

    async def cached(
        self,
        key: str,
        ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
    ) -> dict:
        return await fetch()

<<<<<<< HEAD
=======
    async def cached_swr(
        self,
        key: str,
        fresh_ttl: int,
        stale_ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
    ) -> dict:
        return await fetch()

>>>>>>> main

# ---------------------------------------------------------------------------
# MemcachedBackend — production backend with stampede protection
# ---------------------------------------------------------------------------

class MemcachedBackend:
    """Memcached-backed cache with two-layer stampede protection.

    Layer 1 — asyncio.Lock: coalesces concurrent coroutines within one worker.
    Layer 2 — Memcached add-lock: prevents multiple worker processes from
               fetching the same key simultaneously on expiry.
    """

    def __init__(self) -> None:
        self._client: PooledClient | None = None
        self._client_failed: bool = False
        self._locks: dict[str, asyncio.Lock] = {}
<<<<<<< HEAD
=======
        self._refreshing: set[str] = set()
>>>>>>> main

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> PooledClient | None:
        if self._client is not None:
            return self._client
<<<<<<< HEAD
=======
        logger.info("memcached connecting to %s:%s", MEMCACHE_HOST, MEMCACHE_PORT)
>>>>>>> main
        try:
            self._client = PooledClient(
                (MEMCACHE_HOST, MEMCACHE_PORT),
                max_pool_size=10,
                connect_timeout=1.0,
                timeout=1.0,
                ignore_exc=True,
            )
            return self._client
        except Exception as exc:
            if not self._client_failed:
                logger.warning(
                    "Memcached unavailable (%s:%s): %s — running without cache",
                    MEMCACHE_HOST, MEMCACHE_PORT, exc,
                )
                self._client_failed = True
            return None

    def _serialize(self, value: dict) -> bytes:
        raw = json.dumps(value, separators=(",", ":")).encode()
        if len(raw) > _COMPRESSION_THRESHOLD:
            return b"z:" + zlib.compress(raw, level=6)
        return b"j:" + raw

    def _deserialize(self, data: bytes) -> dict | None:
        try:
            if data.startswith(b"z:"):
                return json.loads(zlib.decompress(data[2:]))
            if data.startswith(b"j:"):
                return json.loads(data[2:])
        except Exception:
            pass
        return None

    def _acquire_distributed_lock(self, key: str, expire: int = 30) -> bool:
        """Try to acquire a cross-process lock via Memcached add. Returns True if acquired."""
        client = self._get_client()
        if client is None:
            return True  # no Memcached → act as if we own the lock
        try:
            return bool(client.add(f"{key}:lock", b"1", expire=expire))
        except Exception:
            return True  # unexpected failure → fall through, accept possible double-fetch

    def _release_distributed_lock(self, key: str) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            client.delete(f"{key}:lock")
        except Exception:
            pass  # TTL will clean it up

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get(self, key: str) -> dict | None:
<<<<<<< HEAD
        try:
            client = self._get_client()
            if client is None:
                return None
            raw = client.get(key)
            if raw is None:
                logger.info("cache MISS key=%s", key)
                return None
            result = self._deserialize(raw)
            if result is None:
                logger.error("cache decode error key=%s", key)
                return None
            logger.info("cache HIT key=%s", key)
            return result
        except Exception as exc:
            logger.warning("cache get failed key=%s: %s", key, exc)
            return None

    def set(self, key: str, value: dict, ttl: int) -> None:
        try:
            client = self._get_client()
            if client is None:
                return
            client.set(key, self._serialize(value), expire=_jitter(ttl))
        except Exception as exc:
            logger.warning("cache set failed key=%s: %s", key, exc)
=======
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(key)
        except Exception as exc:
            logger.warning("cache get error key=%s: %s", key, exc)
            return None
        if raw is None:
            logger.info("cache MISS key=%s", key)
            return None
        result = self._deserialize(raw)
        if result is None:
            logger.error("cache decode error key=%s", key)
            return None
        logger.info("cache HIT key=%s", key)
        return result

    def set(self, key: str, value: dict, ttl: int) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            client.set(key, self._serialize(value), expire=_jitter(ttl))
        except Exception as exc:
            logger.warning("cache set error key=%s: %s", key, exc)
>>>>>>> main

    async def cached(
        self,
        key: str,
        ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
    ) -> dict:
<<<<<<< HEAD
        fetch_started = False
=======
>>>>>>> main
        try:
            result = self.get(key)
            if result is not None:
                return result

            async with self._locks.setdefault(key, asyncio.Lock()):
                result = self.get(key)
                if result is not None:
                    return result

                got_distributed_lock = self._acquire_distributed_lock(key)
                if not got_distributed_lock:
                    # Another worker holds the lock — poll until it fills the cache or times out.
                    # 15 × 200ms = 3s max wait, covers typical OL API response times.
                    for _ in range(15):
                        await asyncio.sleep(0.2)
                        result = self.get(key)
                        if result is not None:
                            return result
                    # Lock holder took >3s or crashed — fall through and fetch.

                try:
<<<<<<< HEAD
                    fetch_started = True
                    result = await fetch()
                finally:
                    if got_distributed_lock:
                        self._release_distributed_lock(key)
                self.set(key, result, ttl)
                return result
        except Exception as exc:
            if fetch_started:
                raise  # real upstream error — let it propagate
            logger.warning("cache layer failed key=%s, bypassing: %s", key, exc)
=======
                    result = await fetch()
                    self.set(key, result, ttl)
                    return result
                finally:
                    if got_distributed_lock:
                        self._release_distributed_lock(key)
        except Exception as exc:
            logger.warning("cache layer failed key=%s: %s — fetching uncached", key, exc)
            return await fetch()

    # ------------------------------------------------------------------
    # Stale-while-revalidate
    # ------------------------------------------------------------------

    def _set_swr(self, key: str, value: dict, fresh_ttl: int, stale_ttl: int) -> None:
        self.set(key, {"v": value, "exp": time.time() + fresh_ttl}, stale_ttl)

    async def _refresh_in_background(
        self,
        key: str,
        fresh_ttl: int,
        stale_ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
    ) -> None:
        try:
            if not self._acquire_distributed_lock(key, expire=max(fresh_ttl, 30)):
                return
            try:
                fresh = await fetch()
                self._set_swr(key, fresh, fresh_ttl, stale_ttl)
            finally:
                self._release_distributed_lock(key)
        except Exception as exc:
            logger.warning("cache swr refresh failed key=%s: %s", key, exc)
        finally:
            self._refreshing.discard(key)

    async def cached_swr(
        self,
        key: str,
        fresh_ttl: int,
        stale_ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
    ) -> dict:
        """Stale-while-revalidate. Hot/stale hits return immediately; stale hits
        spawn a background refresh. Misses block-fetch with stampede protection.
        """
        try:
            wrapped = self.get(key)
            if isinstance(wrapped, dict) and "v" in wrapped:
                if time.time() < wrapped.get("exp", 0):
                    return wrapped["v"]
                if key not in self._refreshing:
                    self._refreshing.add(key)
                    asyncio.create_task(
                        self._refresh_in_background(key, fresh_ttl, stale_ttl, fetch)
                    )
                return wrapped["v"]

            async with self._locks.setdefault(key, asyncio.Lock()):
                wrapped = self.get(key)
                if isinstance(wrapped, dict) and "v" in wrapped:
                    return wrapped["v"]

                got_lock = self._acquire_distributed_lock(key)
                if not got_lock:
                    for _ in range(15):
                        await asyncio.sleep(0.2)
                        wrapped = self.get(key)
                        if isinstance(wrapped, dict) and "v" in wrapped:
                            return wrapped["v"]

                try:
                    result = await fetch()
                    self._set_swr(key, result, fresh_ttl, stale_ttl)
                    return result
                finally:
                    if got_lock:
                        self._release_distributed_lock(key)
        except Exception as exc:
            logger.warning("cache swr failed key=%s: %s — fetching uncached", key, exc)
>>>>>>> main
            return await fetch()


# ---------------------------------------------------------------------------
# Module-level singleton + FastAPI dependency
# ---------------------------------------------------------------------------

_backend: CacheBackend = MemcachedBackend() if CACHE_ENABLED else NullCacheBackend()


def get_cache() -> CacheBackend:
    """FastAPI dependency — returns the active cache backend singleton."""
    return _backend
