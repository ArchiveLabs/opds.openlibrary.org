from __future__ import annotations

from typing import Optional
import asyncio
import inspect
import time

import httpx
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse

from pyopds2 import Catalog, Link, Metadata
from pyopds2_openlibrary import OpenLibraryDataProvider, fetch_author_bio

from app.config import (
    ENVIRONMENT,
    OL_BASE_URL,
    OL_REQUEST_TIMEOUT,
    OL_USER_AGENT,
    OPDS_BASE_URL,
    OPDS_MEDIA_TYPE,
    OPDS_PUB_MEDIA_TYPE,
)
from app.exceptions import AuthorNotFound, EditionNotFound, UpstreamError
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

_home_cache: dict[str, tuple[float, dict]] = {}
HOME_CACHE_TTL = 1 * 60 * 60  # 1 hours


def _safe_total(value: object) -> int:
    """Return a non-negative integer total for pagination safety."""
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _base_url(request: Request) -> str:
    if OPDS_BASE_URL:
        return OPDS_BASE_URL.rstrip("/")
    return str(request.base_url).rstrip("/")


def _common_links(base: str) -> list[Link]:
    """Links shared across catalog responses (search template, shelf, profile)."""
    return [
        Link(rel="search", href=f"{base}/search{{?query}}", type=OPDS_MEDIA_TYPE, templated=True),
        Link(rel="http://opds-spec.org/shelf",
             href="https://archive.org/services/loans/loan/?action=user_bookshelf",
             type=OPDS_MEDIA_TYPE),
        Link(rel="profile",
             href="https://archive.org/services/loans/loan/?action=user_profile",
             type="application/opds-profile+json"),
    ]


def get_provider(base: str) -> OpenLibraryDataProvider:
    OpenLibraryDataProvider.OL_BASE_URL = OL_BASE_URL
    OpenLibraryDataProvider.USER_AGENT = OL_USER_AGENT
    OpenLibraryDataProvider.REQUEST_TIMEOUT = OL_REQUEST_TIMEOUT
    OpenLibraryDataProvider.SEARCH_URL = f"{base}/search"
    OpenLibraryDataProvider.OPDS_BASE_URL = base
    return OpenLibraryDataProvider()


def opds_response(data: dict) -> JSONResponse:
    return JSONResponse(content=data, media_type=OPDS_MEDIA_TYPE)


def opds_pub_response(data: dict) -> JSONResponse:
    return JSONResponse(content=data, media_type=OPDS_PUB_MEDIA_TYPE)


def _search(provider: OpenLibraryDataProvider, **kwargs):
    try:
        logger.info("search query=%r limit=%s offset=%s sort=%s",
                    kwargs.get("query"), kwargs.get("limit"),
                    kwargs.get("offset", 0), kwargs.get("sort"))
        return _call_provider_compat(provider.search, **kwargs)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.error("upstream HTTP error status=%s url=%s", status_code, exc.request.url)
        raise UpstreamError(
            f"OpenLibrary returned {status_code}",
            status_code=status_code,
        ) from exc
    except httpx.RequestError as exc:
        logger.error("upstream request error: %s", exc)
        raise UpstreamError(f"Could not reach OpenLibrary: {exc}") from exc


def _call_provider_compat(func, **kwargs):
    """Call provider methods while tolerating older signatures.

    This keeps newer route parameters (like ``access``) from crashing when
    an older ``pyopds2_openlibrary`` version is imported at runtime.
    """
    signature = inspect.signature(func)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return func(**kwargs)

    supported = {k: v for k, v in kwargs.items() if k in signature.parameters}
    skipped = sorted(set(kwargs) - set(supported))
    if skipped:
        logger.warning(
            "provider call %s does not support parameters %s; ignoring them",
            getattr(func, "__qualname__", getattr(func, "__name__", str(func))),
            skipped,
        )
    return func(**supported)


