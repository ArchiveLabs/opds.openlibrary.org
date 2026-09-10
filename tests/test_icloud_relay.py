"""Tests for the iCloud asset relay.

The relay has one job and one security property, and both are asserted here
without a network: the destination check that keeps it from being an open
relay, and the fact that it is not there at all until configured.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routes import icloud

client = TestClient(app)

ORIGIN = "https://localhost:4173"
OTHER_ORIGIN = "https://evil.example"
SLOT = "https://p42-contentws.icloud.com/abc123/singleFileUpload"

RECEIPT = {
    "singleFile": {
        "wrappingKey": "wk",
        "fileChecksum": "fc",
        "receipt": "r",
        "referenceChecksum": "rc",
        "size": 4,
    }
}


def _enabled():
    """Patch the module's read-once configuration into the enabled state."""
    return patch.multiple(icloud, ICLOUD_RELAY_ENABLED=True, ICLOUD_RELAY_ORIGINS=[ORIGIN])


def _upstream(status: int = 200, body: dict | None = None):
    """A stand-in for ``httpx.AsyncClient`` whose ``post`` records its call."""
    response = SimpleNamespace(
        status_code=status,
        content=httpx.Response(status, json=body or RECEIPT).content,
        headers={"content-type": "application/json"},
    )
    post = AsyncMock(return_value=response)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=SimpleNamespace(post=post))
    fake_client.__aexit__ = AsyncMock(return_value=False)

    return patch.object(icloud.httpx, "AsyncClient", return_value=fake_client), post


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_relay_does_not_exist_until_configured():
    # 404, not 403: an unconfigured deployment should not advertise a relay it
    # never meant to run. This is what keeps a public repo from shipping an
    # open byte relay to anyone who deploys it.
    with patch.multiple(icloud, ICLOUD_RELAY_ENABLED=False, ICLOUD_RELAY_ORIGINS=[]):
        assert client.post(f"{icloud.RELAY_PATH}?to={SLOT}", content=b"data", headers={"Origin": ORIGIN}).status_code == 404
        assert client.options(icloud.RELAY_PATH, headers={"Origin": ORIGIN}).status_code == 404
        assert client.get(icloud.STATUS_PATH).json() == {"enabled": False}


def test_status_reports_enabled_without_naming_origins():
    with _enabled():
        assert client.get(icloud.STATUS_PATH).json() == {"enabled": True}


# ---------------------------------------------------------------------------
# The destination check — the whole security design
# ---------------------------------------------------------------------------


def test_accepts_a_real_slot_url():
    assert icloud.upload_target(SLOT) == SLOT
    assert icloud.upload_target("https://p1-contentws.icloud.com/x/singleFileUpload").endswith("singleFileUpload")


@pytest.mark.parametrize(
    "target",
    [
        # Plain http: a signed URL is only ever https, and downgrading it would
        # send a reader's book in the clear.
        "http://p42-contentws.icloud.com/abc/singleFileUpload",
        # Not Apple at all.
        "https://evil.example/singleFileUpload",
        # A lookalike that *contains* the real host: this is why the pattern is
        # anchored at both ends.
        "https://p42-contentws.icloud.com.evil.example/singleFileUpload",
        "https://evil.example/p42-contentws.icloud.com/singleFileUpload",
        # Apple, but not the content host.
        "https://api.apple-cloudkit.com/database/1/x/development/private/records/modify",
        # The content host, but not an upload.
        "https://p42-contentws.icloud.com/abc/download",
        # Garbage.
        "not a url",
        "",
    ],
)
def test_refuses_anything_that_is_not_an_apple_upload_slot(target):
    with pytest.raises(HTTPException) as refused:
        icloud.upload_target(target)

    assert refused.value.status_code == 400


# ---------------------------------------------------------------------------
# CORS and origins
# ---------------------------------------------------------------------------


