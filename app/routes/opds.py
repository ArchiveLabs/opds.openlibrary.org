from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode
import asyncio
import time

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from pyopds2 import Catalog, Link, Metadata, Navigation
from pyopds2_openlibrary import OpenLibraryDataProvider

from app.config import (
    ENVIRONMENT,
    FEATURED_SUBJECTS,
    OL_BASE_URL,
    OL_REQUEST_TIMEOUT,
    OL_USER_AGENT,
    OPDS_BASE_URL,
    OPDS_MEDIA_TYPE,
    OPDS_PUB_MEDIA_TYPE,
)
from app.exceptions import EditionNotFound, UpstreamError
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


def _home_self_href(base: str, mode: str, language: Optional[str]) -> str:
    """Build the self-link href for the homepage catalog."""
    params: dict[str, str] = {}
    if mode != "everything":
        params["mode"] = mode
    if language:
        params["language"] = language
    return f"{base}/?{urlencode(params)}" if params else f"{base}/"


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
        return provider.search(**kwargs)
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


@router.get("/", summary="OPDS 2.0 homepage")
async def opds_home(
    request: Request,
    mode: str = Query(default="everything", description="Availability filter: everything, ebooks, open_access, buyable"),
    language: Optional[str] = Query(default=None, description="BCP 47 language filter (e.g. 'en'). Omit for all languages."),
):
    logger.info("GET / client=%s language=%s", request.client, language)
    base = _base_url(request)

    # Only cache the fully-default homepage (no mode filter, no language filter).
    is_default = mode == "everything" and language is None
    cached = _home_cache.get(base)
    if ENVIRONMENT != "development" and is_default and cached and (time.monotonic() - cached[0]) < HOME_CACHE_TTL:
        logger.info("serving cached homepage for base=%s", base)
        return opds_response(cached[1])

    provider = get_provider(base)
    search_url = OpenLibraryDataProvider.SEARCH_URL

    # Mode-aware ebook_access filter for group queries.
    # open_access uses ebook_access:public so groups populate with Standard Ebooks,
    # Project Gutenberg, etc. All other modes use the borrowable range.
    ea = "ebook_access:public" if mode == "open_access" else "ebook_access:[borrowable TO *]"

    groups_config = [
        (
            "Trending Books",
            f'trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover" {ea} readinglog_count:[4 TO *]',
            "trending",
        ),
        (
            "Classic Books",
            'ddc:8* first_publish_year:[* TO 1950] publish_year:[2000 TO *] NOT public_scan_b:false -subject:"content_warning:cover"',
            "trending",
        ),
        (
            "Romance",
            f'subject:romance {ea} first_publish_year:[1930 TO *] trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover"',
            "trending,trending_score_hourly_sum",
        ),
        (
            "Kids",
            f'{ea} trending_score_hourly_sum:[1 TO *] (subject_key:(juvenile_audience OR children\'s_fiction OR juvenile_nonfiction OR juvenile_encyclopedias OR juvenile_riddles OR juvenile_poetry OR juvenile_wit_and_humor OR juvenile_limericks OR juvenile_dictionaries OR juvenile_non-fiction) OR subject:("Juvenile literature" OR "Juvenile fiction" OR "pour la jeunesse" OR "pour enfants"))',
            "random.hourly",
        ),
        (
            "Thrillers",
            f'subject:thrillers {ea} trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover"',
            "trending,trending_score_hourly_sum",
        ),
        (
            "Textbooks",
            f'subject_key:textbooks publish_year:[1990 TO *] {ea}',
            "trending",
        ),
        (
            "Standard Ebooks",
            'publisher:"Standard Ebooks" ebook_access:public',
            "random.hourly",
        ),
    ]

    async def fetch_group(title: str, q: str, sort: str):
        try:
            resp = await asyncio.to_thread(
                _search, provider, query=q, sort=sort, limit=25,
                language=language, facets={"mode": mode}, title=title,
            )
            return Catalog.create(metadata=Metadata(title=title), response=resp)
        except UpstreamError as exc:
            logger.warning("Omitting shelf %r due to upstream error: %s", title, exc)
            return None

    results = await asyncio.gather(
        *(fetch_group(title, q, sort) for title, q, sort in groups_config)
    )
    loaded_groups = [
        g for g in results
        if g is not None and g.publications
    ]

    navigation = [
        Navigation(
            type=OPDS_MEDIA_TYPE,
            title=subject["presentable_name"],
            href=f"{search_url}?{urlencode({
                'sort': 'trending',
                'title': subject['presentable_name'],
                **({'language': language} if language else {}),
                'query': subject.get('query') or (
                    f'subject_key:{subject["key"].split("/")[-1]}'
                    f' -subject:"content_warning:cover"'
                    f' ebook_access:[borrowable TO *]'
                ),
            })}",
        )
        for subject in FEATURED_SUBJECTS
    ] if loaded_groups else []

    catalog = Catalog(
        metadata=Metadata(title="Open Library"),
        publications=[],
        navigation=navigation,
        groups=loaded_groups,
        facets=OpenLibraryDataProvider.build_home_facets(base, mode, language),
        links=[
            Link(
                rel="self",
                href=_home_self_href(base, mode, language),
                type=OPDS_MEDIA_TYPE,
            ),
            Link(rel="start", href=f"{base}/", type=OPDS_MEDIA_TYPE),
            *_common_links(base),
        ],
    )
    data = catalog.model_dump()
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
):
    logger.info("GET /search query=%r limit=%s page=%s sort=%s mode=%s language=%s", query, limit, page, sort, mode, language)
    base = _base_url(request)
    provider = get_provider(base)

    self_href = f"{base}/search?{request.url.query}" if request.url.query else f"{base}/search"

    def _fetch_facet_counts_safe(q: str) -> dict:
        try:
            return OpenLibraryDataProvider.fetch_facet_counts(q)
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
        facets=OpenLibraryDataProvider.build_facets(
            base_url=base,
            query=query,
            sort=sort,
            mode=mode,
            language=language,
            title=title,
            total=safe_total,
            availability_counts=availability_counts,
        ),
    )
    return opds_response(catalog.model_dump())


@router.get("/books/{edition_olid}", summary="OPDS 2.0 single edition")
async def opds_books(request: Request, edition_olid: str):
    logger.info("GET /books/%s", edition_olid)
    base = _base_url(request)
    provider = get_provider(base)
    resp = await asyncio.to_thread(_search, provider, query=f"edition_key:{edition_olid}", language="en")
    if not resp.records:
        logger.warning("edition not found: %s", edition_olid)
        raise EditionNotFound(edition_olid)
    pub = resp.records[0].to_publication()
    return opds_pub_response(pub.model_dump())