@router.get("/", summary="OPDS 2.0 homepage")
async def opds_home(
    request: Request,
    mode: str = Query(default="everything", description="Availability filter: everything, ebooks, open_access, buyable"),
    language: Optional[str] = Query(default=None, description="BCP 47 language filter (e.g. 'en'). Omit for all languages."),
    page: int = Query(default=1, ge=1, description="Group page (each page loads a batch of carousels)"),
    media_type: Optional[str] = Query(default=None, description="Media type filter: ebook, audiobook. Omit for all."),
    access: Optional[str] = Query(default=None, description="Access filter: general (default), print_disabled."),
):
    logger.info("GET / client=%s language=%s page=%s media_type=%s access=%s", request.client, language, page, media_type, access)
    base = _base_url(request)

    # Only cache the fully-default first page.
    is_default = mode == "everything" and language is None and page == 1 and media_type is None and access is None
    cached = _home_cache.get(base)
    if ENVIRONMENT != "development" and is_default and cached and (time.monotonic() - cached[0]) < HOME_CACHE_TTL:
        logger.info("serving cached homepage for base=%s", base)
        return opds_response(cached[1])

    get_provider(base)

    data = await asyncio.to_thread(
        _call_provider_compat,
        OpenLibraryDataProvider.build_home_feed,
        base=base,
        mode=mode,
        language=language,
        page=page,
        media_type=media_type,
        access=access,
    )

    if ENVIRONMENT != "development" and is_default:
        _home_cache[base] = (time.monotonic(), data)
    return opds_response(data)


@router.get("/search", summary="OPDS 2.0 search")
async def opds_search(
    request: Request,
    query: str = Query(default="trending_score_hourly_sum:[1 TO *]", description="Solr search query"),
    limit: int = Query(default=25, ge=1, le=100),
    page: int = Query(default=1, ge=1),
    sort: Optional[str] = Query(default=None),
    mode: str = Query(default="everything", description="Search mode, e.g. 'ebooks' or 'everything'"),
    title: Optional[str] = Query(default=None, description="Display title for the results page"),
    language: Optional[str] = Query(default=None, description="BCP 47 language filter (e.g. 'en'). Omit for all languages."),
    media_type: Optional[str] = Query(default=None, description="Media type filter: ebook, audiobook. Omit for all."),
    access: Optional[str] = Query(default=None, description="Access filter: general (default), print_disabled."),
):
    logger.info("GET /search query=%r limit=%s page=%s sort=%s mode=%s language=%s media_type=%s access=%s", query, limit, page, sort, mode, language, media_type, access)
    base = _base_url(request)
    provider = get_provider(base)

    self_href = f"{base}/search?{request.url.query}" if request.url.query else f"{base}/search"

    def _fetch_facet_counts_safe(q: str) -> dict:
        try:
            return OpenLibraryDataProvider.fetch_facet_counts(q, media_type=media_type)
        except Exception as exc:
            logger.warning("facet count fetch failed, omitting counts: %s", exc)
            return {}

    search_response, availability_counts = await asyncio.gather(
        asyncio.to_thread(
            _search,
            provider,
            query=query,
            limit=limit,
            offset=(page - 1) * limit,
            sort=sort,
            facets={"mode": mode},
            language=language,
            title=title,
            require_cover=False,
            media_type=media_type,
            access=access,
        ),
        asyncio.to_thread(_fetch_facet_counts_safe, query),
    )

    safe_total = _safe_total(getattr(search_response, "total", None))
    if safe_total != getattr(search_response, "total", None):
        logger.warning("search response returned invalid total=%r; defaulting to 0", getattr(search_response, "total", None))
    search_response.total = safe_total

    # The main search already gives us the exact count for the active mode,
    # so patch it in to avoid any mismatch.
    availability_counts[mode] = safe_total

    catalog = Catalog.create(
        metadata=Metadata(title=title or "Search Results"),
        response=search_response,
        links=[
            Link(rel="self", href=self_href, type=OPDS_MEDIA_TYPE),
            *_common_links(base),
        ],
        facets=_call_provider_compat(
            OpenLibraryDataProvider.build_facets,
            base_url=base,
            query=query,
            sort=sort,
            mode=mode,
            language=language,
            title=title,
            total=safe_total,
            availability_counts=availability_counts,
            media_type=media_type,
            access=access,
        ),
    )
    return opds_response(catalog.model_dump())


