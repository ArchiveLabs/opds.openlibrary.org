from __future__ import annotations

from typing import Optional

import httpx
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


def get_provider(base_url: str) -> OpenLibraryDataProvider:
    OpenLibraryDataProvider.BASE_URL = base_url
    OpenLibraryDataProvider.OL_BASE_URL = OL_BASE_URL
    OpenLibraryDataProvider.USER_AGENT = OL_USER_AGENT
    OpenLibraryDataProvider.REQUEST_TIMEOUT = OL_REQUEST_TIMEOUT
    OpenLibraryDataProvider.SEARCH_URL = "/opds/search"
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
        logger.error("upstream HTTP error status=%s url=%s",
                     exc.response.status_code, exc.request.url)
        raise UpstreamError(
            f"OpenLibrary returned {exc.response.status_code}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.RequestError as exc:
        logger.error("upstream request error: %s", exc)
        raise UpstreamError(f"Could not reach OpenLibrary: {exc}") from exc


@router.get("/opds", summary="OPDS 2.0 homepage")
def opds_home(request: Request):
    logger.info("GET /opds client=%s", request.client)
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

    loaded_groups = []
    for title, q, sort in groups_config:
        try:
            resp = _search(provider, query=q, sort=sort, limit=25)
            loaded_groups.append(
                Catalog.create(metadata=Metadata(title=title), response=resp)
            )
        except UpstreamError as exc:
            logger.warning("Omitting shelf %r due to upstream error: %s", title, exc)

    catalog = Catalog(
        metadata=Metadata(title="Open Library"),
        publications=[],
        navigation=[
            Navigation(
                type=OPDS_MEDIA_TYPE,
                title=subject["presentable_name"],
                href=(
                    f"{base}{search_url}?sort=trending"
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
            Link(rel="self",   href=f"{base}/opds",                  type=OPDS_MEDIA_TYPE),
            Link(rel="start",  href=f"{base}/opds",                  type=OPDS_MEDIA_TYPE),
            Link(rel="search", href=f"{base}/opds/search{{?query}}", type=OPDS_MEDIA_TYPE, templated=True),
            Link(rel="http://opds-spec.org/shelf",
                 href="https://archive.org/services/loans/loan/?action=user_bookshelf",
                 type=OPDS_MEDIA_TYPE),
            Link(rel="profile",
                 href="https://archive.org/services/loans/loan/?action=user_profile",
                 type="application/opds-profile+json"),
        ],
    )
    return opds_response(catalog.model_dump())


@router.get("/opds/search", summary="OPDS 2.0 search")
def opds_search(
    request: Request,
    query: str = Query(default="trending_score_hourly_sum:[1 TO *]", description="Solr search query"),
    limit: int = Query(default=25, ge=1, le=100),
    page: int = Query(default=1, ge=1),
    sort: Optional[str] = Query(default=None),
):
    logger.info("GET /opds/search query=%r limit=%s page=%s sort=%s", query, limit, page, sort)
    base = _base_url(request)
    provider = get_provider(base)

    catalog = Catalog.create(
        metadata=Metadata(title="Search Results"),
        response=_search(
            provider,
            query=query,
            limit=limit,
            offset=(page - 1) * limit,
            sort=sort,
        ),
        links=[
            Link(rel="self",   href=str(request.url),                type=OPDS_MEDIA_TYPE),
            Link(rel="search", href=f"{base}/opds/search{{?query}}", type=OPDS_MEDIA_TYPE, templated=True),
            Link(rel="http://opds-spec.org/shelf",
                 href="https://archive.org/services/loans/loan/?action=user_bookshelf",
                 type=OPDS_MEDIA_TYPE),
            Link(rel="profile",
                 href="https://archive.org/services/loans/loan/?action=user_profile",
                 type="application/opds-profile+json"),
        ],
    )
    return opds_response(catalog.model_dump())


@router.get("/opds/books/{edition_olid}", summary="OPDS 2.0 single edition")
def opds_books(request: Request, edition_olid: str):
    logger.info("GET /opds/books/%s", edition_olid)
    base = _base_url(request)
    provider = get_provider(base)
    resp = _search(provider, query=f"edition_key:{edition_olid}")
    if not resp.records:
        logger.warning("edition not found: %s", edition_olid)
        raise EditionNotFound(edition_olid)
    pub = resp.records[0].to_publication()
    return opds_pub_response(pub.model_dump())