def test_preflight_answers_an_allowlisted_origin_by_name():
    with _enabled():
        response = client.options(icloud.RELAY_PATH, headers={"Origin": ORIGIN})

    assert response.status_code == 204
    # Named, not "*": the allowlist is the point. And Vary, so a shared cache
    # never hands one origin's answer to another.
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]
    assert response.headers["vary"] == "Origin"


def test_refuses_an_origin_it_was_not_told_about():
    with _enabled():
        assert client.options(icloud.RELAY_PATH, headers={"Origin": OTHER_ORIGIN}).status_code == 403
        assert client.post(f"{icloud.RELAY_PATH}?to={SLOT}", content=b"data", headers={"Origin": OTHER_ORIGIN}).status_code == 403
        # No Origin at all is not a browser, and the relay exists for browsers.
        assert client.post(f"{icloud.RELAY_PATH}?to={SLOT}", content=b"data").status_code == 403


# ---------------------------------------------------------------------------
# The relay itself
# ---------------------------------------------------------------------------


def test_relays_the_bytes_and_returns_apples_answer_verbatim():
    upstream, post = _upstream(200, RECEIPT)

    with _enabled(), upstream:
        response = client.post(
            f"{icloud.RELAY_PATH}?to={SLOT}",
            content=b"book",
            headers={"Origin": ORIGIN, "Content-Type": "application/epub+zip"},
        )

    assert response.status_code == 200
    assert response.json() == RECEIPT
    assert response.headers["access-control-allow-origin"] == ORIGIN

    post.assert_awaited_once()
    args, kwargs = post.await_args
    assert args == (SLOT,)
    assert kwargs["content"] == b"book"
    # Nothing of the caller's travels on: the slot URL is signed and needs no
    # headers, and forwarding a browser's would be a way to smuggle something
    # into a request this service is putting its own name to.
    assert "headers" not in kwargs


def test_passes_an_upstream_refusal_through_unchanged():
    upstream, _ = _upstream(403, {"error": "expired"})

    with _enabled(), upstream:
        response = client.post(f"{icloud.RELAY_PATH}?to={SLOT}", content=b"book", headers={"Origin": ORIGIN})

    # Apple's verdict is Apple's; this service has no opinion to add.
    assert response.status_code == 403
    assert response.json() == {"error": "expired"}
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_refuses_a_chunk_over_the_ceiling_before_sending_anything():
    upstream, post = _upstream()

    with _enabled(), upstream, patch.object(icloud, "ICLOUD_RELAY_MAX_BYTES", 4):
        response = client.post(f"{icloud.RELAY_PATH}?to={SLOT}", content=b"12345", headers={"Origin": ORIGIN})

    assert response.status_code == 413
    # Refused here, not at Apple: an oversized body must never leave a
    # half-written asset behind.
    post.assert_not_awaited()


def test_refuses_an_empty_chunk():
    upstream, post = _upstream()

    with _enabled(), upstream:
        response = client.post(f"{icloud.RELAY_PATH}?to={SLOT}", content=b"", headers={"Origin": ORIGIN})

    assert response.status_code == 400
    post.assert_not_awaited()


def test_a_dead_upstream_is_a_502_not_a_traceback():
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(post=AsyncMock(side_effect=httpx.ConnectError("no route")))
    )
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with _enabled(), patch.object(icloud.httpx, "AsyncClient", return_value=fake_client):
        response = client.post(f"{icloud.RELAY_PATH}?to={SLOT}", content=b"book", headers={"Origin": ORIGIN})

    assert response.status_code == 502


def test_a_bad_destination_is_refused_before_the_body_is_read():
    upstream, post = _upstream()

    with _enabled(), upstream:
        response = client.post(
            f"{icloud.RELAY_PATH}?to=https://evil.example/singleFileUpload",
            content=b"book",
            headers={"Origin": ORIGIN},
        )

    assert response.status_code == 400
    post.assert_not_awaited()
