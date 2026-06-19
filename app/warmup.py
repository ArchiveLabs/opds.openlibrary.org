"""Background cache warming — the single place that decides WHAT is kept warm,
in what ORDER, and HOW, across any number of workers.

Per cycle (leader only):
  1. home pages 1..N  (one OL group fan-out renders all pages)
  2. featured subject feeds (Art, Sci-Fi, …) — the home nav links

NOT warmed here: the Trending carousel (its own short-TTL SWR key, refreshed by
request traffic — see app/routes/opds.py) and languages (too many variants).

TTL / cadence (constants in app/cache.py):
  home default 10 min · home other pages 15 min · subjects 30 min · lang map 7 days
  warmer cadence = 80% of the home TTL, so the home feed is rebaked *before* it
  expires (the 80/20 refresh). Subjects rebake only when within the last 20% of
  their own (longer) TTL, so they aren't needlessly refetched every cycle.

Multi-worker: exactly ONE worker warms. It holds a renewable lease
(``acquire_or_renew_lease``); the others stay idle and take over only if the
leader dies (lease auto-expires). Scales cleanly to any worker count.
"""
from __future__ import annotations

import asyncio
import os
import random
import socket
import time
from urllib.parse import urlencode

import pyopds2_openlibrary as _ol_module
from pyopds2_openlibrary import OpenLibraryDataProvider

from app.cache import (
    LANG_COUNTS_KEY,
    LANG_OPTIONS_KEY,
    TTL_HOME_DEFAULT_SECONDS,
    TTL_HOME_DEFAULT_STALE_SECONDS,
    TTL_HOME_NONDEFAULT_SECONDS,
    TTL_LANG_COUNTS_SECONDS,
    TTL_LANG_OPTIONS_SECONDS,
    TTL_SUBJECT_SECONDS,
    TTL_SUBJECT_STALE_SECONDS,
    TTL_TRENDING_SECONDS,
    TTL_TRENDING_STALE_SECONDS,
    get_cache,
)
from app.config import OL_PREWARM_HOME_PAGES
from app.logger import get_logger
from app.routes.opds import (
    _call_provider_compat,
    _fetch_lang_counts,
    _fetch_trending,
    _home_cache_key,
    _home_is_valid,
    _refresh_lang_options_cache,
    _search_cache_key,
    _trending_cache_key,
    _trending_is_valid,
    build_search_page,
    get_provider,
)

logger = get_logger(__name__)

WARM_LEADER_NAME = "home_warmer"
# The loop ticks at trending's cadence (80% of its short TTL) so the leader keeps
# the Trending key continuously fresh — the route then reads a FRESH hit and never
# triggers a per-request background refresh. Home/subjects are gated to rebuild
# only within their own (longer) 80/20 windows, so a fast tick stays cheap.
TRENDING_CADENCE_SECONDS = max(15, int(0.8 * TTL_TRENDING_SECONDS))
# Home/subjects loop runs much slower (80% of the home TTL); the per-key 80/20
# due-gate decides what actually rebuilds each pass.
FEED_CADENCE_SECONDS = max(60, int(0.8 * TTL_HOME_DEFAULT_SECONDS))
# Lease must outlive the fast loop's tick (which renews it) by a safe margin, so
# the heavy feed loop's long work can never let it lapse. A dead leader is
# replaced within ~_LEASE_TTL_SECONDS.
_LEASE_TTL_SECONDS = TRENDING_CADENCE_SECONDS * 3
_OWNER = f"{socket.gethostname()}:{os.getpid()}"


def _due_for_rebake(cache, key: str, fresh_ttl: int) -> bool:
    """True if a key is missing/invalid/stale, or within the last 20% of its
    fresh window (proactive 80/20 rebake)."""
    wrapped = cache.get(key)
    if not (isinstance(wrapped, dict) and "v" in wrapped):
        return True
    return time.time() >= wrapped.get("exp", 0) - 0.2 * fresh_ttl


async def _warm_language_cache() -> None:
    """Warm pyopds2_openlibrary's in-process language map, deduped across workers
    via the shared cache. Only the worker that actually fetches logs it."""
    cache = get_cache()

    async def _fetch() -> dict:
        await asyncio.to_thread(_ol_module.fetch_languages_map)
        if not _ol_module._languages_map_cache:
            return {}
        logger.info("language map fetched (%d languages)", len(_ol_module._languages_map_cache))
        return {"map": _ol_module._languages_map_cache, "names": _ol_module._languages_names_cache}

    try:
        data = await cache.cached(
            LANG_OPTIONS_KEY, TTL_LANG_OPTIONS_SECONDS, _fetch,
            is_valid=lambda d: bool(d.get("map")),
        )
    except Exception as exc:
        logger.warning("could not warm language cache: %s", exc)
        return

    if data.get("map"):
        _ol_module._languages_map_cache = data["map"]
        _ol_module._languages_names_cache = data["names"]
        _ol_module._languages_map_fetched_at = time.monotonic()


