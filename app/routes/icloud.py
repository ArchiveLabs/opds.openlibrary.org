"""A byte relay for CloudKit asset uploads made from a browser.

Why this exists, in one paragraph. Putting a book into a reader's own iCloud
over CloudKit web services is a two-step dance: ask ``assets/upload`` on
``api.apple-cloudkit.com`` for a signed slot, then POST the bytes to the URL it
hands back. The first host answers a browser; the second —
``p<N>-contentws.icloud.com`` — sends no ``Access-Control-Allow-Origin`` on any
response, so a browser can send those bytes but can never read the receipt, and
the receipt (``singleFile``) is what the record needs. Measured, along with the
workarounds that do not work, in the reader repo's
``plans/ios/08-native-cloudkit.md``. Native shells are unaffected: their
requests do not come from a browser.

What this relay is trusted with, and what it is not. The signed slot URL carries
its own authorisation and expires in fifteen minutes, so **no credential passes
through here** — not the API token, not the reader's web-auth session. What does
pass through is one chunk of one file. That is not nothing: what a person reads
is not a neutral fact about them, so nothing here logs a body, and nothing here
keeps one.

Why it is not a CORS proxy. A relay that forwards to any URL it is given is an
open relay, and an open relay is how a service ends up carrying somebody else's
traffic. The only thing that makes this safe is that the destination is checked
against one host pattern and refused otherwise — see ``_upload_target``.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.config import (
    ICLOUD_RELAY_ENABLED,
    ICLOUD_RELAY_MAX_BYTES,
    ICLOUD_RELAY_ORIGINS,
    ICLOUD_RELAY_TIMEOUT,
)
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

# Apple's content hosts, which is where every signed upload slot lives. Two
# live forms, both observed: `cws.icloud-content.com` (what the web-services
# token issues today) and `p<N>-contentws.icloud.com` (the form the docs and
# older probes showed). Anchored at both ends and requiring a real label before
# the Apple suffix, so neither `cws.icloud-content.com.evil.example` nor
# `evilicloud-content.com` can match and turn this into an open relay.
_APPLE_CONTENT_HOST = re.compile(
    r"^(p\d+-contentws\.icloud\.com|[a-z0-9-]+\.icloud-content\.com)$"
)

RELAY_PATH = "/icloud/asset"
STATUS_PATH = "/icloud/relay-status"


def _require_enabled() -> None:
    """An unconfigured deployment does not have this endpoint at all.

    404 rather than 403: there is nothing here to be forbidden from, and a
    deployment that never meant to run a relay should not advertise one.
    """
    if not ICLOUD_RELAY_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")


def _allowed_origin(request: Request) -> str:
    """The caller's origin, if it is one this relay answers.

    Hygiene rather than a security control, and worth being clear about which:
    an ``Origin`` header is set by browsers and trivially forged by anything
    else, so this keeps the endpoint from being casually reused — it is not what
    makes it safe. That is ``_upload_target``.
    """
    origin = request.headers.get("origin", "")

    if origin not in ICLOUD_RELAY_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin not allowed")

    return origin


def _cors(origin: str) -> dict[str, str]:
    """CORS headers for one allowlisted origin.

    Named rather than ``*`` because the allowlist is the point, and ``Vary``
    because a shared cache must not serve one origin's answer to another.

    Set on the route's own responses rather than by middleware: the app-level
    ``CORSMiddleware`` is off in production, where nginx supplies the headers
    for the catalog's GET routes, and turning it on would emit a duplicate
    ``Access-Control-Allow-Origin`` and break every browser client. See
    ``CORS_ENABLED``. Whether that nginx also decorates *this* path is a
    deployment question, not one this module can answer.
    """
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "content-type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def upload_target(to: str) -> str:
    """The URL to relay to, or a refusal.

    The whole security design of this module. Three things are required and any
    one of them missing is a 400: ``https``, a host matching
    ``_APPLE_CONTENT_HOST`` exactly, and a path ending in ``singleFileUpload``,
    which is the only operation a slot URL names.

    Public so the tests can drive it without a server.
    """
    parsed = urlparse(to)

    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Upload target must be https")

    if not _APPLE_CONTENT_HOST.match(parsed.hostname or ""):
        raise HTTPException(status_code=400, detail="Upload target is not an iCloud content host")

    if not parsed.path.endswith("singleFileUpload"):
        raise HTTPException(status_code=400, detail="Upload target is not an asset upload")

    return to


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")

    if raw is None:
        return None

    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad Content-Length") from None


async def _capped_body(request: Request) -> bytes:
    """The request body, refused rather than truncated if it is too large.

    Read into memory rather than streamed through. At a 15 MB ceiling that is a
    bounded cost, and it buys the one thing a streamed relay cannot: the refusal
    happens before anything is sent to Apple, so an oversized body cannot leave
    a half-written asset behind.

    ``Content-Length`` is checked first because it is free, and the bytes are
    counted anyway because a header is a claim.
    """
    declared = _declared_length(request)

    if declared is not None and declared > ICLOUD_RELAY_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Asset chunk too large")

    chunks: list[bytes] = []
    size = 0

    async for chunk in request.stream():
        size += len(chunk)
        if size > ICLOUD_RELAY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Asset chunk too large")
        chunks.append(chunk)

    if not size:
        raise HTTPException(status_code=400, detail="Empty asset chunk")

    return b"".join(chunks)


@router.options(RELAY_PATH, include_in_schema=False)
async def relay_preflight(request: Request) -> Response:
    _require_enabled()

    return Response(status_code=204, headers=_cors(_allowed_origin(request)))


@router.post(RELAY_PATH, include_in_schema=False)
async def relay_asset(
    request: Request,
    to: str = Query(..., description="The signed CloudKit upload slot URL"),
) -> Response:
    """Relays one asset chunk to Apple and hands back what Apple said.

    Apple's status and body are returned as they arrive, unread except for the
    content type: the caller is the reader app, which knows how to interpret
    ``singleFile`` and does not need this service to have an opinion about it.
    """
    _require_enabled()

    # Origin first: a caller this relay does not answer gets a 403 with no CORS
    # headers, which is correct — it is not an allowed origin. Every response
    # *after* this point carries CORS, so an allowed caller can read even a
    # refusal. Without that, a 400 (bad target, oversized) reaches the browser
    # as an opaque "blocked by CORS" and the reader app cannot tell a rejected
    # target from a dead network.
    origin = _allowed_origin(request)

    try:
        target = upload_target(to)
        body = await _capped_body(request)
    except HTTPException as refused:
        return JSONResponse(
            {"detail": refused.detail}, status_code=refused.status_code, headers=_cors(origin)
        )

    try:
        async with httpx.AsyncClient(timeout=ICLOUD_RELAY_TIMEOUT) as client:
            # No headers forwarded from the caller. The slot URL is signed and
            # needs none, and passing a browser's headers on would be a way to
            # smuggle something into a request this service is putting its own
            # name to.
            upstream = await client.post(target, content=body)
    except httpx.HTTPError as exc:
        # The chunk's size is worth knowing when this goes wrong; its contents
        # never are.
        logger.warning("icloud relay: upstream failed (%d bytes): %s", len(body), exc)
        return JSONResponse(
            {"detail": "iCloud did not answer"}, status_code=502, headers=_cors(origin)
        )

    logger.info("icloud relay: %d bytes -> %s", len(body), upstream.status_code)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
        headers=_cors(origin),
    )


@router.get(STATUS_PATH, include_in_schema=False)
async def relay_status() -> JSONResponse:
    """Whether this deployment runs a relay, for the reader app to check.

    Deliberately says nothing about *which* origins are allowed: a caller that
    is allowed already knows, and a caller that is not has no use for the list.
    """
    return JSONResponse(content={"enabled": ICLOUD_RELAY_ENABLED})
