"""Tests for the background warmer (app/warmup.py): home + subject warming and
single-leader gating."""
from __future__ import annotations

import asyncio

import pytest

from app import warmup as warm_mod
from app.routes import opds as opds_route
from app.routes.opds import _home_cache_key, _search_cache_key
from pyopds2_openlibrary import OpenLibraryDataProvider

BASE = "https://openlibrary.org/opds"


class FakeCache:
    """Minimal dict-backed cache mirroring the backend interface used by warmup."""

    def __init__(self):
        self.store: dict = {}
        self.lease_ok = True

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl):
        self.store[key] = value

    async def cached(self, key, ttl, fetch, is_valid=None):
        if key in self.store:
            return self.store[key]
        value = await fetch()
        if is_valid is None or is_valid(value):
            self.store[key] = value
        return value

    async def cached_swr(self, key, fresh_ttl, stale_ttl, fetch, is_valid=None):
        wrapped = self.store.get(key)
        if isinstance(wrapped, dict) and "v" in wrapped and (is_valid is None or is_valid(wrapped["v"])):
            return wrapped["v"]
        value = await fetch()
        if is_valid is None or is_valid(value):
            self.store[key] = {"v": value, "exp": 1e18}
        return value

    def acquire_or_renew_lease(self, name, ttl, owner):
        return self.lease_ok


def _use_fake(monkeypatch) -> FakeCache:
    cache = FakeCache()
    monkeypatch.setattr(warm_mod, "get_cache", lambda: cache)
    monkeypatch.setattr(opds_route, "_safe_fetch_language_counts", lambda **kw: {})
    # language warm: no network, empty map → no-op
    monkeypatch.setattr(warm_mod._ol_module, "fetch_languages_map", lambda: None)
    return cache


def _page_key(page: int) -> str:
    return _home_cache_key(BASE, "everything", None, page, None, None, 0)


def test_warm_home_one_fetch_all_pages(monkeypatch):
    cache = _use_fake(monkeypatch)
    calls = []

    def fake_pages(**kwargs):
        calls.append(kwargs)
        return [
            {"groups": [{"metadata": {"title": "Trending Books"}}]},
            {"groups": [{"x": 1}]},
            {"groups": [{"y": 2}]},
        ]

    monkeypatch.setattr(OpenLibraryDataProvider, "build_home_pages", staticmethod(fake_pages))

    pages = asyncio.run(warm_mod.warm_home(BASE))
    assert pages == 3
    assert len(calls) == 1  # ONE OL fan-out for all pages
    for page in (1, 2, 3):
        assert _page_key(page) in cache.store


def test_warm_home_skips_when_fresh(monkeypatch):
    # Fast loop ticks must not re-fetch when the page is still within its window.
    cache = _use_fake(monkeypatch)
    calls = []
    monkeypatch.setattr(
        OpenLibraryDataProvider, "build_home_pages",
        staticmethod(lambda **kw: calls.append(1) or [{"groups": [{"x": 1}]}]),
    )
    assert asyncio.run(warm_mod.warm_home(BASE)) == 1   # cold → builds
    assert asyncio.run(warm_mod.warm_home(BASE)) == 0   # fresh → skipped
    assert len(calls) == 1  # only one OL fan-out


def test_warm_home_skips_empty_page(monkeypatch):
    cache = _use_fake(monkeypatch)
    monkeypatch.setattr(
        OpenLibraryDataProvider, "build_home_pages",
        staticmethod(lambda **kw: [{"groups": []}]),
    )
    asyncio.run(warm_mod.warm_home(BASE))
    assert _page_key(1) not in cache.store


def test_warm_subjects_caches_featured_feeds(monkeypatch):
    cache = _use_fake(monkeypatch)

    async def fake_build(provider, base, *, query, **kwargs):
        return {"publications": [1], "query": query}

    monkeypatch.setattr(warm_mod, "build_search_page", fake_build)

    rebaked, total = asyncio.run(warm_mod.warm_subjects(BASE))
    feeds = OpenLibraryDataProvider.featured_subject_feeds()
    assert total == len(feeds)
    assert rebaked == len(feeds)  # all cold → all rebaked
    title, query, sort = feeds[0]
    key = _search_cache_key(query, sort, 25, 1, "everything", None, None, None)
    assert key in cache.store
    assert cache.store[key]["v"]["query"] == query


def test_warm_trending_populates_key(monkeypatch):
    cache = _use_fake(monkeypatch)

    async def fake_trending(provider, mode, language, limit, media_type, access):
        return {"publications": [1], "metadata": {"title": "Trending Books"}}

    monkeypatch.setattr(warm_mod, "_fetch_trending", fake_trending)

    asyncio.run(warm_mod.warm_trending(BASE))
    from app.routes.opds import _trending_cache_key
    key = _trending_cache_key("everything", None, None, None, 0)
    assert key in cache.store  # first home hit will be a cache HIT, not a cold fetch


def test_only_featured_searches_are_cached():
    # The route caches a search only when (query, sort) is a featured subject feed;
    # long-tail searches stay uncached (no unbounded keys).
    feeds = OpenLibraryDataProvider.featured_subject_feeds()
    _title, query, sort = feeds[0]
    assert opds_route._is_featured_search(query, sort) is True
    assert opds_route._is_featured_search("title:some random query", "trending") is False


def test_non_leader_does_not_warm(monkeypatch):
    cache = _use_fake(monkeypatch)
    cache.lease_ok = False  # this worker is NOT the leader
    built = []
    monkeypatch.setattr(
        OpenLibraryDataProvider, "build_home_pages",
        staticmethod(lambda **kw: built.append(1) or [{"groups": [{"x": 1}]}]),
    )
    monkeypatch.setattr(warm_mod, "TRENDING_CADENCE_SECONDS", 0.01)
    monkeypatch.setattr(warm_mod, "FEED_CADENCE_SECONDS", 0.01)

    async def run():
        task = asyncio.create_task(warm_mod.run_warmer(BASE))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert built == []  # non-leader never warms
