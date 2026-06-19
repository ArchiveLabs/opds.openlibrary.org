from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.cache import get_cache
from app.exceptions import AuthorNotFound, EditionNotFound, UpstreamError
from app.logger import get_logger, set_worker_label
from app.routes.opds import router as opds_router
from app.sentry import init_sentry
from app.config import (
    ENVIRONMENT,
    OPDS_BASE_URL,
    OL_MAX_CONCURRENT_REQUESTS,
)
from app.ol_throttle import (
    client_ip_from_request,
    install_ol_request_throttle,
    reset_client_ip,
    set_client_ip,
)
from app.warmup import start_warmer

logger = get_logger(__name__)

sentry_enabled = init_sentry()


def _assign_worker_number() -> None:
    """Hand each worker a friendly ordinal (1, 2, …) via a shared memcached
    counter, so logs read ``[worker 1]`` instead of an opaque PID. Best-effort:
    falls back to the PID label if memcached is unavailable. The counter carries
    a short TTL so numbering resets to 1,2,… on each boot (workers start within
    the window) rather than growing across restarts."""
    backend = get_cache()
    client = getattr(backend, "_get_client", lambda: None)()
    if client is None:
        return
    try:
        client.add("opds:worker_seq", b"0", expire=300)
        n = client.incr("opds:worker_seq", 1)
        if n:
            set_worker_label(n)
    except Exception:
        pass  # keep the PID fallback


@asynccontextmanager
async def lifespan(_: FastAPI):
    _assign_worker_number()
    logger.info("OPDS service starting up (sentry=%s)", sentry_enabled)
    # Bound outbound OL concurrency before any warming/serving to avoid 429s.
    install_ol_request_throttle(OL_MAX_CONCURRENT_REQUESTS)
    logger.info(
        "startup config: ENVIRONMENT=%s OPDS_BASE_URL=%s", ENVIRONMENT, OPDS_BASE_URL,
    )
    warmer_task: asyncio.Task | None = None
    if ENVIRONMENT != "test":
        base = OPDS_BASE_URL.rstrip("/") if OPDS_BASE_URL else None
        if not base:
            logger.warning(
                "homepage warming disabled: OPDS_BASE_URL is unset, so the cache-key "
                "base is per-request and cannot be warmed. Set OPDS_BASE_URL to the "
                "public base (e.g. https://openlibrary.org/opds) to enable it."
            )
        # Non-blocking: the warmer (app/warmup.py) elects a single leader and
        # keeps home + subjects warm; startup completes immediately.
        warmer_task = start_warmer(base)
    yield
    if warmer_task is not None:
        warmer_task.cancel()
        try:
            await warmer_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Open Library OPDS 2.0",
    description="Stand-alone OPDS 2.0 feed for Open Library",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _forward_client_ip(request: Request, call_next):
    """Stash the originating client IP so outbound OL requests can forward it
    as X-Forwarded-For (OL rate-limits per end user, not per server IP)."""
    token = set_client_ip(client_ip_from_request(request))
    try:
        return await call_next(request)
    finally:
        reset_client_ip(token)


class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and len(record.args) >= 3:
            return record.args[2] not in ("/sw.js", "/static/favicon.ico")
        return True

logging.getLogger("uvicorn.access").addFilter(EndpointFilter())


@app.exception_handler(EditionNotFound)
def handle_edition_not_found(_: Request, exc: EditionNotFound) -> JSONResponse:
    logger.warning("404 EditionNotFound: %s", exc)
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(AuthorNotFound)
def handle_author_not_found(_: Request, exc: AuthorNotFound) -> JSONResponse:
    logger.warning("404 AuthorNotFound: %s", exc)
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(UpstreamError)
def handle_upstream_error(_: Request, exc: UpstreamError) -> JSONResponse:
    logger.error("502 UpstreamError: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return JSONResponse(content="", media_type="application/javascript")


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.get("/sentry-debug", include_in_schema=False)
def sentry_debug():
    if os.getenv("ENVIRONMENT", "production") == "production":
        raise HTTPException(status_code=404, detail="Not Found")
    1 / 0


app.include_router(opds_router)
