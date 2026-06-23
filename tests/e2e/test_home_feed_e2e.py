"""
End-to-end tests for the OPDS home feed (in-process).

These tests use FastAPI's TestClient backed by a real OL Docker instance and
validate home feed structure, content, and filtering parameters.

Automatically skipped when OL is not reachable at OL_BASE_URL.

Run with:
    OL_BASE_URL=http://localhost:8080 pytest tests/e2e/ -m e2e -v
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers / markers
# ---------------------------------------------------------------------------

_OL_BASE_URL = os.environ.get("OL_BASE_URL", "http://localhost:8080")


def _ol_reachable() -> bool:
    """Return True if the OL instance responds."""
    try:
        r = httpx.get(f"{_OL_BASE_URL}/", timeout=5.0)
        return r.status_code < 500
    except httpx.RequestError:
        return False


ol_available = pytest.mark.skipif(
    not _ol_reachable(),
    reason=f"OL not reachable at {_OL_BASE_URL} — start Docker first",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def opds_client():
    """TestClient for the OPDS service in-process, backed by live OL Docker."""
    os.environ["OL_BASE_URL"] = _OL_BASE_URL
    os.environ["ENVIRONMENT"] = "test"
    os.environ["CACHE_ENABLED"] = "false"

    from app.main import app

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Home feed integration tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@ol_available
class TestOpdsHomeFeedE2E:
    """Full stack: OPDS service in-process → real OL Docker → real Solr.

    Covers home feed structure, content, and query-parameter filtering.
    Complements the external e2e tests in tests/test_e2e.py, which test the
    same endpoints via httpx against a separately-started service instance.
    """

    def test_home_returns_200(self, opds_client):
        r = opds_client.get("/")
        assert r.status_code == 200

    def test_home_returns_opds_content_type(self, opds_client):
        r = opds_client.get("/")
        ct = r.headers.get("content-type", "").lower()
        assert "opds" in ct, f"Expected OPDS content-type, got: {ct}"

    def test_home_metadata_has_title(self, opds_client):
        """Feed-level metadata must include a title."""
        r = opds_client.get("/")
        meta = r.json().get("metadata") or {}
        assert meta.get("title"), f"Feed metadata missing title: {meta}"

    def test_home_has_groups_with_titles(self, opds_client):
        """Every group must carry a title in its metadata."""
        r = opds_client.get("/")
        groups = r.json().get("groups") or []
        assert len(groups) > 0, "Home feed returned no groups"
        for group in groups:
            meta = group.get("metadata") or {}
            assert meta.get("title"), f"Group missing title: {group}"

    def test_home_groups_have_publications(self, opds_client):
        """At least one publication must appear across all groups."""
        r = opds_client.get("/")
        groups = r.json().get("groups") or []
        all_pubs = [p for g in groups for p in (g.get("publications") or [])]
        assert len(all_pubs) > 0, "No publications found across all home-feed groups"

    def test_home_cover_urls_well_formed(self, opds_client):
        """Cover image URLs must be https and point to a recognised OL domain."""
        r = opds_client.get("/")
        cover_urls = [
            img.get("href", "")
            for g in (r.json().get("groups") or [])
            for pub in (g.get("publications") or [])
            for img in (pub.get("images") or [])
            if img.get("href")
        ]
        for url in cover_urls[:10]:
            assert url.startswith("https://"), f"Cover URL not https: {url}"
            assert "covers.openlibrary.org" in url or "archive.org" in url, (
                f"Unexpected cover URL domain: {url}"
            )

    def test_home_navigation_links_present(self, opds_client):
        """Feed must include at least a 'self' navigation link."""
        r = opds_client.get("/")
        links = r.json().get("links") or []
        rels = {link.get("rel") for link in links}
        assert "self" in rels, f"Missing 'self' link; rels found: {rels}"

    def test_home_ebooks_mode(self, opds_client):
        """mode=ebooks must return 200 with a valid catalog."""
        r = opds_client.get("/?mode=ebooks")
        assert r.status_code == 200
        data = r.json()
        assert "groups" in data or "publications" in data

    def test_home_open_access_mode(self, opds_client):
        """mode=open_access must return 200."""
        r = opds_client.get("/?mode=open_access")
        assert r.status_code == 200

    def test_home_language_filter(self, opds_client):
        """language=en must return 200 with non-empty groups."""
        r = opds_client.get("/?language=en")
        assert r.status_code == 200
        groups = r.json().get("groups") or []
        assert len(groups) > 0, "language=en filter returned no groups"

    def test_home_page2(self, opds_client):
        """Second page must return 200 with non-empty groups."""
        r = opds_client.get("/?page=2")
        assert r.status_code == 200
        groups = r.json().get("groups") or []
        assert len(groups) > 0, "Page 2 returned no groups"
