"""
Tests for the opds.openlibrary.org FastAPI service.

These tests use pytest and httpx's AsyncClient / TestClient.
Network calls to openlibrary.org are mocked so tests run offline.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests as requests_lib
from fastapi.testclient import TestClient

from app.main import app
from app.config import FEATURED_SUBJECTS

client = TestClient(app)

SEARCH_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.search"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_search_response(records=None, total=0):
    """Return a minimal DataProvider.SearchResponse-like object."""
    from pyopds2.provider import DataProvider
    from pyopds2_openlibrary import OpenLibraryDataProvider

    return DataProvider.SearchResponse(
        provider=OpenLibraryDataProvider,
        records=records or [],
        total=total,
        query="test",
        limit=25,
        offset=0,
        sort=None,
    )


def _make_record(title="Test Book", edition_key="OL1M"):
    """Return a minimal OpenLibraryDataRecord."""
    from pyopds2_openlibrary import OpenLibraryDataRecord

    return OpenLibraryDataRecord.model_validate(
        {
            "key": "/works/OL1W",
            "title": title,
            "author_name": ["Test Author"],
            "author_key": ["OL1A"],
            "editions": {
                "numFound": 1,
                "start": 0,
                "numFoundExact": True,
                "docs": [{"key": f"/books/{edition_key}", "title": title}],
            },
        }
    )


@pytest.fixture
def mock_empty_search():
    """Patch provider.search to return an empty response."""
    with patch(SEARCH_PATCH_TARGET, return_value=_make_search_response()) as m:
        yield m


@pytest.fixture
def mock_single_record():
    """Patch provider.search to return one record."""
    record = _make_record()
    with patch(
        SEARCH_PATCH_TARGET,
        return_value=_make_search_response(records=[record], total=1),
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------

class TestOpdsHome:
    def test_returns_200(self, mock_empty_search):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_content_type(self, mock_empty_search):
        resp = client.get("/")
        assert "application/opds+json" in resp.headers["content-type"]

    def test_metadata_title(self, mock_empty_search):
        data = client.get("/").json()
        assert data["metadata"]["title"] == "Open Library"

    def test_navigation_has_featured_subjects(self, mock_empty_search):
        data = client.get("/").json()
        nav_titles = {n["title"] for n in data.get("navigation", [])}
        for subject in FEATURED_SUBJECTS:
            assert subject["presentable_name"] in nav_titles

    def test_groups_present(self, mock_empty_search):
        data = client.get("/").json()
        assert len(data.get("groups", [])) == 6

    def test_links_include_self_and_search(self, mock_empty_search):
        data = client.get("/").json()
        rels = {lnk["rel"] for lnk in data.get("links", [])}
        assert "self" in rels
        assert "search" in rels

    def test_self_link_uses_base_url(self, mock_empty_search):
        with patch("app.routes.opds.OPDS_BASE_URL", "https://example.com/opds"):
            data = client.get("/").json()
        self_link = next(l for l in data["links"] if l["rel"] == "self")
        assert self_link["href"] == "https://example.com/opds/"

    def test_publication_self_links_use_opds_base(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider as OLP
        record = _make_record(edition_key="OL99M")
        with patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            with patch("app.routes.opds.OPDS_BASE_URL", "https://myopds.example.com"):
                with patch.object(OLP, "OPDS_BASE_URL", "https://myopds.example.com", create=True):
                    data = client.get("/").json()
        for group in data.get("groups", []):
            for pub in group.get("publications", []):
                self_link = next(
                    (l for l in pub["links"] if l["rel"] == "self"), None
                )
                if self_link:
                    assert self_link["href"].startswith("https://myopds.example.com/books/")
                    assert "openlibrary.org" not in self_link["href"]

    def test_upstream_error_omits_shelf(self):
        """If one shelf fails upstream, the rest still load."""
        call_count = 0

        def flaky_search(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise requests_lib.exceptions.HTTPError(
                    "500 Server Error",
                    response=MagicMock(status_code=500),
                )
            return _make_search_response()

        with patch(SEARCH_PATCH_TARGET, side_effect=flaky_search):
            resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("groups", [])) == 5


# ---------------------------------------------------------------------------
# GET /search
# ---------------------------------------------------------------------------

class TestOpdsSearch:
    def test_returns_200(self, mock_single_record):
        resp = client.get("/search?query=Python")
        assert resp.status_code == 200

    def test_content_type(self, mock_empty_search):
        resp = client.get("/search")
        assert "application/opds+json" in resp.headers["content-type"]

    def test_metadata_title(self, mock_empty_search):
        data = client.get("/search").json()
        assert data["metadata"]["title"] == "Search Results"

    def test_publications_in_response(self):
        record = _make_record(title="Python Cookbook")
        with patch(
            SEARCH_PATCH_TARGET,
            return_value=_make_search_response(records=[record], total=1),
        ):
            data = client.get("/search?query=Python").json()
        assert len(data.get("publications", [])) == 1
        assert data["publications"][0]["metadata"]["title"] == "Python Cookbook"

    def test_pagination_params_forwarded(self, mock_empty_search):
        client.get("/search?query=test&page=2&limit=10")
        mock_empty_search.assert_called_once_with(
            query="test", limit=10, offset=10, sort=None, facets={"mode": "everything"}
        )

    def test_invalid_limit_rejected(self):
        resp = client.get("/search?limit=0")
        assert resp.status_code == 422

    def test_invalid_page_rejected(self):
        resp = client.get("/search?page=0")
        assert resp.status_code == 422

    def test_self_link_uses_base_url_with_query(self):
        with patch(SEARCH_PATCH_TARGET, return_value=_make_search_response()):
            with patch("app.routes.opds.OPDS_BASE_URL", "https://myopds.example.com"):
                data = client.get("/search?query=hello&sort=trending").json()
        self_link = next(l for l in data["links"] if l["rel"] == "self")
        assert self_link["href"].startswith("https://myopds.example.com/search?")
        assert "query=hello" in self_link["href"]

    def test_publication_self_links_use_opds_base(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider as OLP
        record = _make_record(edition_key="OL42M")
        with patch(
            SEARCH_PATCH_TARGET,
            return_value=_make_search_response(records=[record], total=1),
        ):
            with patch("app.routes.opds.OPDS_BASE_URL", "https://myopds.example.com"):
                with patch.object(OLP, "OPDS_BASE_URL", "https://myopds.example.com", create=True):
                    data = client.get("/search?query=test").json()
        pub_self = next(
            l for l in data["publications"][0]["links"] if l["rel"] == "self"
        )
        assert pub_self["href"] == "https://myopds.example.com/books/OL42M"


# ---------------------------------------------------------------------------
# GET /books/{edition_olid}
# ---------------------------------------------------------------------------

class TestOpdsBooks:
    def test_returns_200_for_known_edition(self):
        record = _make_record(title="Moby-Dick", edition_key="OL7353617M")
        with patch(
            SEARCH_PATCH_TARGET,
            return_value=_make_search_response(records=[record], total=1),
        ):
            resp = client.get("/books/OL7353617M")
        assert resp.status_code == 200

    def test_content_type(self, mock_single_record):
        resp = client.get("/books/OL1M")
        assert "application/opds-publication+json" in resp.headers["content-type"]

    def test_returns_404_for_unknown_edition(self):
        with patch(
            SEARCH_PATCH_TARGET,
            return_value=_make_search_response(records=[], total=0),
        ):
            resp = client.get("/books/OL9999999M")
        assert resp.status_code == 404

    def test_404_body_has_detail(self):
        with patch(
            SEARCH_PATCH_TARGET,
            return_value=_make_search_response(records=[], total=0),
        ):
            data = client.get("/books/OL9999999M").json()
        assert "detail" in data

    def test_self_link_uses_opds_base(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider as OLP
        record = _make_record(edition_key="OL55M")
        with patch(
            SEARCH_PATCH_TARGET,
            return_value=_make_search_response(records=[record], total=1),
        ):
            with patch("app.routes.opds.OPDS_BASE_URL", "https://myopds.example.com"):
                with patch.object(OLP, "OPDS_BASE_URL", "https://myopds.example.com", create=True):
                    data = client.get("/books/OL55M").json()
        self_link = next(l for l in data["links"] if l["rel"] == "self")
        assert self_link["href"] == "https://myopds.example.com/books/OL55M"
        assert "openlibrary.org" not in self_link["href"]


# ---------------------------------------------------------------------------
# Upstream error handling
# ---------------------------------------------------------------------------

class TestUpstreamErrors:
    def test_requests_http_error_returns_502(self):
        mock_response = MagicMock(status_code=500)
        mock_request = MagicMock()
        mock_request.url = "https://openlibrary.org/search.json"
        exc = requests_lib.exceptions.HTTPError(
            "500 Server Error", response=mock_response, request=mock_request
        )
        with patch(SEARCH_PATCH_TARGET, side_effect=exc):
            resp = client.get("/search?query=test")
        assert resp.status_code == 502

    def test_requests_connection_error_returns_502(self):
        exc = requests_lib.exceptions.ConnectionError("Connection refused")
        with patch(SEARCH_PATCH_TARGET, side_effect=exc):
            resp = client.get("/search?query=test")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Search modes
# ---------------------------------------------------------------------------

class TestSearchModes:
    def test_ebooks_mode_forwarded(self, mock_empty_search):
        client.get("/search?query=test&mode=ebooks")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "ebooks"}
        )

    def test_open_access_mode_forwarded(self, mock_empty_search):
        client.get("/search?query=test&mode=open_access")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "open_access"}
        )

    def test_buyable_mode_forwarded(self, mock_empty_search):
        client.get("/search?query=test&mode=buyable")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "buyable"}
        )


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

class TestFacets:
    def test_search_response_includes_facets(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        assert "facets" in data
        assert len(data["facets"]) == 2

    def test_sort_facet_has_metadata_title(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        sort_facet = data["facets"][0]
        assert sort_facet["metadata"]["title"] == "Sort"

    def test_availability_facet_has_metadata_title(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        avail_facet = data["facets"][1]
        assert avail_facet["metadata"]["title"] == "Availability"

    def test_active_sort_facet_has_self_rel(self, mock_empty_search):
        data = client.get("/search?query=test&sort=new").json()
        sort_links = data["facets"][0]["links"]
        new_link = next(l for l in sort_links if l["title"] == "Most Recent")
        rel = new_link["rel"]
        # Active sort link should include "self" and the semantic rel
        assert "self" in rel
        assert "http://opds-spec.org/sort/new" in rel

    def test_active_availability_facet_has_self_rel(self, mock_empty_search):
        data = client.get("/search?query=test&mode=ebooks").json()
        avail_links = data["facets"][1]["links"]
        ebooks_link = next(l for l in avail_links if l["title"] == "Available to Borrow")
        assert ebooks_link["rel"] == "self"

    def test_sort_facet_links_have_numberOfItems(self, mock_single_record):
        data = client.get("/search?query=test").json()
        sort_links = data["facets"][0]["links"]
        for link in sort_links:
            assert "properties" in link
            assert "numberOfItems" in link["properties"]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
