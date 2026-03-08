from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.exceptions import EditionNotFound, UpstreamError
from app.logger import get_logger
from app.routes.opds import router as opds_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("OPDS service starting up")
    yield


app = FastAPI(
    title="Open Library OPDS 2.0",
    description="Stand-alone OPDS 2.0 feed for Open Library",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(EditionNotFound)
def handle_edition_not_found(_: Request, exc: EditionNotFound) -> JSONResponse:
    logger.warning("404 EditionNotFound: %s", exc)
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(UpstreamError)
def handle_upstream_error(_: Request, exc: UpstreamError) -> JSONResponse:
    logger.error("502 UpstreamError: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


app.include_router(opds_router)
