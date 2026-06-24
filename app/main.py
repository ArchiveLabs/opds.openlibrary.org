from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.cache import get_cache, LANG_OPTIONS_KEY, TTL_LANG_OPTIONS_SECONDS
from app.exceptions import AuthorNotFound, EditionNotFound, UpstreamError
from app.logger import get_logger
from app.routes.opds import router as opds_router
from app.sentry import init_sentry
from app.config import CORS_ENABLED, ENVIRONMENT

logger = get_logger(__name__)

sentry_enabled = init_sentry()


def _warm_language_cache() -> None:
    """On startup: warm pyopds2_openlibrary's in-process language cache.

    Tries Memcached first (fast, survives restarts). Falls back to fetching
    from OL and storing the result in Memcached for the next startup.
    """
    import pyopds2_openlibrary as _ol
    cache = get_cache()

    cached_data = cache.get(LANG_OPTIONS_KEY)
    if cached_data:
        _ol._languages_map_cache = cached_data["map"]
        _ol._languages_names_cache = cached_data["names"]
        _ol._languages_map_fetched_at = time.monotonic()
        logger.info("language cache warmed from Memcached (%d languages)", len(cached_data["map"]))
        return

    try:
        _ol.fetch_languages_map()
        if _ol._languages_map_cache:
            cache.set(LANG_OPTIONS_KEY, {
                "map": _ol._languages_map_cache,
                "names": _ol._languages_names_cache,
            }, TTL_LANG_OPTIONS_SECONDS)
            logger.info("language map fetched from OL on startup (%d languages)", len(_ol._languages_map_cache))
    except Exception as exc:
        logger.warning("could not warm language cache on startup: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("OPDS service starting up (sentry=%s)", sentry_enabled)
    if ENVIRONMENT != "test":
        await asyncio.to_thread(_warm_language_cache)
    yield


app = FastAPI(
    title="Open Library OPDS 2.0",
    description="Stand-alone OPDS 2.0 feed for Open Library",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# OPDS is a public read-only catalog API. In production the fronting nginx
# already supplies CORS headers; enabling this here would duplicate
# ``Access-Control-Allow-Origin`` and break browser clients. Gate it behind
# CORS_ENABLED so local dev without nginx (e.g. the Cloudflare tunnel to
# reader.archive.org) can still be consumed by browsers.
if CORS_ENABLED:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )


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
