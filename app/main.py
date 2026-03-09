from __future__ import annotations

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://localhost:5173",
        "https://reader.archive.org",
        "https://reader.archive.org/",
    ],
    allow_methods=["GET", "HEAD", "OPTIONS"],
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


app.include_router(opds_router)
