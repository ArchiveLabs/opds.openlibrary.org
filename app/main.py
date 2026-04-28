from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.exceptions import AuthorNotFound, EditionNotFound, UpstreamError
from app.logger import get_logger
from app.routes.opds import router as opds_router
from app.sentry import init_sentry

logger = get_logger(__name__)

sentry_enabled = init_sentry()


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("OPDS service starting up (sentry=%s)", sentry_enabled)
    yield


app = FastAPI(
    title="Open Library OPDS 2.0",
    description="Stand-alone OPDS 2.0 feed for Open Library",
    version="0.1.0",
    lifespan=lifespan,
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
    # Evaluate env at request time so tests and CI overrides are honored.
    if os.getenv("ENVIRONMENT", "development") == "production":
        raise HTTPException(status_code=404, detail="Not Found")
    1 / 0


app.include_router(opds_router)
