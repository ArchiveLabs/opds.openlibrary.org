from __future__ import annotations

from typing import Optional
import asyncio

import httpx
import requests as requests_lib
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from pyopds2 import Catalog, Link, Metadata, Navigation
from pyopds2_openlibrary import OpenLibraryDataProvider

from app.config import (
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
        return provider.search(**kwargs)
    except (httpx.HTTPStatusError, requests_lib.exceptions.HTTPError) as exc:
        response = exc.response
        status_code = response.status_code if response is not None else 502
        url = getattr(exc, "request", None)
        url = url.url if url else "?"
        logger.error("upstream HTTP error status=%s url=%s", status_code, url)
        raise UpstreamError(
            f"OpenLibrary returned {status_code}",
            status_code=status_code,
        ) from exc
    except (httpx.RequestError, requests_lib.exceptions.RequestException) as exc:
        logger.error("upstream request error: %s", exc)
        raise UpstreamError(f"Could not reach OpenLibrary: {exc}") from exc


@router.get("/", summary="OPDS 2.0 homepage")
async def opds_home(request: Request):
    logger.info("GET / client=%s", request.client)
    base = _base_url(request)
    provider = get_provider(base)
    search_url = OpenLibraryDataProvider.SEARCH_URL

    groups_config = [
        (
            "Trending Books",
            'trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover" ebook_access:[borrowable TO *] readinglog_count:[4 TO *]',
            "trending",
        ),
        (
            "Classic Books",
            'ddc:8* first_publish_year:[* TO 1950] publish_year:[2000 TO *] NOT public_scan_b:false -subject:"content_warning:cover"',
            "trending",
        ),
        (
            "Romance",
            'subject:romance ebook_access:[borrowable TO *] first_publish_year:[1930 TO *] trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover"',
            "trending,trending_score_hourly_sum",
        ),
        (
            "Kids",
            'ebook_access:[borrowable TO *] trending_score_hourly_sum:[1 TO *] (subject_key:(juvenile_audience OR children\'s_fiction OR juvenile_nonfiction OR juvenile_encyclopedias OR juvenile_riddles OR juvenile_poetry OR juvenile_wit_and_humor OR juvenile_limericks OR juvenile_dictionaries OR juvenile_non-fiction) OR subject:("Juvenile literature" OR "Juvenile fiction" OR "pour la jeunesse" OR "pour enfants"))',
            "random.hourly",
        ),
        (
            "Thrillers",
            'subject:thrillers ebook_access:[borrowable TO *] trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover"',
            "trending,trending_score_hourly_sum",
        ),
        (
            "Textbooks",
            'subject_key:textbooks publish_year:[1990 TO *] ebook_access:[borrowable TO *]',
            "trending",
        ),
    ]

    async def fetch_group(title: str, q: str, sort: str):
        try:
            resp = await asyncio.to_thread(_search, provider, query=q, sort=sort, limit=25)
            return Catalog.create(metadata=Metadata(title=title), response=resp)
        except UpstreamError as exc:
            logger.warning("Omitting shelf %r due to upstream error: %s", title, exc)
            return None

    results = await asyncio.gather(
        *(fetch_group(title, q, sort) for title, q, sort in groups_config)
    )
    loaded_groups = [g for g in results if g is not None]

    catalog = Catalog(
        metadata=Metadata(title="Open Library"),
        publications=[],
        navigation=[
            Navigation(
                type=OPDS_MEDIA_TYPE,
                title=subject["presentable_name"],
                href=(
                    f"{search_url}?sort=trending"
                    f"&query=subject_key:{subject['key'].split('/')[-1]}"
                    f' -subject:"content_warning:cover"'
                    f" ebook_access:[borrowable TO *]"
                ),
            )
            for subject in FEATURED_SUBJECTS
        ],
        groups=loaded_groups,
        facets=None,
        links=[
            Link(rel="self",  href=f"{base}/", type=OPDS_MEDIA_TYPE),
            Link(rel="start", href=f"{base}/", type=OPDS_MEDIA_TYPE),
            *_common_links(base),
        ],
    )
    return opds_response(catalog.model_dump())


@router.get("/search", summary="OPDS 2.0 search")
async def opds_search(
    request: Request,
    query: str = Query(default="trending_score_hourly_sum:[1 TO *]", description="Solr search query"),
    limit: int = Query(default=25, ge=1, le=100),
    page: int = Query(default=1, ge=1),
    sort: Optional[str] = Query(default=None),
    mode: str = Query(default="everything", description="Search mode, e.g. 'ebooks' or 'everything'"),
):
    logger.info("GET /search query=%r limit=%s page=%s sort=%s mode=%s", query, limit, page, sort, mode)
    base = _base_url(request)
    provider = get_provider(base)

    self_href = f"{base}/search?{request.url.query}" if request.url.query else f"{base}/search"

    catalog = Catalog.create(
        metadata=Metadata(title="Search Results"),
        response=await asyncio.to_thread(
            _search,
            provider,
            query=query,
            limit=limit,
            offset=(page - 1) * limit,
            sort=sort,
            facets={"mode": mode},
        ),
        links=[
            Link(rel="self", href=self_href, type=OPDS_MEDIA_TYPE),
            *_common_links(base),
        ],
    )
    return opds_response(catalog.model_dump())


@router.get("/books/{edition_olid}", summary="OPDS 2.0 single edition")
async def opds_books(request: Request, edition_olid: str):
    logger.info("GET /books/%s", edition_olid)
    base = _base_url(request)
    provider = get_provider(base)
    resp = await asyncio.to_thread(_search, provider, query=f"edition_key:{edition_olid}")
    if not resp.records:
        logger.warning("edition not found: %s", edition_olid)
        raise EditionNotFound(edition_olid)
    pub = resp.records[0].to_publication()
    return opds_pub_response(pub.model_dump())
