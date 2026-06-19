"""
Tests for the opds.openlibrary.org FastAPI service.

These tests use pytest and httpx's AsyncClient / TestClient.
Network calls to openlibrary.org are mocked so tests run offline.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.parse import unquote_plus

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.cache import NullCacheBackend, get_cache
from pyopds2_openlibrary import OpenLibraryDataProvider
from app.routes import opds as opds_module

# Captured before any autouse patches fire so tests can opt back into the real
# implementation without manual unpatching.
_REAL_FETCH_LANG_COUNTS = OpenLibraryDataProvider.fetch_language_counts
_REAL_FETCH_FACET_COUNTS = OpenLibraryDataProvider.fetch_facet_counts

app = fastapi_app

client = TestClient(app)

SEARCH_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.search"
FACET_COUNTS_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.fetch_facet_counts"
LANG_COUNTS_PATCH_TARGET = "app.routes.opds.OpenLibraryDataProvider.fetch_language_counts"
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
def null_cache():
    """Inject NullCacheBackend for all tests via FastAPI DI override."""
    fastapi_app.dependency_overrides[get_cache] = NullCacheBackend
    yield
    fastapi_app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def mock_facet_counts():
    """Always mock fetch_facet_counts and facet builders to prevent real HTTP calls."""
    with patch(FACET_COUNTS_PATCH_TARGET, create=True, return_value=_FAKE_AVAILABILITY_COUNTS.copy()), \
         patch(LANG_COUNTS_PATCH_TARGET, create=True, return_value={}), \
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
        """Page 3 returns the remaining group (7 total English groups)."""
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

    def test_home_limit_param_overrides_default(self, mock_empty_search):
        """?limit=N on the home route overrides the language-default per-group cap."""
        client.get("/?language=fr&limit=15")
        group_calls = [c for c in mock_empty_search.call_args_list
                       if c.kwargs.get("facets") is not None]
        assert group_calls
        for call in group_calls:
            assert call.kwargs.get("limit") == 15

    def test_home_limit_zero_keeps_language_default(self, mock_empty_search):
        """?limit=0 (or omitted) falls back to language default: 25 EN, 100 non-EN."""
        client.get("/?language=fr")
        fr_calls = [c for c in mock_empty_search.call_args_list
                    if c.kwargs.get("facets") is not None]
        assert fr_calls
        for call in fr_calls:
            assert call.kwargs.get("limit") == 50
        mock_empty_search.reset_mock()
        client.get("/")
        en_calls = [c for c in mock_empty_search.call_args_list
                    if c.kwargs.get("facets") is not None]
        assert en_calls
        for call in en_calls:
            assert call.kwargs.get("limit") == 25

    def test_home_non_english_drops_require_cover(self, mock_empty_search):
        """Non-English homepage groups use require_cover=False + larger limit
        so subject carousels don't silently empty out."""
        client.get("/?language=fr")
        # build_home_feed.fetch_one passes language as a kwarg, so identify
        # group-fetch calls by presence of "facets" + non-empty title.
        group_calls = [c for c in mock_empty_search.call_args_list
                       if c.kwargs.get("facets") is not None]
        assert group_calls, "expected at least one group search call"
        for call in group_calls:
            assert call.kwargs.get("require_cover") is False
            assert call.kwargs.get("limit") == 50

    def test_home_english_keeps_require_cover_and_default_limit(self, mock_empty_search):
        """English homepage keeps the cover requirement and 25-result limit."""
        client.get("/?language=en")
        group_calls = [c for c in mock_empty_search.call_args_list
                       if c.kwargs.get("facets") is not None]
        assert group_calls, "expected at least one group search call"
        for call in group_calls:
            assert call.kwargs.get("require_cover") is True
            assert call.kwargs.get("limit") == 25

    def test_home_non_english_drops_year_filter_on_romance(self):
        """Non-English Romance/Textbooks groups drop the English year window."""
        from pyopds2_openlibrary import OpenLibraryDataProvider
        en_groups = OpenLibraryDataProvider._home_groups_config(language="en")
        fr_groups = OpenLibraryDataProvider._home_groups_config(language="fr")
        en_romance = next(g for g in en_groups if g[0] == "Romance")
        fr_romance = next(g for g in fr_groups if g[0] == "Romance")
        assert "first_publish_year:[1930 TO *]" in en_romance[1]
        assert "first_publish_year:[1930 TO *]" not in fr_romance[1]
        en_tb = next(g for g in en_groups if g[0] == "Textbooks")
        fr_tb = next(g for g in fr_groups if g[0] == "Textbooks")
        assert "publish_year:[1990 TO *]" in en_tb[1]
        assert "publish_year:[1990 TO *]" not in fr_tb[1]

    def test_home_groups_config_keeps_all_groups_regardless_of_corpus_size(self):
        """Even a tiny-corpus language gets all 6 non-English groups attempted —
        the empty-publications filter at the end of build_home_feed is what
        drops carousels that come back zero. Pruning by corpus size was removed
        because it hid groups that would have filled fine for mid-tier languages."""
        from pyopds2_openlibrary import OpenLibraryDataProvider
        tiny = OpenLibraryDataProvider._home_groups_config(
            language="xx", language_counts={"xx": 50},
        )
        large = OpenLibraryDataProvider._home_groups_config(
            language="fr", language_counts={"fr": 352111},
        )
        expected = {"Trending Books", "Classic Books", "Romance",
                    "Kids", "Thrillers", "Textbooks"}
        assert expected <= {g[0] for g in tiny}
        assert expected <= {g[0] for g in large}

    def test_home_groups_config_english_ignores_counts(self):
        """English path never prunes — even with a deliberately tiny count."""
        from pyopds2_openlibrary import OpenLibraryDataProvider
        groups = OpenLibraryDataProvider._home_groups_config(
            language="en", language_counts={"en": 5},
        )
        titles = [g[0] for g in groups]
        for required in ("Trending Books", "Classic Books", "Romance",
                         "Kids", "Thrillers", "Textbooks", "Standard Ebooks"):
            assert required in titles

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
        # All groups are fetched, the failed one is dropped, and the survivors
        # are paginated — so page 1 still fills to GROUPS_PER_PAGE (3).
        assert len(data.get("groups", [])) == 3


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

