"""
Tests for the opds.openlibrary.org FastAPI service.

These tests use pytest and httpx's AsyncClient / TestClient.
Network calls to openlibrary.org are mocked so tests run offline.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from pyopds2_openlibrary import OpenLibraryDataProvider
from app.routes import opds as opds_module

client = TestClient(app)

SEARCH_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.search"
FACET_COUNTS_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.fetch_facet_counts"
BUILD_FACETS_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.build_facets"
BUILD_HOME_FACETS_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.build_home_facets"
FETCH_AUTHOR_BIO_PATCH_TARGET = "app.routes.opds.fetch_author_bio"


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
        for subject in OpenLibraryDataProvider.FEATURED_SUBJECTS:
            assert subject["presentable_name"] in nav_titles

    def test_groups_present(self, mock_single_record):
        """Page 1 returns first batch of groups (GROUPS_PER_PAGE=3)."""
        data = client.get("/").json()
        assert len(data.get("groups", [])) == 3

    def test_page2_groups(self, mock_single_record):
        """Page 2 returns the next batch of groups."""
        data = client.get("/?page=2").json()
        assert len(data.get("groups", [])) == 3

    def test_page3_groups(self, mock_single_record):
        """Page 3 returns the remaining group."""
        data = client.get("/?page=3").json()
        assert len(data.get("groups", [])) == 1

    def test_next_link_on_page1(self, mock_single_record):
        """Page 1 includes a 'next' link."""
        data = client.get("/").json()
        rels = {lnk["rel"] for lnk in data.get("links", [])}
        assert "next" in rels
        assert "previous" not in rels

    def test_previous_link_on_page2(self, mock_single_record):
        """Page 2 includes both 'next' and 'previous' links."""
        data = client.get("/?page=2").json()
        rels = {lnk["rel"] for lnk in data.get("links", [])}
        assert "next" in rels
        assert "previous" in rels

    def test_last_page_no_next(self, mock_single_record):
        """Last page has 'previous' but no 'next' link."""
        data = client.get("/?page=3").json()
        rels = {lnk["rel"] for lnk in data.get("links", [])}
        assert "next" not in rels
        assert "previous" in rels

    def test_navigation_only_on_page1(self, mock_single_record):
        """Navigation items only appear on page 1."""
        data1 = client.get("/").json()
        data2 = client.get("/?page=2").json()
        assert len(data1.get("navigation", [])) > 0
        assert len(data2.get("navigation", [])) == 0

    def test_facets_present_on_all_pages(self, mock_single_record):
        """build_home_facets is called on every page, not just page 1 (issue #71)."""
        stub = [{"metadata": {"title": "Availability"}, "links": []}]
        with patch(BUILD_HOME_FACETS_PATCH_TARGET, return_value=stub) as mock_hf:
            for page in (1, 2, 3):
                data = client.get(f"/?page={page}").json()
                assert data["facets"] == stub, f"facets missing on page {page}"
        assert mock_hf.call_count == 3, "build_home_facets must be called for every page"

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

    def test_home_defaults_to_all_languages(self, mock_empty_search):
        """Homepage defaults to all languages (no filter)."""
        client.get("/")
        for call in mock_empty_search.call_args_list:
            assert call.kwargs.get("language") is None

    def test_home_english_filter(self, mock_empty_search):
        """Passing language=en on homepage filters to English."""
        client.get("/?language=en")
        for call in mock_empty_search.call_args_list:
            assert call.kwargs.get("language") == "en"

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
        assert len(data.get("groups", [])) == 2


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
            language=None, title=None, require_cover=False, media_type=None, access=None,
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
            language=None, title=None, require_cover=False, media_type=None, access=None,
        )

    def test_open_access_mode_forwarded(self, mock_empty_search):
        client.get("/search?query=test&mode=open_access")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "open_access"},
            language=None, title=None, require_cover=False, media_type=None, access=None,
        )

    def test_access_print_disabled_forwarded(self, mock_empty_search):
        client.get("/search?query=test&access=print_disabled")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "everything"},
            language=None, title=None, require_cover=False, media_type=None, access="print_disabled",
        )

    def test_buyable_mode_forwarded(self, mock_empty_search):
        client.get("/search?query=test&mode=buyable")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "buyable"},
            language=None, title=None, require_cover=False, media_type=None, access=None,
        )



# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

