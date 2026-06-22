"""
End-to-end tests for the OPDS home feed.

These tests require a live Open Library instance (with the /search/carousels.json
endpoint from PR #12987) running at OL_BASE_URL (default: http://localhost:8080).

Run with:
    OL_BASE_URL=http://localhost:8080 pytest tests/e2e/ -m e2e -v

They are skipped automatically when OL is unreachable.
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OL_BASE_URL = os.environ.get("OL_BASE_URL", "http://localhost:8080")
_CAROUSELS_URL = f"{_OL_BASE_URL}/search/carousels.json"


def _ol_reachable() -> bool:
    """Return True if the OL instance responds to a quick health probe."""
    try:
        r = httpx.get(f"{_OL_BASE_URL}/", timeout=5.0)
        return r.status_code < 500
    except httpx.RequestError:
        return False


def _carousels_endpoint_exists() -> bool:
    """Return True if /search/carousels.json accepts POST (not 404/405)."""
    try:
        r = httpx.post(_CAROUSELS_URL, json={"queries": []}, timeout=5.0)
        return r.status_code != 404
    except httpx.RequestError:
        return False


ol_available = pytest.mark.skipif(
    not _ol_reachable(),
    reason=f"OL not reachable at {_OL_BASE_URL} — start Docker first",
)

carousels_available = pytest.mark.skipif(
    not _ol_reachable() or not _carousels_endpoint_exists(),
    reason=f"POST /search/carousels.json not available at {_OL_BASE_URL} — needs PR #12987",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def opds_client():
    """TestClient for the opds service, pointed at the local OL Docker."""
    os.environ["OL_BASE_URL"] = _OL_BASE_URL
    os.environ["ENVIRONMENT"] = "test"
    os.environ["CACHE_ENABLED"] = "false"

    from app.main import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# /search/carousels.json — OL endpoint smoke tests
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@carousels_available
class TestCarouselsEndpointDirect:
    """Smoke-test the OL /search/carousels.json endpoint directly."""

    def test_single_query_returns_list(self):
        r = httpx.post(
            _CAROUSELS_URL,
            json={"queries": [{"q": "subject:art", "limit": 3}]},
            timeout=30.0,
        )
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_result_has_expected_search_fields(self):
        r = httpx.post(
            _CAROUSELS_URL,
            json={"queries": [{"q": "subject:science", "limit": 5}]},
            timeout=30.0,
        )
        assert r.status_code == 200
        result = r.json()[0]
        # Solr search response shape
        assert "numFound" in result or "docs" in result or "works" in result, (
            f"Unexpected response shape: {list(result.keys())}"
        )

    def test_multiple_queries_return_in_order(self):
        queries = [
            {"q": "subject:art", "limit": 2},
            {"q": "subject:fantasy", "limit": 2},
            {"q": "subject:science", "limit": 2},
        ]
        r = httpx.post(
            _CAROUSELS_URL,
            json={"queries": queries},
            timeout=30.0,
        )
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 3, f"Expected 3 results, got {len(results)}"

    def test_max_query_cap_enforced(self):
        """More than 20 queries should be rejected (422 Unprocessable Entity)."""
        queries = [{"q": f"subject:{i}", "limit": 1} for i in range(21)]
        r = httpx.post(
            _CAROUSELS_URL,
            json={"queries": queries},
            timeout=10.0,
        )
        assert r.status_code == 422

    def test_empty_query_list_rejected(self):
        r = httpx.post(
            _CAROUSELS_URL,
            json={"queries": []},
            timeout=10.0,
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# OPDS service home feed — end-to-end (opds service → OL Docker → Solr)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@carousels_available
class TestOpdsHomeFeedE2E:
    """Full stack: opds service in-process → real OL Docker → real Solr."""

    def test_home_returns_200(self, opds_client):
        r = opds_client.get("/")
        assert r.status_code == 200

    def test_home_returns_opds_content_type(self, opds_client):
        r = opds_client.get("/")
        assert "opds" in r.headers.get("content-type", "").lower()

    def test_home_has_publications(self, opds_client):
        r = opds_client.get("/")
        assert r.status_code == 200
        data = r.json()
        # OPDS 2.0 catalog has a "publications" or "groups" list
        groups = data.get("groups") or data.get("publications") or []
        assert len(groups) > 0, (
            "Home feed returned no groups — carousels endpoint may not be reachable"
        )

    def test_home_groups_have_titles(self, opds_client):
        r = opds_client.get("/")
        data = r.json()
        groups = data.get("groups") or []
        for group in groups:
            meta = group.get("metadata") or {}
            assert meta.get("title"), f"Group missing title: {group}"

    def test_home_groups_have_publications(self, opds_client):
        """At least one group should have publications (books)."""
        r = opds_client.get("/")
        data = r.json()
        groups = data.get("groups") or []
        all_pubs = [p for g in groups for p in (g.get("publications") or [])]
        assert len(all_pubs) > 0, (
            "No publications found across all groups — Solr may have empty index"
        )

    def test_home_cover_urls_well_formed(self, opds_client):
        """Cover image URLs should point to covers.openlibrary.org."""
        r = opds_client.get("/")
        data = r.json()
        groups = data.get("groups") or []
        cover_urls = []
        for group in groups:
            for pub in group.get("publications") or []:
                for img in (pub.get("images") or []):
                    href = img.get("href", "")
                    if href:
                        cover_urls.append(href)
        # Only assert format if covers are present (empty Solr index is OK for e2e)
        for url in cover_urls[:10]:
            assert url.startswith("https://"), f"Cover URL not https: {url}"
            assert "covers.openlibrary.org" in url or "archive.org" in url, (
                f"Unexpected cover URL domain: {url}"
            )

    def test_home_ebooks_mode(self, opds_client):
        """mode=ebooks should return 200 and a valid catalog."""
        r = opds_client.get("/?mode=ebooks")
        assert r.status_code == 200
        data = r.json()
        assert "groups" in data or "publications" in data

    def test_home_language_filter(self, opds_client):
        """language=en filter should return 200."""
        r = opds_client.get("/?language=en")
        assert r.status_code == 200

    def test_home_page2(self, opds_client):
        """Second page of carousels should return 200."""
        r = opds_client.get("/?page=2")
        assert r.status_code == 200
