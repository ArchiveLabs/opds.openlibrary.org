from __future__ import annotations

from typing import Optional
import asyncio
import inspect

import httpx
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse

from pyopds2 import Catalog, Link, Metadata
from pyopds2_openlibrary import OpenLibraryDataProvider, fetch_author_bio

import pyopds2_openlibrary as _ol_module
from app.cache import (
    CacheBackend,
    LANG_OPTIONS_KEY,
    TTL_AUTHOR_BIO_SECONDS,
    TTL_AUTHOR_CATALOG_SECONDS,
    TTL_BOOK_SECONDS,
    TTL_HOME_DEFAULT_SECONDS,
    TTL_HOME_DEFAULT_STALE_SECONDS,
    TTL_HOME_NONDEFAULT_SECONDS,
    TTL_LANG_OPTIONS_SECONDS,
    TTL_TRENDING_SECONDS,
    TTL_TRENDING_STALE_SECONDS,
    get_cache,
    make_key,
)
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


try:
    from pyopds2_openlibrary import _GROUP_DESCRIPTIONS as _OL_GROUP_DESCRIPTIONS
except ImportError:
    _OL_GROUP_DESCRIPTIONS: dict = {}


@router.get("/", summary="OPDS 2.0 homepage")
async def opds_home(
    request: Request,
    mode: str = Query(default="everything", description="Availability filter: everything, ebooks, open_access, buyable"),
    language: Optional[str] = Query(default=None, description="BCP 47 language filter (e.g. 'en'). Omit for all languages."),
    page: int = Query(default=1, ge=1, description="Group page (each page loads a batch of carousels)"),
    media_type: Optional[str] = Query(default=None, description="Media type filter: ebook, audiobook. Omit for all."),
    access: Optional[str] = Query(default=None, description="Access filter: general (default), print_disabled."),
    cache: CacheBackend = Depends(get_cache),
):
    logger.info("GET / client=%s language=%s page=%s media_type=%s access=%s", request.client, language, page, media_type, access)
    base = _base_url(request)
    provider = get_provider(base)

    is_default = mode == "everything" and language is None and page == 1 and media_type is None and access is None
    ttl = TTL_HOME_DEFAULT_SECONDS if is_default else TTL_HOME_NONDEFAULT_SECONDS

    # Each page cached separately — page is included in the key.
    home_key = make_key("home", {
        "base": base, "mode": mode, "language": language,
        "page": page, "media_type": media_type, "access": access,
    })
    # Trending group gets its own short-TTL key shared across pages/base variants.
    trending_key = make_key("home_trending", {
        "mode": mode, "language": language,
        "media_type": media_type, "access": access,
    })

    async def _fetch_full() -> dict:
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
        # Refresh language options in Memcached while we have fresh in-process data.
        if _ol_module._languages_map_cache:
            cache.set(LANG_OPTIONS_KEY, {
                "map": _ol_module._languages_map_cache,
                "names": _ol_module._languages_names_cache,
            }, TTL_LANG_OPTIONS_SECONDS)
        return data

    async def _fetch_trending() -> dict:
        """Fetch the Trending Books group independently for short-TTL refresh.

        Always populates trending_key so the overlay has a consistent structure.
        Uses _search() to get proper httpx error → UpstreamError wrapping.
        """
        groups_config = OpenLibraryDataProvider._home_groups_config(mode, language)
        trending = next((g for g in groups_config if g[0] == "Trending Books"), None)
        if not trending:
            return {}
        t_title, t_query, t_sort = trending
        resp = await asyncio.to_thread(
            _search,
            provider,
            query=t_query,
            sort=t_sort,
            limit=25,
            language=language,
            facets={"mode": mode},
            title=t_title,
            require_cover=False,
            media_type=media_type,
            access=access,
        )
        group_catalog = Catalog.create(
            metadata=Metadata(title=t_title, description=_OL_GROUP_DESCRIPTIONS.get(t_title)),
            response=resp,
        )
        return group_catalog.model_dump()

<<<<<<< HEAD
    # Full page cached at long TTL (stable carousels).
    data = await cache.cached(home_key, ttl, _fetch_full)

    # Trending group overlaid at short TTL — only needed on pages that contain it.
    groups = data.get("groups", [])
    if any(g.get("metadata", {}).get("title") == "Trending Books" for g in groups):
        fresh_trending = await cache.cached(trending_key, TTL_TRENDING_SECONDS, _fetch_trending)
=======
    # Full page served via stale-while-revalidate for default mode (hot path);
    # other variants stay on plain TTL since they are cold and rarely hit twice.
    if is_default:
        data = await cache.cached_swr(
            home_key, ttl, TTL_HOME_DEFAULT_STALE_SECONDS, _fetch_full
        )
    else:
        data = await cache.cached(home_key, ttl, _fetch_full)

    # Trending group overlaid via SWR so the 60s refresh never blocks a user.
    groups = data.get("groups", [])
    if any(g.get("metadata", {}).get("title") == "Trending Books" for g in groups):
        fresh_trending = await cache.cached_swr(
            trending_key, TTL_TRENDING_SECONDS, TTL_TRENDING_STALE_SECONDS, _fetch_trending
        )
>>>>>>> main
        if fresh_trending:
            data = {**data, "groups": [
                fresh_trending if g.get("metadata", {}).get("title") == "Trending Books" else g
                for g in groups
            ]}

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
    cache: CacheBackend = Depends(get_cache),
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

    async def _fetch() -> dict:
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
        return catalog.model_dump()

    data = await _fetch()
    return opds_response(data)


@router.get("/books/{edition_olid}", summary="OPDS 2.0 single edition")
async def opds_books(
    request: Request,
    edition_olid: str,
    cache: CacheBackend = Depends(get_cache),
):
    logger.info("GET /books/%s", edition_olid)
    base = _base_url(request)
    provider = get_provider(base)

    key = make_key("book", {"edition_olid": edition_olid})

    async def _fetch() -> dict:
        resp = await asyncio.to_thread(_search, provider, query=f"edition_key:{edition_olid}", require_cover=False)
        if not resp.records:
            logger.warning("edition not found: %s", edition_olid)
            raise EditionNotFound(edition_olid)
        return resp.records[0].to_publication().model_dump()

    data = await cache.cached(key, TTL_BOOK_SECONDS, _fetch)
    return opds_pub_response(data)


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
    cache: CacheBackend = Depends(get_cache),
):
    logger.info("GET /authors/%s page=%s limit=%s mode=%s language=%s media_type=%s access=%s", olid, page, limit, mode, language, media_type, access)
    base = _base_url(request)
    provider = get_provider(base)

    bio_key = make_key("author_bio", {"olid": olid})
    catalog_key = make_key("author_catalog", {
        "base": base, "olid": olid, "page": page, "limit": limit, "mode": mode,
        "language": language, "media_type": media_type, "access": access,
    })

    async def _fetch_bio() -> dict:
        name, bio = await asyncio.to_thread(fetch_author_bio, olid)
        return {"name": name, "bio": bio}

    async def _fetch_catalog() -> dict:
        bio_data, search_response = await asyncio.gather(
            cache.cached(bio_key, TTL_AUTHOR_BIO_SECONDS, _fetch_bio),
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

        author_name = bio_data["name"]
        author_bio = bio_data["bio"]

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
        return catalog.model_dump()

    data = await cache.cached(catalog_key, TTL_AUTHOR_CATALOG_SECONDS, _fetch_catalog)
    return opds_response(data)