def _fake_facets(base_url="", query="test", sort=None, mode="everything", language=None, title=None, total=0, availability_counts=None, media_type=None, access=None):
    """Return realistic facet data matching what build_facets now produces.

    Only the active mode/access link carries rel="self"; non-active links have no rel key,
    matching the real _build_availability_links / _build_access_links output.
    """
    counts = availability_counts or _FAKE_AVAILABILITY_COUNTS

    def _avail_link(mode_val, lbl, href):
        link = {"title": lbl, "href": href, "type": "application/opds+json",
                "properties": {"numberOfItems": counts.get(mode_val, 0)}}
        if mode_val == mode:
            link["rel"] = "self"
        return link

    active_access = access or "general"

    def _access_link(ac_val, lbl, href):
        link = {"title": lbl, "href": href, "type": "application/opds+json"}
        if ac_val == active_access:
            link["rel"] = "self"
        return link

    avail_links = [
        _avail_link("everything",  "Everything",            f"{base_url}/search?query={query}"),
        _avail_link("ebooks",      "Available to Borrow",   f"{base_url}/search?query={query}&mode=ebooks"),
        _avail_link("open_access", "Open Access",           f"{base_url}/search?query={query}&mode=open_access"),
        _avail_link("buyable",     "Available for Purchase", f"{base_url}/search?query={query}&mode=buyable"),
    ]
    access_links = [
        _access_link("general",        "General",       f"{base_url}/search?query={query}"),
        _access_link("print_disabled", "Print Disabled", f"{base_url}/search?query={query}&access=print_disabled"),
    ]
    return [
        {"metadata": {"title": "Availability"}, "links": avail_links},
        {"metadata": {"title": "Access"}, "links": access_links},
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
        assert len(data["facets"]) == 2

    def test_availability_facet_has_metadata_title(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        facet_titles = {f["metadata"]["title"] for f in data["facets"]}
        assert "Availability" in facet_titles

    def test_no_sort_facet_in_response(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        titles = [f["metadata"]["title"] for f in data["facets"]]
        assert "Sort" not in titles

    def test_active_availability_facet_has_self_rel(self, mock_empty_search):
        data = client.get("/search?query=test&mode=ebooks").json()
        avail = next(f for f in data["facets"] if f["metadata"]["title"] == "Availability")
        ebooks_link = next(l for l in avail["links"] if l["title"] == "Available to Borrow")
        assert ebooks_link["rel"] == "self"

    def test_availability_labels_match_canonical(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        avail = next(f for f in data["facets"] if f["metadata"]["title"] == "Availability")
        titles = {l["title"] for l in avail["links"]}
        assert "Everything" in titles
        assert "Available to Borrow" in titles
        assert "Open Access" in titles
        assert "Available for Purchase" in titles
        assert "Print Disabled" not in titles

    def test_access_facet_present(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        access_facet = next((f for f in data["facets"] if f["metadata"]["title"] == "Access"), None)
        assert access_facet is not None
        titles = {l["title"] for l in access_facet["links"]}
        assert "General" in titles
        assert "Print Disabled" in titles

    def test_active_access_facet_has_self_rel(self, mock_empty_search):
        data = client.get("/search?query=test&access=print_disabled").json()
        access_facet = next(f for f in data["facets"] if f["metadata"]["title"] == "Access")
        pd_link = next(l for l in access_facet["links"] if l["title"] == "Print Disabled")
        assert pd_link["rel"] == "self"

    def test_general_access_is_default_active(self, mock_empty_search):
        data = client.get("/search?query=test").json()
        access_facet = next(f for f in data["facets"] if f["metadata"]["title"] == "Access")
        general_link = next(l for l in access_facet["links"] if l["title"] == "General")
        assert general_link.get("rel") == "self"


# ---------------------------------------------------------------------------
# Media type facet
# ---------------------------------------------------------------------------

class TestMediaTypeFacet:
    def test_media_type_forwarded_to_search(self, mock_empty_search):
        client.get("/search?query=test&media_type=audiobook")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "everything"},
            language=None, title=None, require_cover=False, media_type="audiobook", access=None,
        )

    def test_ebook_media_type_forwarded(self, mock_empty_search):
        client.get("/search?query=test&media_type=ebook")
        mock_empty_search.assert_called_once_with(
            query="test", limit=25, offset=0, sort=None, facets={"mode": "everything"},
            language=None, title=None, require_cover=False, media_type="ebook", access=None,
        )

    def test_media_type_in_build_facets_call(self, mock_empty_search):
        with patch(BUILD_FACETS_PATCH_TARGET, return_value=[]) as mock_bf:
            client.get("/search?query=test&media_type=audiobook")
        _, kwargs = mock_bf.call_args
        assert kwargs.get("media_type") == "audiobook"

    def test_home_media_type_forwarded_to_search(self, mock_empty_search):
        client.get("/?media_type=ebook")
        for call in mock_empty_search.call_args_list:
            assert call.kwargs.get("media_type") == "ebook"


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
# strip_markdown
# ---------------------------------------------------------------------------

class TestStripMarkdown:
    """Tests for pyopds2_openlibrary.strip_markdown (markdown-it-py based)."""

    def test_plain_text_unchanged(self):
        from pyopds2_openlibrary import strip_markdown
        assert strip_markdown("Hello world") == "Hello world"

    def test_strips_bold(self):
        from pyopds2_openlibrary import strip_markdown
        assert strip_markdown("**bold text**") == "bold text"

    def test_strips_italic(self):
        from pyopds2_openlibrary import strip_markdown
        assert strip_markdown("*italic text*") == "italic text"

    def test_strips_heading(self):
        from pyopds2_openlibrary import strip_markdown
        result = strip_markdown("## Chapter One\nBody text")
        assert "##" not in result
        assert "Chapter One" in result
        assert "Body text" in result

    def test_strips_link(self):
        from pyopds2_openlibrary import strip_markdown
        assert strip_markdown("[click here](http://example.com)") == "click here"

    def test_strips_link_with_nested_brackets(self):
        from pyopds2_openlibrary import strip_markdown
        result = strip_markdown("[Prey [1/2]](http://example.com)")
        assert "Prey" in result
        assert "http://example.com" not in result

    def test_strips_html_tags(self):
        from pyopds2_openlibrary import strip_markdown
        assert strip_markdown("<p>hello</p>") == "hello"

    def test_strips_inline_html(self):
        from pyopds2_openlibrary import strip_markdown
        result = strip_markdown("Some <b>bold</b> and <i>italic</i> text")
        assert "<b>" not in result
        assert "<i>" not in result
        assert "bold" in result

    def test_normalises_crlf(self):
        from pyopds2_openlibrary import strip_markdown
        result = strip_markdown("line1\r\nline2")
        assert "\r" not in result
        assert "line1" in result
        assert "line2" in result

    def test_collapses_excessive_blank_lines(self):
        from pyopds2_openlibrary import strip_markdown
        result = strip_markdown("a\n\n\n\n\nb")
        assert "\n\n\n" not in result
        assert "a" in result
        assert "b" in result

    def test_strips_horizontal_rule(self):
        from pyopds2_openlibrary import strip_markdown
        result = strip_markdown("above\n\n---\n\nbelow")
        assert "---" not in result
        assert "above" in result
        assert "below" in result

    def test_empty_string(self):
        from pyopds2_openlibrary import strip_markdown
        assert strip_markdown("") == ""

    def test_mixed_markdown(self):
        from pyopds2_openlibrary import strip_markdown
        text = "# Title\n\n**Bold** and [a link](http://x.com).\n\n---\n\n*End*"
        result = strip_markdown(text)
        assert "#" not in result
        assert "**" not in result
        assert "*" not in result
        assert "---" not in result
        assert "http://x.com" not in result
        assert "Title" in result
        assert "Bold" in result
        assert "a link" in result
        assert "End" in result


# ---------------------------------------------------------------------------
# GET /authors/{olid}
# ---------------------------------------------------------------------------

class TestOpdsAuthors:
    def test_happy_path_returns_200(self):
        record = _make_record(title="The Good Lord Bird")
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("James McBride", "An American author.")), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            resp = client.get("/authors/OL1234A")
        assert resp.status_code == 200

    def test_content_type(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author Name", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            resp = client.get("/authors/OL1234A")
        assert "application/opds+json" in resp.headers["content-type"]

    def test_metadata_title_is_author_name(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("James McBride", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        assert data["metadata"]["title"] == "James McBride"

    def test_metadata_description_is_bio(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("James McBride", "An American author.")), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        assert data["metadata"]["description"] == "An American author."

    def test_publications_present(self):
        record = _make_record(title="The Color of Water")
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("James McBride", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        assert len(data.get("publications", [])) == 1
        assert data["publications"][0]["metadata"]["title"] == "The Color of Water"

    def test_bio_fetch_failure_still_returns_200(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=(None, None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            resp = client.get("/authors/OL1234A")
        assert resp.status_code == 200

    def test_bio_failure_uses_olid_as_title(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=(None, None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        assert data["metadata"]["title"] == "OL1234A"

    def test_no_books_and_no_bio_returns_404(self):
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=(None, None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[], total=0)):
            resp = client.get("/authors/OL9999A")
        assert resp.status_code == 404

    def test_invalid_olid_returns_422(self):
        resp = client.get("/authors/notanolid")
        assert resp.status_code == 422

    def test_invalid_olid_wrong_suffix_returns_422(self):
        resp = client.get("/authors/OL1234M")
        assert resp.status_code == 422

    def test_self_link_present(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        rels = {l["rel"] for l in data.get("links", [])}
        assert "self" in rels

    def test_first_link_present(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        rels = {l["rel"] for l in data.get("links", [])}
        assert "first" in rels

    def test_next_link_when_more_results(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=50)):
            data = client.get("/authors/OL1234A?limit=25").json()
        rels = {l["rel"] for l in data.get("links", [])}
        assert "next" in rels

    def test_no_next_link_on_last_page(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        rels = {l["rel"] for l in data.get("links", [])}
        assert "next" not in rels

    def test_previous_link_on_page2(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=50)):
            data = client.get("/authors/OL1234A?page=2&limit=25").json()
        rels = {l["rel"] for l in data.get("links", [])}
        assert "previous" in rels

    def test_no_previous_link_on_page1(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        rels = {l["rel"] for l in data.get("links", [])}
        assert "previous" not in rels

    def test_author_facets_present(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        facet_titles = {f["metadata"]["title"] for f in data.get("facets", [])}
        assert "Availability" in facet_titles
        assert "Language" in facet_titles
        assert "Media Type" in facet_titles
        assert "Access" in facet_titles

    def test_author_availability_facet_no_buyable(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A").json()
        avail = next(f for f in data["facets"] if f["metadata"]["title"] == "Availability")
        titles = {l["title"] for l in avail["links"]}
        assert "Available for Purchase" not in titles

    def test_author_active_mode_marked_with_self_rel(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A?mode=ebooks").json()
        avail = next(f for f in data["facets"] if f["metadata"]["title"] == "Availability")
        ebooks_link = next(l for l in avail["links"] if l["title"] == "Available to Borrow")
        assert ebooks_link.get("rel") == "self"

    def test_author_media_type_filter_forwarded(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)) as mock_s:
            client.get("/authors/OL1234A?media_type=audiobook")
        _, kwargs = mock_s.call_args
        assert kwargs.get("media_type") == "audiobook"

    def test_author_facet_links_preserve_filters(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A?mode=ebooks&media_type=ebook").json()
        # Language facet links should carry current mode and media_type
        lang = next(f for f in data["facets"] if f["metadata"]["title"] == "Language")
        en_link = next(l for l in lang["links"] if l["title"] == "English")
        assert "mode=ebooks" in en_link["href"]
        assert "media_type=ebook" in en_link["href"]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# AuthorNotFound exception
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# fetch_author_bio helper
# ---------------------------------------------------------------------------

class TestFetchAuthorBio:
    def test_happy_path_string_bio(self):
        from pyopds2_openlibrary import fetch_author_bio
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "name": "James McBride",
            "bio": "An American author and musician.",
        }
        with patch("pyopds2_openlibrary._get", return_value=mock_resp):
            name, bio = fetch_author_bio("OL1234A")
        assert name == "James McBride"
        assert bio == "An American author and musician."

    def test_dict_bio_normalized(self):
        from pyopds2_openlibrary import fetch_author_bio
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "name": "Anton Chekhov",
            "bio": {"type": "/type/text", "value": "Russian playwright."},
        }
        with patch("pyopds2_openlibrary._get", return_value=mock_resp):
            name, bio = fetch_author_bio("OL19677A")
        assert name == "Anton Chekhov"
        assert bio == "Russian playwright."

    def test_no_bio_field_returns_none_bio(self):
        from pyopds2_openlibrary import fetch_author_bio
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"name": "No Bio Author"}
        with patch("pyopds2_openlibrary._get", return_value=mock_resp):
            name, bio = fetch_author_bio("OL9999A")
        assert name == "No Bio Author"
        assert bio is None

    def test_returns_none_none_on_network_error(self):
        from pyopds2_openlibrary import fetch_author_bio
        with patch("pyopds2_openlibrary._get", side_effect=Exception("timeout")):
            name, bio = fetch_author_bio("OL1234A")
        assert name is None
        assert bio is None

    def test_personal_name_fallback(self):
        from pyopds2_openlibrary import fetch_author_bio
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"personal_name": "Fallback Name"}
        with patch("pyopds2_openlibrary._get", return_value=mock_resp):
            name, bio = fetch_author_bio("OL5678A")
        assert name == "Fallback Name"


# ---------------------------------------------------------------------------
# AuthorNotFound exception
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Author OPDS link in publication contributors
# ---------------------------------------------------------------------------

class TestAuthorOpdsLink:
    def test_author_has_opds_link(self):
        from pyopds2_openlibrary import OpenLibraryDataRecord, OpenLibraryDataProvider
        OpenLibraryDataProvider.OPDS_BASE_URL = "https://opds.example.com"
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL1W",
            "title": "Test Book",
            "author_name": ["James McBride"],
            "author_key": ["OL1234A"],
            "editions": {
                "numFound": 1, "start": 0, "numFoundExact": True,
                "docs": [{"key": "/books/OL1M", "title": "Test Book"}],
            },
        })
        meta = record.metadata()
        assert meta.author is not None
        author = meta.author[0]
        link_types = [l.type for l in (author.links or [])]
        assert "application/opds+json" in link_types

    def test_opds_link_href_contains_olid(self):
        from pyopds2_openlibrary import OpenLibraryDataRecord, OpenLibraryDataProvider
        OpenLibraryDataProvider.OPDS_BASE_URL = "https://opds.example.com"
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL1W",
            "title": "Test Book",
            "author_name": ["James McBride"],
            "author_key": ["OL1234A"],
            "editions": {
                "numFound": 1, "start": 0, "numFoundExact": True,
                "docs": [{"key": "/books/OL1M", "title": "Test Book"}],
            },
        })
        meta = record.metadata()
        author = meta.author[0]
        opds_link = next(
            (l for l in (author.links or []) if l.type == "application/opds+json"),
            None,
        )
        assert opds_link is not None
        assert "OL1234A" in opds_link.href
        assert opds_link.href.startswith("https://opds.example.com/authors/")

    def test_name_only_author_has_no_opds_link(self):
        from pyopds2_openlibrary import OpenLibraryDataRecord
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL2W",
            "title": "Name Only Book",
            "author_name": ["Anonymous"],
            "editions": {
                "numFound": 1, "start": 0, "numFoundExact": True,
                "docs": [{"key": "/books/OL2M", "title": "Name Only Book"}],
            },
        })
        meta = record.metadata()
        author = meta.author[0]
        opds_link = next(
            (l for l in (author.links or []) if l.type == "application/opds+json"),
            None,
        )
        assert opds_link is None


# ---------------------------------------------------------------------------
# AuthorNotFound exception
# ---------------------------------------------------------------------------

class TestAuthorNotFound:
    def test_exception_message(self):
        from app.exceptions import AuthorNotFound
        exc = AuthorNotFound("OL123A")
        assert str(exc) == "Author not found: OL123A"
        assert exc.author_olid == "OL123A"


# ---------------------------------------------------------------------------
# Issue #75: Smart description fallback
# ---------------------------------------------------------------------------

class TestSmartDescriptionFallback:
    def _make_record_with_desc(self, edition_desc=None, work_desc=None, lang=None):
        from pyopds2_openlibrary import OpenLibraryDataRecord
        doc: dict = {
            "key": "/works/OL1W",
            "title": "Test Book",
            "editions": {
                "numFound": 1, "start": 0, "numFoundExact": True,
                "docs": [{"key": "/books/OL1M", "title": "Test Book"}],
            },
        }
        if work_desc:
            doc["description"] = work_desc
        if lang:
            doc["editions"]["docs"][0]["language"] = lang
        if edition_desc:
            doc["editions"]["docs"][0]["description"] = edition_desc
        return OpenLibraryDataRecord.model_validate(doc)

    def test_edition_description_used_when_present(self):
        record = self._make_record_with_desc(edition_desc="Edition desc.", work_desc="Work desc.", lang=["eng"])
        assert record.metadata().description == "Edition desc."

    def test_work_description_fallback_for_english(self):
        record = self._make_record_with_desc(work_desc="Work desc.", lang=["eng"])
        assert record.metadata().description == "Work desc."

    def test_no_fallback_for_non_english(self):
        record = self._make_record_with_desc(work_desc="Work desc.", lang=["fre"])
        assert record.metadata().description is None

    def test_no_fallback_when_no_work_desc(self):
        record = self._make_record_with_desc(lang=["eng"])
        assert record.metadata().description is None

    def test_no_fallback_when_no_language(self):
        record = self._make_record_with_desc(work_desc="Work desc.")
        assert record.metadata().description is None


# ---------------------------------------------------------------------------
# Issue #76: Group descriptions
# ---------------------------------------------------------------------------

class TestGroupDescriptions:
    def test_standard_ebooks_group_has_description(self, mock_single_record):
        # Standard Ebooks is the last group — scan all pages until we find it.
        num_pages = 3
        se_group = None
        for p in range(1, num_pages + 1):
            data = client.get(f"/?page={p}").json()
            se_group = next(
                (g for g in data.get("groups", []) if g.get("metadata", {}).get("title") == "Standard Ebooks"),
                None,
            )
            if se_group:
                break
        assert se_group is not None, "Standard Ebooks group not found on any homepage page"
        assert se_group["metadata"].get("description") is not None
        assert "Standard Ebooks" in se_group["metadata"]["description"]

    def test_group_descriptions_dict_has_standard_ebooks(self):
        from pyopds2_openlibrary import _GROUP_DESCRIPTIONS
        assert "Standard Ebooks" in _GROUP_DESCRIPTIONS
        assert len(_GROUP_DESCRIPTIONS["Standard Ebooks"]) > 10

    def test_group_descriptions_dict_has_classic_books(self):
        from pyopds2_openlibrary import _GROUP_DESCRIPTIONS
        assert "Classic Books" in _GROUP_DESCRIPTIONS
        assert len(_GROUP_DESCRIPTIONS["Classic Books"]) > 10

    def test_group_descriptions_dict_has_kids(self):
        from pyopds2_openlibrary import _GROUP_DESCRIPTIONS
        assert "Kids" in _GROUP_DESCRIPTIONS
        assert len(_GROUP_DESCRIPTIONS["Kids"]) > 10


# ---------------------------------------------------------------------------
# Issue #73: Access facet post-filter
# ---------------------------------------------------------------------------

class TestAccessFilter:
    def test_printdisabled_records_excluded_by_default(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider, OpenLibraryDataRecord

        pd_record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL2W", "title": "Print Disabled Book",
            "ebook_access": "printdisabled",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL2M", "title": "PD Book",
                                   "ebook_access": "printdisabled",
                                   "providers": [{"provider_name": "ia", "url": "https://archive.org/x"}],
                                   "ia": ["someident"], "cover_i": 1}]},
        })
        borrowable_record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL3W", "title": "Borrowable Book",
            "ebook_access": "borrowable",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL3M", "title": "Borrow Book",
                                   "ebook_access": "borrowable",
                                   "providers": [{"provider_name": "ia", "url": "https://archive.org/y"}],
                                   "ia": ["otherid"], "cover_i": 2}]},
        })

        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {
                "docs": [pd_record.model_dump(), borrowable_record.model_dump()],
                "numFound": 2,
            }
            resp = OpenLibraryDataProvider.search(
                query="test", require_cover=False, access=None,
            )
        titles = [r.title for r in resp.records]
        assert "Borrowable Book" in titles
        assert "Print Disabled Book" not in titles

    def test_printdisabled_records_shown_with_access_param(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider, OpenLibraryDataRecord

        pd_record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL2W", "title": "Print Disabled Book",
            "ebook_access": "printdisabled",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL2M", "title": "PD Book",
                                   "ebook_access": "printdisabled",
                                   "providers": [{"provider_name": "ia", "url": "https://archive.org/x"}],
                                   "ia": ["someident"], "cover_i": 1}]},
        })

        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {
                "docs": [pd_record.model_dump()],
                "numFound": 1,
            }
            resp = OpenLibraryDataProvider.search(
                query="test", require_cover=False, access="print_disabled",
            )
        titles = [r.title for r in resp.records]
        assert "Print Disabled Book" in titles

    def test_access_param_forwarded_to_search_from_route(self, mock_empty_search):
        client.get("/search?query=test&access=print_disabled")
        _, kwargs = mock_empty_search.call_args
        assert kwargs.get("access") == "print_disabled"

    def test_author_access_param_forwarded(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)) as mock_s:
            client.get("/authors/OL1234A?access=print_disabled")
        _, kwargs = mock_s.call_args
        assert kwargs.get("access") == "print_disabled"

    def test_access_facet_links_preserve_current_access(self):
        record = _make_record()
        with patch(FETCH_AUTHOR_BIO_PATCH_TARGET, return_value=("Author", None)), \
             patch(SEARCH_PATCH_TARGET, return_value=_make_search_response(records=[record], total=1)):
            data = client.get("/authors/OL1234A?access=print_disabled&mode=ebooks").json()
        access_facet = next(f for f in data["facets"] if f["metadata"]["title"] == "Access")
        pd_link = next(l for l in access_facet["links"] if l["title"] == "Print Disabled")
        assert "access=print_disabled" in pd_link["href"]

    def _make_pd_and_borrowable(self):
        from pyopds2_openlibrary import OpenLibraryDataRecord
        pd = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL2W", "title": "Print Disabled Book",
            "ebook_access": "printdisabled",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL2M", "title": "PD Book",
                                   "ebook_access": "printdisabled",
                                   "providers": [{"provider_name": "ia", "url": "https://archive.org/x"}],
                                   "ia": ["someident"], "cover_i": 1}]},
        })
        borrowable = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL3W", "title": "Borrowable Book",
            "ebook_access": "borrowable",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL3M", "title": "Borrow Book",
                                   "ebook_access": "borrowable",
                                   "providers": [{"provider_name": "ia", "url": "https://archive.org/y"}],
                                   "ia": ["otherid"], "cover_i": 2}]},
        })
        return pd, borrowable

    def test_printdisabled_excluded_with_ebooks_mode(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider
        pd, borrowable = self._make_pd_and_borrowable()
        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {
                "docs": [pd.model_dump(), borrowable.model_dump()],
                "numFound": 2,
            }
            resp = OpenLibraryDataProvider.search(
                query="test", require_cover=False, access=None,
                facets={"mode": "ebooks"},
            )
        titles = [r.title for r in resp.records]
        assert "Borrowable Book" in titles
        assert "Print Disabled Book" not in titles

    def test_printdisabled_excluded_with_language_filter(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider
        pd, borrowable = self._make_pd_and_borrowable()
        with patch("pyopds2_openlibrary._get") as mock_get, \
             patch("pyopds2_openlibrary.iso_639_1_to_marc", return_value="eng"):
            mock_get.return_value.json.return_value = {
                "docs": [pd.model_dump(), borrowable.model_dump()],
                "numFound": 2,
            }
            resp = OpenLibraryDataProvider.search(
                query="test", require_cover=False, access=None,
                language="en",
            )
        titles = [r.title for r in resp.records]
        assert "Borrowable Book" in titles
        assert "Print Disabled Book" not in titles

    def test_printdisabled_excluded_with_media_type_ebook(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider
        pd, borrowable = self._make_pd_and_borrowable()
        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {
                "docs": [pd.model_dump(), borrowable.model_dump()],
                "numFound": 2,
            }
            resp = OpenLibraryDataProvider.search(
                query="test", require_cover=False, access=None,
                media_type="ebook",
            )
        titles = [r.title for r in resp.records]
        assert "Borrowable Book" in titles
        assert "Print Disabled Book" not in titles

    def test_printdisabled_excluded_with_all_facets_combined(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider
        pd, borrowable = self._make_pd_and_borrowable()
        with patch("pyopds2_openlibrary._get") as mock_get, \
             patch("pyopds2_openlibrary.iso_639_1_to_marc", return_value="eng"):
            mock_get.return_value.json.return_value = {
                "docs": [pd.model_dump(), borrowable.model_dump()],
                "numFound": 2,
            }
            resp = OpenLibraryDataProvider.search(
                query="test", require_cover=False, access=None,
                facets={"mode": "ebooks"}, language="en", media_type="ebook",
            )
        titles = [r.title for r in resp.records]
        assert "Borrowable Book" in titles
        assert "Print Disabled Book" not in titles