def _fake_facets(base_url="", query="test", sort=None, mode="everything", language=None, title=None, total=0, availability_counts=None, media_type=None, access=None, language_counts=None):
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

class RecordingCacheBackend(NullCacheBackend):
    def __init__(self):
        self.cached_calls: list[tuple] = []  # (key, ttl)

    async def cached(self, key: str, ttl: int, fetch, is_valid=None):
        self.cached_calls.append((key, ttl))
        return await fetch()

    async def cached_swr(self, key: str, fresh_ttl: int, stale_ttl: int, fetch, is_valid=None):
        self.cached_calls.append((key, fresh_ttl))
        return await fetch()


class HitCacheBackend(NullCacheBackend):
    def __init__(self, data: dict):
        self._data = data

    async def cached(self, key: str, ttl: int, fetch, is_valid=None):
        return self._data

    async def cached_swr(self, key: str, fresh_ttl: int, stale_ttl: int, fetch, is_valid=None):
        return self._data


class TestHomeCacheDevMode:
    def test_cache_miss_triggers_provider_and_stores(self, mock_empty_search):
        """On cache MISS, provider is called and result is passed through cached()."""
        rec = RecordingCacheBackend()
        fastapi_app.dependency_overrides[get_cache] = lambda: rec
        client.get("/")
        assert mock_empty_search.called
        assert any(k.startswith("opds:home:") for k, _ in rec.cached_calls)

    def test_cache_hit_skips_provider(self, mock_empty_search):
        """On cache HIT, provider is not called — cached dict returned directly."""
        fake = {"metadata": {"title": "Cached"}, "links": [], "groups": [], "navigation": []}
        fastapi_app.dependency_overrides[get_cache] = lambda: HitCacheBackend(fake)
        resp = client.get("/")
        assert resp.status_code == 200
        assert not mock_empty_search.called

    def test_default_params_use_longer_ttl(self, mock_empty_search):
        """Default homepage params (no filters) use TTL_HOME_DEFAULT."""
        from app import cache as cache_module
        rec = RecordingCacheBackend()
        fastapi_app.dependency_overrides[get_cache] = lambda: rec
        client.get("/")
        home_calls = [(k, t) for k, t in rec.cached_calls if k.startswith("opds:home:")]
        assert home_calls
        ttl = home_calls[0][1]
        expected = cache_module.TTL_HOME_DEFAULT_SECONDS
        assert abs(ttl - expected) <= expected * 0.10 + 1

    def test_non_default_params_use_shorter_ttl(self, mock_empty_search):
        """Non-default homepage params use TTL_HOME_NONDEFAULT."""
        from app import cache as cache_module
        rec = RecordingCacheBackend()
        fastapi_app.dependency_overrides[get_cache] = lambda: rec
        client.get("/?language=fr")
        home_calls = [(k, t) for k, t in rec.cached_calls if k.startswith("opds:home:")]
        assert home_calls
        ttl = home_calls[0][1]
        expected = cache_module.TTL_HOME_NONDEFAULT_SECONDS
        assert abs(ttl - expected) <= expected * 0.10 + 1

    def test_search_is_cached_on_demand(self, mock_empty_search):
        """Every /search response (incl. free-text + facet filters) routes through
        the cache, so a repeat hit serves from store instead of OL."""
        rec = RecordingCacheBackend()
        fastapi_app.dependency_overrides[get_cache] = lambda: rec
        client.get("/search?query=python&mode=ebooks")
        assert any(k.startswith("opds:search:") for k, _ in rec.cached_calls)


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

    def _make_public_record(self, edition_access: str = "public"):
        from pyopds2_openlibrary import OpenLibraryDataRecord
        return OpenLibraryDataRecord.model_validate({
            "key": "/works/OL9W", "title": "Public Book",
            "ebook_access": "public",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL9M", "title": "Public Edition",
                                   "ebook_access": edition_access,
                                   "providers": [{"provider_name": "ia", "url": "https://archive.org/p"}],
                                   "ia": ["pubident"], "cover_i": 9}]},
        })

    def test_public_excluded_from_borrow_mode_when_query_has_ebook_access(self):
        """Standard Ebooks scenario: query bakes in ebook_access:public, user
        selects Available to Borrow. Mode must win — zero results expected."""
        from pyopds2_openlibrary import OpenLibraryDataProvider
        public = self._make_public_record()
        captured: dict = {}

        def fake_get(url, params=None, **kwargs):
            captured["q"] = (params or {}).get("q")
            mock = MagicMock()
            mock.json.return_value = {"docs": [public.model_dump()], "numFound": 1}
            return mock

        with patch("pyopds2_openlibrary._get", side_effect=fake_get):
            resp = OpenLibraryDataProvider.search(
                query='publisher:"Standard Ebooks" ebook_access:public',
                require_cover=False, access=None,
                facets={"mode": "ebooks"},
            )
        # Solr query must have been rewritten so ebook_access:public was
        # replaced with the borrow range — otherwise mode is silently ignored.
        assert "ebook_access:public" not in captured["q"]
        assert "ebook_access:(borrowable OR printdisabled)" in captured["q"]
        # Post-filter must strip the public record even if Solr returned it.
        assert resp.records == []

    def test_public_edition_excluded_from_borrow_mode_when_work_is_borrowable(self):
        """Displayed-edition rule: work=borrowable but the surfaced edition is
        public — must not appear under Available to Borrow because the user
        would see an open-access book."""
        from pyopds2_openlibrary import OpenLibraryDataRecord, OpenLibraryDataProvider
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL10W", "title": "Mixed Book",
            "ebook_access": "borrowable",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL10M", "title": "Public Edition",
                                   "ebook_access": "public",
                                   "providers": [{"provider_name": "ia", "url": "https://archive.org/m"}],
                                   "ia": ["mixedid"], "cover_i": 10}]},
        })
        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {
                "docs": [record.model_dump()], "numFound": 1,
            }
            resp = OpenLibraryDataProvider.search(
                query="test", require_cover=False, access=None,
                facets={"mode": "ebooks"},
            )
        assert resp.records == []


