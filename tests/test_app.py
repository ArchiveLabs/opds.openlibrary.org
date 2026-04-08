"""
Tests for the opds.openlibrary.org FastAPI service.

These tests use pytest and httpx's AsyncClient / TestClient.
Network calls to openlibrary.org are mocked so tests run offline.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import FEATURED_SUBJECTS
from app.routes import opds as opds_module

client = TestClient(app)

SEARCH_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.search"
FACET_COUNTS_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.fetch_facet_counts"
BUILD_FACETS_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.build_facets"
BUILD_HOME_FACETS_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.build_home_facets"


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


_FAKE_AVAILABILITY_COUNTS = {"everything": 100, "ebooks": 50, "open_access": 10, "buyable": 5}


@pytest.fixture(autouse=True)
def clear_home_cache():
    """Clear the homepage cache between tests."""
    opds_module._home_cache.clear()
    yield
    opds_module._home_cache.clear()


@pytest.fixture(autouse=True)
def mock_facet_counts():
    """Always mock fetch_facet_counts and facet builders to prevent real HTTP calls."""
    with patch(FACET_COUNTS_PATCH_TARGET, create=True, return_value=_FAKE_AVAILABILITY_COUNTS.copy()), \
         patch(BUILD_FACETS_PATCH_TARGET, create=True, return_value=[]), \
         patch(BUILD_HOME_FACETS_PATCH_TARGET, create=True, return_value=[]):
        yield


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

    def test_navigation_has_featured_subjects(self, mock_single_record):
        """Navigation only appears when groups have publications."""
        data = client.get("/").json()
        nav_titles = {n["title"] for n in data.get("navigation", [])}
        for subject in FEATURED_SUBJECTS:
            assert subject["presentable_name"] in nav_titles

    def test_groups_present(self, mock_single_record):
        """Groups with publications are included; empty groups are filtered."""
        data = client.get("/").json()
        assert len(data.get("groups", [])) == 6

    def test_empty_groups_filtered(self, mock_empty_search):
        """When search returns no records, groups are filtered out."""
        data = client.get("/").json()
        assert len(data.get("groups", [])) == 0

    def test_navigation_hidden_when_no_groups(self, mock_empty_search):
        """Navigation is hidden when there are no loaded groups."""
        data = client.get("/").json()
        assert data.get("navigation", []) == []

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
                with patch.object(OLP, "BASE_URL", "https://myopds.example.com"):
                    data = client.get("/").json()
        for group in data.get("groups", []):
            for pub in group.get("publications", []):
                self_link = next(
                    (l for l in pub["links"] if l["rel"] == "self"), None
                )
                if self_link:
                    assert self_link["href"].startswith("https://myopds.example.com/")
                    assert "openlibrary.org" not in self_link["href"]

    def test_upstream_error_omits_shelf(self):
        """If one shelf fails upstream, the rest still load."""
        call_count = 0
        record = _make_record()
        _req = httpx.Request("GET", "https://openlibrary.org/search.json")

        def flaky_search(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.HTTPStatusError(
                    "500 Server Error",
                    request=_req,
                    response=httpx.Response(500, request=_req),
                )
            return _make_search_response(records=[record], total=1)

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

    def test_total_none_does_not_crash(self):
        record = _make_record(title="Python Cookbook")
        with patch(
            SEARCH_PATCH_TARGET,
            return_value=_make_search_response(records=[record], total=None),
        ):
            resp = client.get("/search?query=Python&mode=buyable")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("publications", [])) == 1

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
            query="test", limit=10, offset=10, sort=None, facets={"mode": "everything"},
            language="en", title=None,
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
                with patch.object(OLP, "BASE_URL", "https://myopds.example.com"):
                    data = client.get("/search?query=test").json()
        pub_self = next(
            l for l in data["publications"][0]["links"] if l["rel"] == "self"
        )
        assert pub_self["href"].startswith("https://myopds.example.com/")
        assert "OL42M" in pub_self["href"]


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
                with patch.object(OLP, "BASE_URL", "https://myopds.example.com"):
                    data = client.get("/books/OL55M").json()
        self_link = next(l for l in data["links"] if l["rel"] == "self")
        assert self_link["href"].startswith("https://myopds.example.com/")
        assert "OL55M" in self_link["href"]
        assert "openlibrary.org" not in self_link["href"]


# ---------------------------------------------------------------------------
# Upstream error handling
# ---------------------------------------------------------------------------

class TestUpstreamErrors:
    def test_httpx_http_status_error_returns_502(self):
        _req = httpx.Request("GET", "https://openlibrary.org/search.json")
        exc = httpx.HTTPStatusError(
            "500 Server Error",
            request=_req,
            response=httpx.Response(500, request=_req),
        )
        with patch(SEARCH_PATCH_TARGET, side_effect=exc):
            resp = client.get("/search?query=test")
        assert resp.status_code == 502

    def test_httpx_request_error_returns_502(self):
        _req = httpx.Request("GET", "https://openlibrary.org/search.json")
        exc = httpx.ConnectError("Connection refused", request=_req)
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
            query="test", limit=25, offset=0, sort=None, facets={"mode": "ebooks"},
            language="en", title=None,
        )

    def test_open_access_mode_forwarded(self, mock_empty_search):
        client.get("/search?query=test&mode=open_access")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "open_access"},
            language="en", title=None,
        )

    def test_buyable_mode_forwarded(self, mock_empty_search):
        client.get("/search?query=test&mode=buyable")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "buyable"},
            language="en", title=None,
        )



# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

def _fake_facets(base_url="", query="test", sort=None, mode="everything", language=None, total=0, availability_counts=None):
    """Return realistic facet data matching what build_facets now produces (Availability only).

    Only the active mode link carries rel="self"; non-active links have no rel key,
    matching the real _build_availability_links output.
    """
    counts = availability_counts or _FAKE_AVAILABILITY_COUNTS

    def _avail_link(mode_val, title, href):
        link = {"title": title, "href": href, "type": "application/opds+json",
                "properties": {"numberOfItems": counts.get(mode_val, 0)}}
        if mode_val == mode:
            link["rel"] = "self"
        return link

    avail_links = [
        _avail_link("everything",  "Everything",            f"{base_url}/search?query={query}"),
        _avail_link("ebooks",      "Available to Borrow",   f"{base_url}/search?query={query}&mode=ebooks"),
        _avail_link("open_access", "Open Access",           f"{base_url}/search?query={query}&mode=open_access"),
        _avail_link("buyable",     "Available to Purchase", f"{base_url}/search?query={query}&mode=buyable"),
    ]
    return [
        {"metadata": {"title": "Availability"}, "links": avail_links},
    ]


class TestFacets:
    @pytest.fixture(autouse=True)
    def mock_build_facets_with_data(self):
        """Override the autouse empty build_facets mock with real facet data."""
        def _build(**kwargs):
            return _fake_facets(**kwargs)
        with patch(BUILD_FACETS_PATCH_TARGET, create=True, side_effect=_build):
            yield

    def test_search_response_includes_facets(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        assert "facets" in data
        assert len(data["facets"]) == 1

    def test_availability_facet_has_metadata_title(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        avail_facet = data["facets"][0]
        assert avail_facet["metadata"]["title"] == "Availability"

    def test_no_sort_facet_in_response(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        titles = [f["metadata"]["title"] for f in data["facets"]]
        assert "Sort" not in titles

    def test_active_availability_facet_has_self_rel(self, mock_empty_search):
        data = client.get("/search?query=test&mode=ebooks").json()
        avail_links = data["facets"][0]["links"]
        ebooks_link = next(l for l in avail_links if l["title"] == "Available to Borrow")
        assert ebooks_link["rel"] == "self"

    def test_availability_labels_match_canonical(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        titles = {l["title"] for l in data["facets"][0]["links"]}
        assert "Everything" in titles
        assert "Available to Borrow" in titles
        assert "Open Access" in titles
        assert "Available to Purchase" in titles


# ---------------------------------------------------------------------------
# Cache dev-mode bypass
# ---------------------------------------------------------------------------

class TestHomeCacheDevMode:
    def test_cache_is_populated_in_production(self, mock_empty_search):
        """Homepage response is cached in production mode."""
        with patch("app.routes.opds.ENVIRONMENT", "production"):
            client.get("/")
        assert len(opds_module._home_cache) == 1

    def test_cache_is_not_populated_in_development(self, mock_empty_search):
        """Homepage response is NOT cached in development mode."""
        with patch("app.routes.opds.ENVIRONMENT", "development"):
            client.get("/")
        assert len(opds_module._home_cache) == 0

    def test_cache_is_not_served_in_development(self, mock_empty_search):
        """Cached entry is ignored when ENVIRONMENT=development."""
        # Pre-populate the cache manually
        opds_module._home_cache["http://testserver"] = (
            float("inf"),
            {"metadata": {"title": "Cached"}, "links": [], "groups": [], "navigation": []},
        )
        with patch("app.routes.opds.ENVIRONMENT", "development"), \
             patch("app.routes.opds.OPDS_BASE_URL", None):
            resp = client.get("/")
        # Should have hit the real handler, not returned the stale cache entry
        assert resp.status_code == 200
        assert mock_empty_search.called

    def test_cache_is_served_in_production(self, mock_empty_search):
        """Cached entry IS served when ENVIRONMENT=production."""
        opds_module._home_cache["http://testserver"] = (
            float("inf"),
            {"metadata": {"title": "Cached"}, "links": [], "groups": [], "navigation": []},
        )
        with patch("app.routes.opds.ENVIRONMENT", "production"), \
             patch("app.routes.opds.OPDS_BASE_URL", None):
            resp = client.get("/")
        assert resp.status_code == 200
        assert not mock_empty_search.called


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
