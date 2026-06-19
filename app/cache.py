from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import zlib
from contextvars import ContextVar
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

from pymemcache.client.base import PooledClient

from app.config import CACHE_ENABLED, MEMCACHE_HOST, MEMCACHE_PORT
from app.logger import get_logger

logger = get_logger(__name__)

# Records whether the most recent cache resolve served a stored value ("HIT") or
# ran the fetch/build ("MISS"). Routes read it after resolving the *main*
# resource to set the X-Cache response header. Per-request via contextvar.
_cache_status: ContextVar[str] = ContextVar("opds_cache_status", default="")


def last_cache_status() -> str:
    """Outcome of the most recent cache resolve in this request: HIT or MISS."""
    return _cache_status.get()

# ---------------------------------------------------------------------------
# TTL constants (module-level — pure data)
# ---------------------------------------------------------------------------

TTL_HOME_DEFAULT_SECONDS    = 10 * 60
TTL_HOME_NONDEFAULT_SECONDS = 15 * 60
# Trending Books carousel — kept on its own SHORT-TTL key and injected into the
# home feed per request. Trending titles get borrowed fast, so a 60s SWR keeps
# the row biased toward currently-available books even while the rest of the
# (slow-changing) home feed stays long-cached.
TTL_TRENDING_SECONDS        = 1 * 60
# Featured subject feeds (Art, Sci-Fi, …) — the home nav links. Bounded set,
# warmed after home; carousels change slowly so a longer TTL is fine.
TTL_SUBJECT_SECONDS         = 30 * 60
TTL_SUBJECT_STALE_SECONDS   = 60 * 60
# Generic /search responses (free-text queries + facet/availability/media/access
# filters). Cached lazily on first hit so repeats + facet navigation serve from
# cache; memcached LRU + TTL bound the (unbounded) key space.
TTL_SEARCH_SECONDS          = 15 * 60
TTL_SEARCH_STALE_SECONDS    = 30 * 60
TTL_BOOK_SECONDS            = 6 * 60 * 60
TTL_AUTHOR_BIO_SECONDS      = 24 * 60 * 60
TTL_AUTHOR_CATALOG_SECONDS  = 1 * 60 * 60
TTL_LANG_OPTIONS_SECONDS    = 7 * 24 * 60 * 60   # 7 days
# Language facet counts (ebook_edition_count per language) come from
# ``languages.json`` and change glacially — at most a few new languages or
# count jumps per week. 7 days is plenty; users still get fresh counts well
# within a typical content-rotation cycle.
TTL_LANG_COUNTS_SECONDS     = 7 * 24 * 60 * 60

# Stale-while-revalidate windows: after fresh_ttl elapses, served value is
# returned immediately and a background refresh kicks off. stale_ttl caps how
# long the stale value survives if no traffic triggers refresh.
TTL_HOME_DEFAULT_STALE_SECONDS = 30 * 60
TTL_TRENDING_STALE_SECONDS     = 10 * 60

LANG_OPTIONS_KEY = "opds:lang_options"
# Per-language ebook counts come from the global languages.json (independent of
# query/mode/media_type/access), so they share one stable key across every
# home and search request — fetched once, reused everywhere.
LANG_COUNTS_KEY = "opds:lang_counts"

_COMPRESSION_THRESHOLD = 10240  # 10 KB

# Backoff window after a memcached op fails. Prevents per-request reconnect storms
# when memcached is down. New connection attempts gated by monotonic clock.
_MEMCACHE_RECONNECT_BACKOFF_SECONDS = 5.0

