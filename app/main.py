"""
opds.openlibrary.org — stand-alone FastAPI OPDS 2.0 service.

Implements the same OPDS 2 endpoints as openlibrary/plugins/openlibrary/api.py,
backed by pyopds2_openlibrary.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pyopds2 import Catalog, Link, Metadata, Navigation
from pyopds2_openlibrary import OpenLibraryDataProvider

app = FastAPI(
    title="Open Library OPDS 2.0",
    description="Stand-alone OPDS 2.0 feed for Open Library",
    version="0.1.0",
)

OPDS_MEDIA_TYPE = "application/opds+json"
OPDS_PUB_MEDIA_TYPE = "application/opds-publication+json"

# Allow the public-facing base URL to be overridden via an environment variable
# (useful behind a reverse-proxy).  Falls back to the request's own base URL.
_BASE_URL_OVERRIDE: str | None = os.environ.get("OPDS_BASE_URL")

# Featured subjects shown in the navigation section of the homepage.
FEATURED_SUBJECTS = [
    {"key": "/subjects/art", "presentable_name": "Art"},
    {"key": "/subjects/science_fiction", "presentable_name": "Science Fiction"},
    {"key": "/subjects/fantasy", "presentable_name": "Fantasy"},
    {"key": "/subjects/biographies", "presentable_name": "Biographies"},
    {"key": "/subjects/recipes", "presentable_name": "Recipes"},
    {"key": "/subjects/romance", "presentable_name": "Romance"},
    {"key": "/subjects/textbooks", "presentable_name": "Textbooks"},
    {"key": "/subjects/children", "presentable_name": "Children"},
    {"key": "/subjects/history", "presentable_name": "History"},
    {"key": "/subjects/medicine", "presentable_name": "Medicine"},
    {"key": "/subjects/religion", "presentable_name": "Religion"},
    {"key": "/subjects/mystery_and_detective_stories", "presentable_name": "Mystery and Detective Stories"},
    {"key": "/subjects/plays", "presentable_name": "Plays"},
    {"key": "/subjects/music", "presentable_name": "Music"},
    {"key": "/subjects/science", "presentable_name": "Science"},
]


def _base_url(request: Request) -> str:
    """Return the effective public base URL for self-referencing links."""
    if _BASE_URL_OVERRIDE:
        return _BASE_URL_OVERRIDE.rstrip("/")
    return str(request.base_url).rstrip("/")


def get_provider(base_url: str) -> OpenLibraryDataProvider:
    """Return a configured OpenLibraryDataProvider for the given base URL."""
    OpenLibraryDataProvider.BASE_URL = base_url
    OpenLibraryDataProvider.SEARCH_URL = "/opds/search"
    return OpenLibraryDataProvider()


def opds_response(data: dict) -> JSONResponse:
    return JSONResponse(content=data, media_type=OPDS_MEDIA_TYPE)


def opds_pub_response(data: dict) -> JSONResponse:
    return JSONResponse(content=data, media_type=OPDS_PUB_MEDIA_TYPE)


@app.get("/opds", summary="OPDS 2.0 homepage")
def opds_home(request: Request):
    """
    Returns the OPDS 2.0 homepage catalog with:
    - Navigation links for featured subjects
    - Groups of curated publications (Trending, Classic, Romance, Kids, Thrillers, Textbooks)
    """
    base = _base_url(request)
    provider = get_provider(base)
    search_url = OpenLibraryDataProvider.SEARCH_URL

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
        groups=[
            Catalog.create(
                metadata=Metadata(title="Trending Books"),
                response=provider.search(
                    query=(
                        'trending_score_hourly_sum:[1 TO *]'
                        ' -subject:"content_warning:cover"'
                        ' ebook_access:[borrowable TO *]'
                        ' readinglog_count:[4 TO *]'
                    ),
                    sort="trending",
                    limit=25,
                ),
            ),
            Catalog.create(
                metadata=Metadata(title="Classic Books"),
                response=provider.search(
                    query=(
                        'ddc:8* first_publish_year:[* TO 1950]'
                        ' publish_year:[2000 TO *]'
                        ' NOT public_scan_b:false'
                        ' -subject:"content_warning:cover"'
                    ),
                    sort="trending",
                    limit=25,
                ),
            ),
            Catalog.create(
                metadata=Metadata(title="Romance"),
                response=provider.search(
                    query=(
                        'subject:romance ebook_access:[borrowable TO *]'
                        ' first_publish_year:[1930 TO *]'
                        ' trending_score_hourly_sum:[1 TO *]'
                        ' -subject:"content_warning:cover"'
                    ),
                    sort="trending,trending_score_hourly_sum",
                    limit=25,
                ),
            ),
            Catalog.create(
                metadata=Metadata(title="Kids"),
                response=provider.search(
                    query=(
                        'ebook_access:[borrowable TO *]'
                        ' trending_score_hourly_sum:[1 TO *]'
                        ' (subject_key:(juvenile_audience OR children\'s_fiction'
                        " OR juvenile_nonfiction OR juvenile_encyclopedias"
                        " OR juvenile_riddles OR juvenile_poetry"
                        " OR juvenile_wit_and_humor OR juvenile_limericks"
                        " OR juvenile_dictionaries OR juvenile_non-fiction)"
                        ' OR subject:("Juvenile literature" OR "Juvenile fiction"'
                        ' OR "pour la jeunesse" OR "pour enfants"))'
                    ),
                    sort="random.hourly",
                    limit=25,
                ),
            ),
            Catalog.create(
                metadata=Metadata(title="Thrillers"),
                response=provider.search(
                    query=(
                        'subject:thrillers ebook_access:[borrowable TO *]'
                        ' trending_score_hourly_sum:[1 TO *]'
                        ' -subject:"content_warning:cover"'
                    ),
                    sort="trending,trending_score_hourly_sum",
                    limit=25,
                ),
            ),
            Catalog.create(
                metadata=Metadata(title="Textbooks"),
                response=provider.search(
                    query=(
                        'subject_key:textbooks publish_year:[1990 TO *]'
                        ' ebook_access:[borrowable TO *]'
                    ),
                    sort="trending",
                    limit=25,
                ),
            ),
        ],
        facets=None,
        links=[
            Link(
                rel="self",
                href=f"{base}/opds",
                type=OPDS_MEDIA_TYPE,
            ),
            Link(
                rel="start",
                href=f"{base}/opds",
                type=OPDS_MEDIA_TYPE,
            ),
            Link(
                rel="search",
                href=f"{base}/opds/search{{?query}}",
                type=OPDS_MEDIA_TYPE,
                templated=True,
            ),
            Link(
                rel="http://opds-spec.org/shelf",
                href="https://archive.org/services/loans/loan/?action=user_bookshelf",
                type=OPDS_MEDIA_TYPE,
            ),
            Link(
                rel="profile",
                href="https://archive.org/services/loans/loan/?action=user_profile",
                type="application/opds-profile+json",
            ),
        ],
    )
    return opds_response(catalog.model_dump())


@app.get("/opds/search", summary="OPDS 2.0 search")
def opds_search(
    request: Request,
    query: str = Query(
        default="trending_score_hourly_sum:[1 TO *]",
        description="Solr search query",
    ),
    limit: int = Query(default=25, ge=1, le=100),
    page: int = Query(default=1, ge=1),
    sort: Optional[str] = Query(default=None),
):
    """
    Search Open Library and return an OPDS 2.0 catalog.

    Uses the Open Library Solr search API under the hood.
    """
    base = _base_url(request)
    provider = get_provider(base)

    catalog = Catalog.create(
        metadata=Metadata(title="Search Results"),
        response=provider.search(
            query=query,
            limit=limit,
            offset=(page - 1) * limit,
            sort=sort,
        ),
        links=[
            Link(
                rel="self",
                href=str(request.url),
                type=OPDS_MEDIA_TYPE,
            ),
            Link(
                rel="search",
                href=f"{base}/opds/search{{?query}}",
                type=OPDS_MEDIA_TYPE,
                templated=True,
            ),
            Link(
                rel="http://opds-spec.org/shelf",
                href="https://archive.org/services/loans/loan/?action=user_bookshelf",
                type=OPDS_MEDIA_TYPE,
            ),
            Link(
                rel="profile",
                href="https://archive.org/services/loans/loan/?action=user_profile",
                type="application/opds-profile+json",
            ),
        ],
    )
    return opds_response(catalog.model_dump())


@app.get("/opds/books/{edition_olid}", summary="OPDS 2.0 single edition")
def opds_books(request: Request, edition_olid: str):
    """
    Return an OPDS 2.0 publication record for a single Open Library edition.

    The edition OLID must be in the format `OL{n}M` (e.g. `OL7353617M`).
    """
    base = _base_url(request)
    provider = get_provider(base)
    resp = provider.search(query=f"edition_key:{edition_olid}")
    if not resp.records:
        raise HTTPException(status_code=404, detail="Edition not found")
    pub = resp.records[0].to_publication()
    return opds_pub_response(pub.model_dump())
