"""Cross-worker stampede protection: a worker must wait for a peer's in-flight
build rather than rebuilding the same key (slow builds previously stampeded)."""
from __future__ import annotations

import asyncio

import app.cache as cache_mod
from app.cache import MemcachedBackend


def test_waits_for_peer_build_instead_of_rebuilding(monkeypatch):
    monkeypatch.setattr(cache_mod, "_STAMPEDE_MAX_WAIT_SECONDS", 2.0)
    monkeypatch.setattr(cache_mod, "_STAMPEDE_POLL_INTERVAL_SECONDS", 0.05)

    backend = MemcachedBackend()
    peer_value = {"groups": [{"x": 1}]}
    calls = {"get": 0, "fetch": 0}

    def fake_get(key, quiet=False):
        calls["get"] += 1
        # Cold for the first two reads (pre-lock + in-lock), then the peer's
        # build result appears while we poll.
        return {"v": peer_value} if calls["get"] >= 3 else None

    monkeypatch.setattr(backend, "get", fake_get)
    monkeypatch.setattr(backend, "_acquire_distributed_lock", lambda key, expire=30: False)
    monkeypatch.setattr(backend, "_release_distributed_lock", lambda key: None)

    async def fetch():
        calls["fetch"] += 1
        return {"groups": [{"y": 2}]}

    async def run():
        return await backend.cached_swr(
            "opds:home:x", 120, 1800, fetch, is_valid=lambda d: bool(d.get("groups"))
        )

    result = asyncio.run(run())
    assert result == peer_value
    assert calls["fetch"] == 0  # did NOT rebuild — used the peer's result


def test_builds_itself_if_peer_never_produces(monkeypatch):
    monkeypatch.setattr(cache_mod, "_STAMPEDE_MAX_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(cache_mod, "_STAMPEDE_POLL_INTERVAL_SECONDS", 0.05)

    backend = MemcachedBackend()
    monkeypatch.setattr(backend, "get", lambda key, quiet=False: None)  # value never appears
    monkeypatch.setattr(backend, "_acquire_distributed_lock", lambda key, expire=30: False)
    monkeypatch.setattr(backend, "_release_distributed_lock", lambda key: None)
    stored: dict = {}
    monkeypatch.setattr(backend, "_set_swr", lambda k, v, f, s: stored.update({k: v}))

    built = {"groups": [{"y": 2}]}

    async def fetch():
        return built

    async def run():
        return await backend.cached_swr(
            "opds:home:y", 120, 1800, fetch, is_valid=lambda d: bool(d.get("groups"))
        )

    result = asyncio.run(run())
    assert result == built  # after waiting out the peer, builds itself


def test_stale_hit_retains_and_runs_background_refresh(monkeypatch):
    """A stale-but-valid hit returns immediately and runs a background refresh
    that is held to completion (guards the asyncio task-GC footgun)."""
    backend = MemcachedBackend()
    stale_value = {"groups": [{"x": 1}]}
    fresh_value = {"groups": [{"y": 2}]}
    # Stored entry is valid but past its fresh window -> stale path.
    monkeypatch.setattr(
        backend, "get",
        lambda key, quiet=False: {"v": stale_value, "exp": 0},
    )
    monkeypatch.setattr(backend, "_acquire_distributed_lock", lambda key, expire=30: True)
    monkeypatch.setattr(backend, "_release_distributed_lock", lambda key: None)
    stored: dict = {}
    monkeypatch.setattr(backend, "_set_swr", lambda k, v, f, s: stored.update({k: v}))

    calls = {"fetch": 0}

    async def fetch():
        calls["fetch"] += 1
        return fresh_value

    async def run():
        result = await backend.cached_swr(
            "opds:home:z", 120, 1800, fetch, is_valid=lambda d: bool(d.get("groups"))
        )
        # Stale value served immediately, refresh task retained.
        assert result == stale_value
        assert backend._refresh_tasks, "background refresh task not retained"
        # Drain the retained task(s) to completion.
        await asyncio.gather(*list(backend._refresh_tasks))
        return result

    asyncio.run(run())
    assert calls["fetch"] == 1  # background refresh actually ran
    assert stored.get("opds:home:z") == fresh_value  # fresh value written
    assert backend._refresh_tasks == set()  # done-callback cleared the strong ref


def test_lease_elects_single_leader_and_renews(monkeypatch):
    """Exactly one owner holds the lease; that owner renews; others are denied.
    No memcached → caller always leads (single-process/dev)."""
    backend = MemcachedBackend()

    # No memcached client → acts as the sole leader.
    monkeypatch.setattr(backend, "_get_client", lambda: None)
    assert backend.acquire_or_renew_lease("home_warmer", 100, "wA") is True

    # Shared store across "workers".
    store: dict = {}

    class FakeClient:
        def get(self, key):
            return store.get(key)

        def add(self, key, value, expire):
            if key in store:
                return False
            store[key] = value
            return True

        def set(self, key, value, expire):
            store[key] = value
            return True

    monkeypatch.setattr(backend, "_get_client", lambda: FakeClient())
    assert backend.acquire_or_renew_lease("home_warmer", 100, "wA") is True   # wA acquires
    assert backend.acquire_or_renew_lease("home_warmer", 100, "wB") is False  # wB denied
    assert backend.acquire_or_renew_lease("home_warmer", 100, "wA") is True   # wA renews


def test_cache_status_build_marks_miss_even_with_nested_hit(monkeypatch):
    """X-Cache accuracy: a built (MISS) resource must report MISS even if a
    nested cached() call inside the fetch recorded a HIT."""
    backend = MemcachedBackend()
    monkeypatch.setattr(backend, "get", lambda key: None)  # always cold
    monkeypatch.setattr(backend, "_acquire_distributed_lock", lambda key, expire=30: True)
    monkeypatch.setattr(backend, "_release_distributed_lock", lambda key: None)
    monkeypatch.setattr(backend, "set", lambda k, v, t: None)

    async def fetch():
        cache_mod._cache_status.set("HIT")  # simulate a nested cached() hit
        return {"x": 1}

    async def run():
        await backend.cached("k", 60, fetch)
        # Read in the same task/context the route uses (asyncio.run would copy it).
        return cache_mod.last_cache_status()

    assert asyncio.run(run()) == "MISS"
