"""Tests for the app-side Open Library request throttle."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

import pyopds2_openlibrary as ol
import app.ol_throttle as throttle
from app.ol_throttle import (
    _ThrottledHttpx,
    client_ip_from_request,
    install_ol_request_throttle,
    reset_client_ip,
    set_client_ip,
)


class _FakeReq:
    def __init__(self, headers=None, client_host=None):
        self.headers = headers or {}

        class _C:
            host = client_host

        self.client = _C() if client_host else None


def test_throttled_httpx_bounds_concurrency():
    inflight = 0
    peak = 0
    lock = threading.Lock()

    class FakeHttpx:
        def get(self, *args, **kwargs):
            nonlocal inflight, peak
            with lock:
                inflight += 1
                peak = max(peak, inflight)
            time.sleep(0.03)
            with lock:
                inflight -= 1
            return "ok"

    throttled = _ThrottledHttpx(FakeHttpx(), 2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: throttled.get("u"), range(8)))

    assert results == ["ok"] * 8
    assert peak <= 2  # never more than the cap in flight at once


def test_throttled_httpx_delegates_other_attributes():
    throttled = _ThrottledHttpx(httpx, 1)
    # Non-get attributes pass through to the real httpx module unchanged.
    assert throttled.TransportError is httpx.TransportError
    assert throttled.HTTPStatusError is httpx.HTTPStatusError


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setattr(ol, "httpx", httpx)        # baseline, auto-restored
    monkeypatch.setattr(throttle, "_installed", False)

    install_ol_request_throttle(3)
    wrapped = ol.httpx
    assert isinstance(wrapped, _ThrottledHttpx)

    install_ol_request_throttle(3)
    assert ol.httpx is wrapped  # not double-wrapped


def test_get_injects_x_forwarded_for_from_context():
    seen = {}

    class FakeHttpx:
        def get(self, url, **kwargs):
            seen.update(kwargs.get("headers") or {})
            return "ok"

    throttled = _ThrottledHttpx(FakeHttpx(), 4)
    token = set_client_ip("203.0.113.7")
    try:
        throttled.get("https://openlibrary.org/x.json", headers={"User-Agent": "ua"})
    finally:
        reset_client_ip(token)

    assert seen["X-Forwarded-For"] == "203.0.113.7"
    assert seen["User-Agent"] == "ua"  # existing headers preserved


def test_get_omits_x_forwarded_for_when_no_client_ip():
    seen = {}

    class FakeHttpx:
        def get(self, url, **kwargs):
            seen.update(kwargs.get("headers") or {})
            return "ok"

    throttled = _ThrottledHttpx(FakeHttpx(), 4)
    throttled.get("https://openlibrary.org/x.json", headers={"User-Agent": "ua"})
    assert "X-Forwarded-For" not in seen


def test_client_ip_prefers_first_forwarded_hop():
    req = _FakeReq(headers={"x-forwarded-for": "198.51.100.5, 10.0.0.1, 10.0.0.2"})
    assert client_ip_from_request(req) == "198.51.100.5"


def test_client_ip_falls_back_to_peer():
    req = _FakeReq(client_host="192.0.2.9")
    assert client_ip_from_request(req) == "192.0.2.9"