class TestLanguageFacetCounts:
    """Language facet must hide zero-count languages and surface counts."""

    @staticmethod
    def _seed_language_map():
        """Seed in-process language caches with a tiny known map for tests."""
        import pyopds2_openlibrary as m
        m._languages_map_cache = {"eng": "en", "fre": "fr", "spa": "es"}
        m._languages_names_cache = {"en": "English", "fr": "French", "es": "Spanish"}
        m._languages_map_fetched_at = 9e18  # far future; never refetch
        m._iso_to_marc_cache.clear()
        m._iso_to_marc_cache.update({"en": "eng", "fr": "fre", "es": "spa"})

    def test_build_language_links_hides_zero_count_languages(self):
        from pyopds2_openlibrary import _build_language_links
        self._seed_language_map()
        links = _build_language_links(
            language=None,
            href_fn=lambda c: f"/?language={c}" if c else "/",
            counts={"en": 100, "es": 50},  # fr omitted → count 0 → hidden
        )
        titles = [l["title"] for l in links]
        assert "All" in titles
        assert "English" in titles
        assert "Spanish" in titles
        assert "French" not in titles

    def test_build_language_links_shows_count_in_properties(self):
        from pyopds2_openlibrary import _build_language_links
        self._seed_language_map()
        links = _build_language_links(
            language=None,
            href_fn=lambda c: f"/?language={c}" if c else "/",
            counts={"en": 100, "fr": 7},
        )
        by_title = {l["title"]: l for l in links}
        assert by_title["English"]["properties"]["numberOfItems"] == 100
        assert by_title["French"]["properties"]["numberOfItems"] == 7

    def test_build_language_links_falls_back_when_counts_none(self):
        """No counts available → emit full language list (back-compat path)."""
        from pyopds2_openlibrary import _build_language_links
        self._seed_language_map()
        links = _build_language_links(
            language=None,
            href_fn=lambda c: f"/?language={c}" if c else "/",
            counts=None,
        )
        titles = {l["title"] for l in links}
        assert {"All", "English", "French", "Spanish"} <= titles

    def test_build_language_links_keeps_active_language_even_if_zero(self):
        """Active language must always be emitted so the UI can show it as selected."""
        from pyopds2_openlibrary import _build_language_links
        self._seed_language_map()
        links = _build_language_links(
            language="fr",
            href_fn=lambda c: f"/?language={c}" if c else "/",
            counts={"en": 100},  # fr count is 0
        )
        fr = next((l for l in links if l["title"] == "French"), None)
        assert fr is not None
        assert fr.get("rel") == "self"

    def test_fetch_language_counts_parses_languages_endpoint(self):
        self._seed_language_map()
        with patch(LANG_COUNTS_PATCH_TARGET, _REAL_FETCH_LANG_COUNTS), \
             patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = [
                {"name": "English", "marc_code": "eng", "ebook_edition_count": 123},
                {"name": "French", "marc_code": "fre", "ebook_edition_count": 45},
                {"name": "Spanish", "marc_code": "spa", "ebook_edition_count": 7},
                {"name": "Pali", "marc_code": "pli", "ebook_edition_count": 0},
            ]
            counts = OpenLibraryDataProvider.fetch_language_counts()
        assert counts == {"en": 123, "fr": 45, "es": 7}

    def test_fetch_language_counts_returns_none_on_error(self):
        with patch(LANG_COUNTS_PATCH_TARGET, _REAL_FETCH_LANG_COUNTS), \
             patch("pyopds2_openlibrary._get", side_effect=RuntimeError("boom")):
            counts = OpenLibraryDataProvider.fetch_language_counts("q", mode="ebooks")
        assert counts is None