# When another process holds the build lock, wait this long for its result
# before rebuilding ourselves. Must comfortably exceed a typical build (home
# builds take tens of seconds) and roughly match the distributed-lock lifetime,
# or workers stampede and each rebuild the same key — multiplying OL load.
_STAMPEDE_MAX_WAIT_SECONDS = 30.0
_STAMPEDE_POLL_INTERVAL_SECONDS = 0.5


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
        is_valid: Callable[[dict], bool] | None = None,
    ) -> dict: ...

    async def cached_swr(
        self,
        key: str,
        fresh_ttl: int,
        stale_ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
        is_valid: Callable[[dict], bool] | None = None,
    ) -> dict: ...

    def acquire_or_renew_lease(self, name: str, ttl: int, owner: str) -> bool: ...


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
        is_valid: Callable[[dict], bool] | None = None,
    ) -> dict:
        _cache_status.set("MISS")
        return await fetch()

    async def cached_swr(
        self,
        key: str,
        fresh_ttl: int,
        stale_ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
        is_valid: Callable[[dict], bool] | None = None,
    ) -> dict:
        _cache_status.set("MISS")
        return await fetch()

    def acquire_or_renew_lease(self, name: str, ttl: int, owner: str) -> bool:
        # Single-process / no cache → always the leader.
        return True


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
        self._reconnect_after: float = 0.0
        self._locks: dict[str, asyncio.Lock] = {}
        self._refreshing: set[str] = set()
        # Strong refs to in-flight background refresh tasks. asyncio only keeps
        # weak refs, so an un-held task may be GC'd mid-run, silently killing the
        # recompute. Hold each until it completes (cleared via done-callback).
        self._refresh_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> PooledClient | None:
        if self._client is not None:
            return self._client
        if time.monotonic() < self._reconnect_after:
            return None
        logger.info("memcached connecting to %s:%s", MEMCACHE_HOST, MEMCACHE_PORT)
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
            self._invalidate_client(exc)
            return None

    def _invalidate_client(self, exc: BaseException) -> None:
        """Drop the cached client and arm reconnect backoff after an op failure.

        ``PooledClient(ignore_exc=True)`` does not catch raw socket errors like
        ``ConnectionRefusedError`` raised by ``_connect``. When memcached is
        unreachable, every request retries the same broken pool. Resetting the
        client forces a rebuild after the backoff window — calls in between
        skip memcached and run uncached.
        """
        was_active = self._client is not None
        self._client = None
        self._reconnect_after = time.monotonic() + _MEMCACHE_RECONNECT_BACKOFF_SECONDS
        if was_active:
            logger.warning(
                "Memcached op failed (%s:%s): %s — disabling cache for %.1fs",
                MEMCACHE_HOST, MEMCACHE_PORT, exc, _MEMCACHE_RECONNECT_BACKOFF_SECONDS,
            )

    def _serialize(self, value: dict) -> bytes:
        raw = json.dumps(value, separators=(",", ":"), default=str).encode()
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
        except Exception as exc:
            self._invalidate_client(exc)
            return True  # unexpected failure → fall through, accept possible double-fetch

    def _release_distributed_lock(self, key: str) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            client.delete(f"{key}:lock")
        except Exception as exc:
            self._invalidate_client(exc)  # TTL will clean up the lock entry

    def acquire_or_renew_lease(self, name: str, ttl: int, owner: str) -> bool:
        """Sticky single-leader lease via Memcached. Returns True if ``owner``
        holds the lease after this call.

        - absent  → ``add`` (race-safe; exactly one worker wins)
        - ours    → ``set`` to renew (keeps leadership across cycles)
        - other's → not leader

        The lease auto-expires after ``ttl``, so a dead leader is replaced. Used
        to elect ONE warmer among N workers (others stay idle). No Memcached →
        True (single-process/dev acts as the leader).
        """
        client = self._get_client()
        if client is None:
            return True
        lease_key = f"lease:{name}"
        owner_b = owner.encode()
        try:
            current = client.get(lease_key)
            if current is None:
                return bool(client.add(lease_key, owner_b, expire=ttl))
            if current == owner_b:
                client.set(lease_key, owner_b, expire=ttl)
                return True
            return False
        except Exception as exc:
            self._invalidate_client(exc)
            return True

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get(self, key: str) -> dict | None:
        # HIT/MISS are logged at DEBUG (high-volume, per-request); INFO is reserved
        # for meaningful events (builds, warm summary, errors).
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(key)
        except Exception as exc:
            logger.warning("cache get error key=%s: %s", key, exc)
            self._invalidate_client(exc)
            return None
        if raw is None:
            logger.debug("cache MISS key=%s", key)
            return None
        result = self._deserialize(raw)
        if result is None:
            logger.error("cache decode error key=%s", key)
            return None
        logger.debug("cache HIT key=%s", key)
        return result

    def set(self, key: str, value: dict, ttl: int) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            client.set(key, self._serialize(value), expire=_jitter(ttl))
        except Exception as exc:
            logger.warning("cache set error key=%s: %s", key, exc)
            self._invalidate_client(exc)

    def _delete(self, key: str) -> None:
        """Best-effort delete used to evict invalid cache entries."""
        client = self._get_client()
        if client is None:
            return
        try:
            client.delete(key)
        except Exception as exc:
            logger.warning("cache delete error key=%s: %s", key, exc)
            self._invalidate_client(exc)

    async def cached(
        self,
        key: str,
        ttl: int,
        fetch: Callable[[], Coroutine[Any, Any, dict]],
        is_valid: Callable[[dict], bool] | None = None,
    ) -> dict:
        """Cache-or-fetch with optional value validation.

        When ``is_valid`` is supplied it gates both cache reads and writes:
        a HIT that fails validation is treated as a MISS (and discarded so
        future requests don't keep returning the poisoned entry), and a
        fetched value that fails validation is returned to the caller but
        never written to memcached. Prevents blank/error responses (e.g.
        ``groups=[]`` from a transient upstream outage) from being pinned
        for the full TTL.
        """

        def _valid(v: dict) -> bool:
            return is_valid is None or is_valid(v)

        _cache_status.set("MISS")
        try:
            result = self.get(key)
            if result is not None and _valid(result):
                _cache_status.set("HIT")
                return result
            if result is not None:
                logger.info("cache invalid entry, discarding key=%s", key)
                self._delete(key)

            async with self._locks.setdefault(key, asyncio.Lock()):
                result = self.get(key)
                if result is not None and _valid(result):
                    _cache_status.set("HIT")
                    return result
                if result is not None:
                    self._delete(key)

                got_distributed_lock = self._acquire_distributed_lock(key)
                if not got_distributed_lock:
                    # Another worker holds the lock — wait for its result rather
                    # than refetching. Poll up to the lock's lifetime (some OL
                    # calls take many seconds); a short poll would expire mid-
                    # fetch and make every worker refetch the same key.
                    polls = int(_STAMPEDE_MAX_WAIT_SECONDS / _STAMPEDE_POLL_INTERVAL_SECONDS)
                    for _ in range(polls):
                        await asyncio.sleep(_STAMPEDE_POLL_INTERVAL_SECONDS)
                        result = self.get(key)
                        if result is not None and _valid(result):
                            logger.info("cache served peer fetch key=%s", key)
                            _cache_status.set("HIT")
                            return result
                    # Lock holder crashed or never produced — fall through and fetch.

                try:
                    result = await fetch()
                    if _valid(result):
                        self.set(key, result, ttl)
                    else:
                        logger.info("cache skip write (invalid result) key=%s", key)
                    # Set after fetch so a nested cached() call inside fetch can't
                    # leave a stale HIT marker for this (built) resource.
                    _cache_status.set("MISS")
                    return result
                finally:
                    if got_distributed_lock:
                        self._release_distributed_lock(key)
        except Exception as exc:
            logger.warning("cache layer failed key=%s: %s — fetching uncached", key, exc)
            result = await fetch()
            _cache_status.set("MISS")
            return result

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
        is_valid: Callable[[dict], bool] | None = None,
    ) -> None:
        try:
            if not self._acquire_distributed_lock(key, expire=max(fresh_ttl, 30)):
                return
            try:
                fresh = await fetch()
                if is_valid is None or is_valid(fresh):
                    self._set_swr(key, fresh, fresh_ttl, stale_ttl)
                else:
                    logger.info("cache swr skip write (invalid refresh) key=%s", key)
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
        is_valid: Callable[[dict], bool] | None = None,
    ) -> dict:
        """Stale-while-revalidate. Hot/stale hits return immediately; stale hits
        spawn a background refresh. Misses block-fetch with stampede protection.

        When ``is_valid`` is supplied, cache entries failing validation are
        discarded on read and never written, so blank responses cannot poison
        the cache for the full TTL.
        """

        def _valid(v: dict) -> bool:
            return is_valid is None or is_valid(v)

        _cache_status.set("MISS")
        try:
            wrapped = self.get(key)
            if isinstance(wrapped, dict) and "v" in wrapped:
                if _valid(wrapped["v"]):
                    _cache_status.set("HIT")
                    if time.time() < wrapped.get("exp", 0):
                        return wrapped["v"]
                    if key not in self._refreshing:
                        self._refreshing.add(key)
                        task = asyncio.create_task(
                            self._refresh_in_background(key, fresh_ttl, stale_ttl, fetch, is_valid)
                        )
                        self._refresh_tasks.add(task)
                        task.add_done_callback(self._refresh_tasks.discard)
                    return wrapped["v"]
                # Cached value is invalid (e.g. previously-poisoned blank entry) — evict and refetch.
                logger.info("cache swr invalid entry, discarding key=%s", key)
                self._delete(key)

            async with self._locks.setdefault(key, asyncio.Lock()):
                wrapped = self.get(key)
                if isinstance(wrapped, dict) and "v" in wrapped and _valid(wrapped["v"]):
                    _cache_status.set("HIT")
                    return wrapped["v"]
                if isinstance(wrapped, dict) and "v" in wrapped:
                    self._delete(key)

                got_lock = self._acquire_distributed_lock(key)
                if not got_lock:
                    # Another process is building this key. Wait for its result
                    # rather than rebuilding — builds can take tens of seconds,
                    # so poll up to the lock's lifetime before giving up (a short
                    # poll would expire mid-build and cause every worker to
                    # redundantly rebuild). Quiet gets keep the log clean.
                    polls = int(_STAMPEDE_MAX_WAIT_SECONDS / _STAMPEDE_POLL_INTERVAL_SECONDS)
                    for _ in range(polls):
                        await asyncio.sleep(_STAMPEDE_POLL_INTERVAL_SECONDS)
                        wrapped = self.get(key)
                        if isinstance(wrapped, dict) and "v" in wrapped and _valid(wrapped["v"]):
                            logger.info("cache swr served peer build key=%s", key)
                            _cache_status.set("HIT")
                            return wrapped["v"]

                try:
                    result = await fetch()
                    if _valid(result):
                        self._set_swr(key, result, fresh_ttl, stale_ttl)
                    else:
                        logger.info("cache swr skip write (invalid result) key=%s", key)
                    # Set after fetch so a nested cached() call inside fetch can't
                    # leave a stale HIT marker for this (built) resource.
                    _cache_status.set("MISS")
                    return result
                finally:
                    if got_lock:
                        self._release_distributed_lock(key)
        except Exception as exc:
            logger.warning("cache swr failed key=%s: %s — fetching uncached", key, exc)
            result = await fetch()
            _cache_status.set("MISS")
            return result


# ---------------------------------------------------------------------------
# Module-level singleton + FastAPI dependency
# ---------------------------------------------------------------------------

_backend: CacheBackend = MemcachedBackend() if CACHE_ENABLED else NullCacheBackend()


def get_cache() -> CacheBackend:
    """FastAPI dependency — returns the active cache backend singleton."""
    return _backend
