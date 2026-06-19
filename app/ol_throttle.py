"""App-side throttle for outbound Open Library requests.

The ``pyopds2_openlibrary`` package is intentionally free of any concurrency or
rate-limiting logic so it stays usable standalone. The home build and search
fan out across several (sometimes nested) thread pools, which can burst many
simultaneous Solr queries and trip OL's 429 rate limit.

This module bounds that concurrency *from the app* without touching the package:
the package issues every request through its module-global ``httpx`` symbol, so
we swap that symbol for a thin wrapper that guards ``.get()`` with a semaphore
and delegates everything else to the real ``httpx`` module.
"""
from __future__ import annotations

import threading
from contextvars import ContextVar, Token
from typing import Optional

import pyopds2_openlibrary as _ol
from app.logger import get_logger

logger = get_logger(__name__)

_installed = False

# The originating client's IP for the in-flight request. Set by middleware
# (app/main.py) per request and read by the httpx wrapper so every outbound OL
# call carries X-Forwarded-For — OL then rate-limits per end user instead of by
# our single server IP. ``asyncio.to_thread`` copies the context into worker
# threads, so request-scoped provider calls inherit it.
_client_ip: ContextVar[Optional[str]] = ContextVar("ol_client_ip", default=None)


def set_client_ip(ip: Optional[str]) -> Token:
    return _client_ip.set(ip)


def reset_client_ip(token: Token) -> None:
    _client_ip.reset(token)


def client_ip_from_request(request) -> Optional[str]:
    """Originating client IP: the first hop of an inbound X-Forwarded-For chain
    (the real user, ahead of any proxies), else the direct peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


class _ThrottledHttpx:
    """Wraps the real ``httpx`` module, bounding concurrent ``.get()`` calls.

    The semaphore guards only the network call, so the package's retry/backoff
    sleeps (which happen between ``httpx.get`` calls) never hold a slot. All
    other attribute access (``TransportError``, ``HTTPStatusError``, ``stream``,
    …) delegates to the real module.
    """

    def __init__(self, real, max_concurrent: int):
        self._real = real
        self._sem = threading.Semaphore(max_concurrent)

    def get(self, *args, **kwargs):
        ip = _client_ip.get()
        if ip:
            headers = dict(kwargs.get("headers") or {})
            headers["X-Forwarded-For"] = ip
            kwargs["headers"] = headers
        with self._sem:
            return self._real.get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def install_ol_request_throttle(max_concurrent: int) -> None:
    """Bound concurrent outbound OL requests to ``max_concurrent``. Idempotent."""
    global _installed
    if _installed:
        return
    _ol.httpx = _ThrottledHttpx(_ol.httpx, max_concurrent)
    _installed = True
    logger.info("OL request throttle installed (max_concurrent=%d)", max_concurrent)