# ---------------------------------------------------------------------------
# Issue #87: language/availability/media-type facet consistency
# ---------------------------------------------------------------------------

# Servable floor for mode=everything: only books OL can serve over OPDS
# (an ebook of some kind), excluding the lexicographically-adjacent ``no_ebook``
# (print-only) value that an unfiltered query would otherwise return.
_EVERYTHING_FLOOR = "ebook_access:(borrowable OR printdisabled OR public)"


class TestEverythingModeServableFloor:
    """Bug 2: mode=everything returned print-only works that were then dropped
    by _has_acquisition_options, leaving an empty feed with an inflated total."""

    def test_everything_search_query_floors_to_servable(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider as P
        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {"docs": [], "numFound": 0}
            P.search(query="subject:fiction", facets={"mode": "everything"}, require_cover=False)
        q = mock_get.call_args.kwargs["params"]["q"]
        assert _EVERYTHING_FLOOR in q

    def test_everything_count_query_floors_to_servable(self):
        """_count_for_mode must apply the same floor so the Everything badge
        matches the (now servable-only) result total."""
        from pyopds2_openlibrary import OpenLibraryDataProvider as P
        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {"numFound": 3}
            P._count_for_mode("subject:fiction", "everything")
        q = mock_get.call_args.kwargs["params"]["q"]
        assert _EVERYTHING_FLOOR in q


class TestFacetCountsLanguage:
    """Bug 1: per-mode availability counts ignored the active language filter,
    so a subset mode (open_access) could report a larger count than the
    superset (everything)."""

    def test_count_for_mode_adds_language_clause(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider as P
        with patch("pyopds2_openlibrary._get") as mock_get, \
             patch("pyopds2_openlibrary.iso_639_1_to_marc", return_value="zul"):
            mock_get.return_value.json.return_value = {"numFound": 5}
            P._count_for_mode("subject:fiction", "open_access", language="zu")
        q = mock_get.call_args.kwargs["params"]["q"]
        assert "language:zul" in q

    def test_fetch_facet_counts_threads_language(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider as P
        seen: list = []

        def fake(query, mode, language=None):
            seen.append((mode, language))
            return 1

        # Restore the real fetch_facet_counts (the autouse fixture mocks it).
        with patch(FACET_COUNTS_PATCH_TARGET, _REAL_FETCH_FACET_COUNTS), \
             patch.object(P, "_count_for_mode", side_effect=fake):
            P.fetch_facet_counts("subject:fiction", language="zu")
        # Every counted (non-buyable) mode must carry the language.
        assert seen and all(lang == "zu" for _, lang in seen)

    def test_search_route_passes_language_to_facet_counts(self):
        captured: dict = {}

        def fake_counts(query, media_type=None, language=None):
            captured["language"] = language
            return _FAKE_AVAILABILITY_COUNTS.copy()

        with patch(SEARCH_PATCH_TARGET, return_value=_make_search_response()), \
             patch(FACET_COUNTS_PATCH_TARGET, side_effect=fake_counts):
            client.get("/search?query=test&language=zu")
        assert captured.get("language") == "zu"


class TestHomeEmptyGroupsFilteredBeforePagination:
    """Bug 3: empty carousels were dropped *after* pagination, so a page whose
    slice happened to contain empty groups (common for minority languages)
    showed fewer than GROUPS_PER_PAGE carousels — typically only Trending."""

    def test_non_empty_groups_fill_page_one(self):
        rec = _make_record()

        def fake_search(**kwargs):
            # Simulate a minority-language corpus: only Trending + Kids fill.
            if kwargs.get("title") in ("Trending Books", "Kids"):
                return _make_search_response(records=[rec], total=1)
            return _make_search_response()

        with patch(SEARCH_PATCH_TARGET, side_effect=fake_search):
            data = client.get("/?language=zu").json()
        titles = [g["metadata"]["title"] for g in data.get("groups", [])]
        # Both content-bearing groups must surface on page 1, with no empties.
        assert "Trending Books" in titles
        assert "Kids" in titles
        assert all(t in ("Trending Books", "Kids") for t in titles)


# ---------------------------------------------------------------------------
# Issue #86: aggregateRating on publications
# ---------------------------------------------------------------------------

class TestAggregateRating:
    def _rated_record(self, average=4.213573, count=1002):
        from pyopds2_openlibrary import OpenLibraryDataRecord
        data = {
            "key": "/works/OL1W",
            "title": "Rated Book",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL1M", "title": "Rated Book"}]},
        }
        if average is not None:
            data["ratings_average"] = average
        if count is not None:
            data["ratings_count"] = count
        return OpenLibraryDataRecord.model_validate(data)

    def test_metadata_includes_full_aggregate_rating(self):
        meta = self._rated_record().metadata().model_dump(by_alias=True, exclude_none=True)
        ar = meta.get("aggregateRating")
        assert ar == {
            "@type": "AggregateRating",
            "ratingValue": 4.21,   # rounded to 2 decimals
            "ratingCount": 1002,
            "bestRating": 5,
            "worstRating": 1,
        }

    def test_no_aggregate_rating_when_count_zero(self):
        meta = self._rated_record(average=0, count=0).metadata().model_dump(by_alias=True, exclude_none=True)
        assert "aggregateRating" not in meta

    def test_no_aggregate_rating_when_fields_absent(self):
        meta = self._rated_record(average=None, count=None).metadata().model_dump(by_alias=True, exclude_none=True)
        assert "aggregateRating" not in meta

    def test_search_requests_rating_fields(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider as P
        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {"docs": [], "numFound": 0}
            P.search(query="test", require_cover=False)
        fields = mock_get.call_args.kwargs["params"]["fields"]
        assert "ratings_average" in fields
        assert "ratings_count" in fields


# ---------------------------------------------------------------------------
# Issue #85: subjects on publications
# ---------------------------------------------------------------------------

class TestPublicationSubjects:
    def _record_with_subjects(self, subjects):
        from pyopds2_openlibrary import OpenLibraryDataRecord
        return OpenLibraryDataRecord.model_validate({
            "key": "/works/OL1W",
            "title": "Subject Book",
            "subject": subjects,
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL1M", "title": "Subject Book"}]},
        })

    def test_metadata_emits_subject_objects(self):
        meta = self._record_with_subjects(["Historical fiction", "Fiction in translation"]) \
            .metadata().model_dump(by_alias=True, exclude_none=True)
        subs = meta.get("subject")
        assert isinstance(subs, list) and len(subs) == 2
        first = subs[0]
        assert first["name"] == "Historical fiction"
        link = first["links"][0]
        assert link["type"] == "application/opds+json"
        # Browse link is an in-app /search by subject name.
        assert "/search?" in link["href"]
        assert 'subject:"Historical fiction"' in unquote_plus(link["href"])

    def test_subjects_capped_at_ten(self):
        many = [f"Subject {i}" for i in range(25)]
        meta = self._record_with_subjects(many).metadata().model_dump(by_alias=True, exclude_none=True)
        subs = meta["subject"]
        assert len(subs) == 10
        assert [s["name"] for s in subs] == many[:10]

    def test_no_subject_key_when_absent(self):
        from pyopds2_openlibrary import OpenLibraryDataRecord
        rec = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL1W", "title": "No Subjects",
            "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                         "docs": [{"key": "/books/OL1M", "title": "No Subjects"}]},
        })
        meta = rec.metadata().model_dump(by_alias=True, exclude_none=True)
        assert "subject" not in meta

    def test_embedded_quote_does_not_break_query(self):
        meta = self._record_with_subjects(['Quote " inside']) \
            .metadata().model_dump(by_alias=True, exclude_none=True)
        href = unquote_plus(meta["subject"][0]["links"][0]["href"])
        # Exactly one opening and one closing quote around the value.
        assert 'subject:"Quote  inside"' in href
        # Name field keeps the original text.
        assert meta["subject"][0]["name"] == 'Quote " inside'

    def test_search_requests_subject_field(self):
        from pyopds2_openlibrary import OpenLibraryDataProvider as P
        with patch("pyopds2_openlibrary._get") as mock_get:
            mock_get.return_value.json.return_value = {"docs": [], "numFound": 0}
            P.search(query="test", require_cover=False)
        assert "subject" in mock_get.call_args.kwargs["params"]["fields"]


# ---------------------------------------------------------------------------
# Issue #88 follow-up: provider version skew must not 500 the catalog.
# The deployed provider can lag the app's route code (e.g. missing
# fetch_language_counts); routes must degrade gracefully, not crash.
# ---------------------------------------------------------------------------

class TestProviderVersionSkew:
    def test_home_survives_missing_fetch_language_counts(self, mock_single_record):
        # Simulate an older provider where the method is absent.
        with patch.object(OpenLibraryDataProvider, "fetch_language_counts", None):
            resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json().get("groups")

    def test_search_survives_missing_fetch_language_counts(self, mock_empty_search):
        with patch.object(OpenLibraryDataProvider, "fetch_language_counts", None):
            resp = client.get("/search?query=test")
        assert resp.status_code == 200

    def test_home_survives_fetch_language_counts_raising(self, mock_single_record):
        def boom(*a, **k):
            raise RuntimeError("upstream down")
        with patch.object(OpenLibraryDataProvider, "fetch_language_counts", staticmethod(boom)):
            resp = client.get("/")
        assert resp.status_code == 200


class TestHttpCaching:
    """ETag / Cache-Control / conditional-GET behavior on OPDS responses."""

    def test_home_sets_etag_and_cache_control(self, mock_empty_search):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers.get("etag")
        assert "max-age=" in resp.headers.get("cache-control", "")
        assert resp.headers.get("x-cache") in ("HIT", "MISS")

    def test_conditional_get_returns_304(self, mock_empty_search):
        first = client.get("/")
        assert first.status_code == 200
        etag = first.headers["etag"]
        second = client.get("/", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers.get("etag") == etag

    def test_mismatched_etag_returns_full_body(self, mock_empty_search):
        resp = client.get("/", headers={"If-None-Match": '"stale-etag"'})
        assert resp.status_code == 200
        assert resp.content  # full body served