@router.get("/books/{edition_olid}", summary="OPDS 2.0 single edition")
async def opds_books(request: Request, edition_olid: str):
    logger.info("GET /books/%s", edition_olid)
    base = _base_url(request)
    provider = get_provider(base)
    resp = await asyncio.to_thread(_search, provider, query=f"edition_key:{edition_olid}", require_cover=False)
    if not resp.records:
        logger.warning("edition not found: %s", edition_olid)
        raise EditionNotFound(edition_olid)
    pub = resp.records[0].to_publication()
    return opds_pub_response(pub.model_dump())


@router.get("/authors/{olid}", summary="OPDS 2.0 author catalog")
async def opds_authors(
    request: Request,
    olid: str = Path(..., pattern=r"^OL\d+A$"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=100),
    mode: str = Query(default="everything"),
    language: Optional[str] = Query(default=None, description="BCP 47 language filter (e.g. 'en'). Omit for all languages."),
    media_type: Optional[str] = Query(default=None, description="Media type filter: ebook, audiobook. Omit for all."),
    access: Optional[str] = Query(default=None, description="Access filter: general (default), print_disabled."),
):
    logger.info("GET /authors/%s page=%s limit=%s mode=%s language=%s media_type=%s access=%s", olid, page, limit, mode, language, media_type, access)
    base = _base_url(request)
    provider = get_provider(base)

    (author_name, author_bio), search_response = await asyncio.gather(
        asyncio.to_thread(fetch_author_bio, olid),
        asyncio.to_thread(
            _search, provider,
            query=f"author_key:{olid}",
            limit=limit,
            offset=(page - 1) * limit,
            facets={"mode": mode},
            language=language,
            media_type=media_type,
            require_cover=False,
            access=access,
        ),
    )

    if not search_response.records and author_name is None and author_bio is None:
        raise AuthorNotFound(olid)

    def _author_page_href(p: int) -> str:
        params: dict[str, str] = {}
        if p > 1:
            params["page"] = str(p)
        if limit != 25:
            params["limit"] = str(limit)
        if mode != "everything":
            params["mode"] = mode
        if language:
            params["language"] = language
        if media_type:
            params["media_type"] = media_type
        if access and access != "general":
            params["access"] = access
        return f"{base}/authors/{olid}?{urlencode(params)}" if params else f"{base}/authors/{olid}"

    catalog_links: list[Link] = [
        Link(rel="self", href=_author_page_href(page), type=OPDS_MEDIA_TYPE),
        Link(rel="first", href=_author_page_href(1), type=OPDS_MEDIA_TYPE),
        *_common_links(base),
    ]
    if page > 1:
        catalog_links.append(Link(rel="previous", href=_author_page_href(page - 1), type=OPDS_MEDIA_TYPE))
    if search_response.has_more:
        catalog_links.append(Link(rel="next", href=_author_page_href(page + 1), type=OPDS_MEDIA_TYPE))

    catalog = Catalog.create(
        metadata=Metadata(
            title=author_name or olid,
            description=author_bio,
            numberOfItems=search_response.total,
            itemsPerPage=limit,
            currentPage=page,
        ),
        response=search_response,
        paginate=False,
        links=catalog_links,
        facets=_call_provider_compat(
            OpenLibraryDataProvider.build_author_facets,
            base_url=base,
            olid=olid,
            mode=mode,
            language=language,
            media_type=media_type,
            page=page,
            limit=limit,
            access=access,
        ),
    )
    return opds_response(catalog.model_dump())