async def warm_home(base: str) -> int:
    """Rebake the default home pages 1..N from a single OL group fan-out, but
    only when the page-1 key is within its 80/20 refresh window (so a fast loop
    tick doesn't re-fetch every time). ``build_home_pages`` renders every page
    from one fetch. Returns the number of pages stored (0 if not due)."""
    cache = get_cache()
    home_key1 = _home_cache_key(base, "everything", None, 1, None, None, 0)
    if not _due_for_rebake(cache, home_key1, TTL_HOME_DEFAULT_SECONDS):
        return 0

    provider = get_provider(base)  # noqa: F841 (sets provider class attrs)

    lang = await cache.cached(LANG_COUNTS_KEY, TTL_LANG_COUNTS_SECONDS, _fetch_lang_counts)
    language_counts = lang.get("counts") or None

    page_dicts = await asyncio.to_thread(
        _call_provider_compat,
        OpenLibraryDataProvider.build_home_pages,
        base=base, mode="everything", language=None, media_type=None, access=None,
        language_counts=language_counts, limit=0, pages=OL_PREWARM_HOME_PAGES,
    )
    _refresh_lang_options_cache()

    for idx, page_data in enumerate(page_dicts, start=1):
        ttl = TTL_HOME_DEFAULT_SECONDS if idx == 1 else TTL_HOME_NONDEFAULT_SECONDS
        home_key = _home_cache_key(base, "everything", None, idx, None, None, 0)

        async def _store(pd: dict = page_data) -> dict:
            return pd

        await cache.cached_swr(
            home_key, ttl, TTL_HOME_DEFAULT_STALE_SECONDS, _store, is_valid=_home_is_valid,
        )
    return len(page_dicts)


async def warm_trending(base: str) -> None:
    """Proactively keep the short-TTL Trending key fresh so the route always reads
    a FRESH hit and never triggers a per-request background refresh.

    Refreshes at the 80/20 mark (age ≥ 80% of the trending TTL) — *before* the
    SWR fresh boundary — and stores in the SWR-wrapped shape the route reads.
    Only the single leader runs this, so writing directly (no lock) is safe and
    avoids spawning a background task on every tick.
    """
    cache = get_cache()
    key = _trending_cache_key("everything", None, None, None, 0)
    if not _due_for_rebake(cache, key, TTL_TRENDING_SECONDS):
        return
    provider = get_provider(base)
    data = await _fetch_trending(provider, "everything", None, 0, None, None)
    if _trending_is_valid(data):
        cache.set(
            key, {"v": data, "exp": time.time() + TTL_TRENDING_SECONDS},
            TTL_TRENDING_STALE_SECONDS,
        )


async def warm_subjects(base: str) -> tuple[int, int]:
    """Rebake featured subject feeds (page 1, default mode) under the exact key
    the search route reads. Each is its own OL search, so only the ones within
    their 80/20 window are refetched. Returns ``(rebaked, total)``."""
    cache = get_cache()
    provider = get_provider(base)

    lang = await cache.cached(LANG_COUNTS_KEY, TTL_LANG_COUNTS_SECONDS, _fetch_lang_counts)
    language_counts = lang.get("counts") or None

    feeds = OpenLibraryDataProvider.featured_subject_feeds()
    rebaked = 0
    for title, query, sort in feeds:
        key = _search_cache_key(query, sort, 25, 1, "everything", None, None, None)
        if not _due_for_rebake(cache, key, TTL_SUBJECT_SECONDS):
            continue
        self_href = f"{base}/search?" + urlencode({"sort": sort, "title": title, "query": query})

        async def _build(q=query, s=sort, t=title, href=self_href) -> dict:
            return await build_search_page(
                provider, base, query=q, limit=25, page=1, sort=s, mode="everything",
                title=t, language=None, media_type=None, access=None,
                language_counts=language_counts, self_href=href,
            )

        await cache.cached_swr(
            key, TTL_SUBJECT_SECONDS, TTL_SUBJECT_STALE_SECONDS, _build,
            is_valid=_trending_is_valid,
        )
        rebaked += 1
    return rebaked, len(feeds)


def _is_leader() -> bool:
    return get_cache().acquire_or_renew_lease(WARM_LEADER_NAME, _LEASE_TTL_SECONDS, _OWNER)


async def _trending_loop(base: str) -> None:
    """Fast loop: keep Trending continuously fresh AND renew the lease often (so
    the slow feed loop's long work can never let leadership lapse). Logs the
    leader/standby state for the warmer as a whole."""
    was_leader = False
    announced_standby = False
    while True:
        leader = _is_leader()
        if leader:
            if not was_leader:
                logger.info("warm leader elected (%s)", _OWNER)
            try:
                await warm_trending(base)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("trending warm failed: %s", exc)
        elif not announced_standby:
            logger.info("standing by — another worker leads warming")
            announced_standby = True
        was_leader = leader
        await asyncio.sleep(TRENDING_CADENCE_SECONDS * random.uniform(0.9, 1.1))


async def _feed_loop(base: str) -> None:
    """Slow loop: rebuild home + subjects when within their 80/20 windows. Runs
    concurrently with the trending loop, so its long work never starves trending
    or the lease renewal."""
    while True:
        if _is_leader():
            try:
                home_pages = await warm_home(base)
                subj_built, subj_total = await warm_subjects(base)
                if home_pages or subj_built:
                    logger.info(
                        "warm cycle: home rebaked=%d page(s), subjects rebaked=%d/%d",
                        home_pages, subj_built, subj_total,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("feed warm failed: %s", exc)
        await asyncio.sleep(FEED_CADENCE_SECONDS * random.uniform(0.9, 1.1))


async def run_warmer(base: str | None) -> None:
    """Warm the language map once, then run the trending + feed loops concurrently
    (single-leader; non-leaders idle). Never raises out of the loops."""
    try:
        await _warm_language_cache()
    except Exception as exc:
        logger.warning("language cache warm failed: %s", exc)

    if not base:
        return  # no fixed base → cache keys are per-request, can't be warmed

    await asyncio.gather(_trending_loop(base), _feed_loop(base))


def start_warmer(base: str | None) -> asyncio.Task:
    """Launch the warmer as a background task (non-blocking startup)."""
    return asyncio.create_task(run_warmer(base))
