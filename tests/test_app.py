"""
Tests for the opds.openlibrary.org FastAPI service.

These tests use pytest and httpx's AsyncClient / TestClient.
Network calls to openlibrary.org are mocked so tests run offline.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import FEATURED_SUBJECTS

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_search_response(records=None, total=0):
    """Return a minimal DataProvider.SearchResponse-like object."""
    from pyopds2.provider import DataProvider
    from pyopds2_openlibrary import OpenLibraryDataProvider, OpenLibraryDataRecord

    mock_records = records or []
    return DataProvider.SearchResponse(
        provider=OpenLibraryDataProvider,
        records=mock_records,
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
            "key": f"/works/OL1W",
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


# ---------------------------------------------------------------------------
# GET /opds
# ---------------------------------------------------------------------------

class TestOpdsHome:
    def test_returns_200(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ):
            resp = client.get("/opds")
        assert resp.status_code == 200

    def test_content_type(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ):
            resp = client.get("/opds")
        assert "application/opds+json" in resp.headers["content-type"]

    def test_metadata_title(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ):
            resp = client.get("/opds")
        data = resp.json()
        assert data["metadata"]["title"] == "Open Library"

    def test_navigation_has_featured_subjects(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ):
            resp = client.get("/opds")
        data = resp.json()
        nav_titles = {n["title"] for n in data.get("navigation", [])}
        for subject in FEATURED_SUBJECTS:
            assert subject["presentable_name"] in nav_titles

    def test_groups_present(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ):
            resp = client.get("/opds")
        data = resp.json()
        assert len(data.get("groups", [])) == 6

    def test_links_include_self_and_search(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ):
            resp = client.get("/opds")
        data = resp.json()
        rels = {lnk["rel"] for lnk in data.get("links", [])}
        assert "self" in rels
        assert "search" in rels


# ---------------------------------------------------------------------------
# GET /opds/search
# ---------------------------------------------------------------------------

class TestOpdsSearch:
    def test_returns_200(self):
        record = _make_record()
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(records=[record], total=1),
        ):
            resp = client.get("/opds/search?query=Python")
        assert resp.status_code == 200

    def test_content_type(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ):
            resp = client.get("/opds/search")
        assert "application/opds+json" in resp.headers["content-type"]

    def test_metadata_title(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ):
            resp = client.get("/opds/search")
        data = resp.json()
        assert data["metadata"]["title"] == "Search Results"

    def test_publications_in_response(self):
        record = _make_record(title="Python Cookbook")
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(records=[record], total=1),
        ):
            resp = client.get("/opds/search?query=Python")
        data = resp.json()
        assert len(data.get("publications", [])) == 1
        assert data["publications"][0]["metadata"]["title"] == "Python Cookbook"

    def test_pagination_params_forwarded(self):
        """Check that page/limit are forwarded to the provider."""
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(),
        ) as mock_search:
            client.get("/opds/search?query=test&page=2&limit=10")
        mock_search.assert_called_once_with(
            query="test", limit=10, offset=10, sort=None
        )

    def test_invalid_limit_rejected(self):
        resp = client.get("/opds/search?limit=0")
        assert resp.status_code == 422

    def test_invalid_page_rejected(self):
        resp = client.get("/opds/search?page=0")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /opds/books/{edition_olid}
# ---------------------------------------------------------------------------

class TestOpdsBooks:
    def test_returns_200_for_known_edition(self):
        record = _make_record(title="Moby-Dick", edition_key="OL7353617M")
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(records=[record], total=1),
        ):
            resp = client.get("/opds/books/OL7353617M")
        assert resp.status_code == 200

    def test_content_type(self):
        record = _make_record()
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(records=[record], total=1),
        ):
            resp = client.get("/opds/books/OL1M")
        assert "application/opds-publication+json" in resp.headers["content-type"]

    def test_returns_404_for_unknown_edition(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(records=[], total=0),
        ):
            resp = client.get("/opds/books/OL9999999M")
        assert resp.status_code == 404

    def test_404_body_has_detail(self):
        with patch(
            "app.routes.opds.OpenLibraryDataProvider.search",
            return_value=_make_search_response(records=[], total=0),
        ):
            resp = client.get("/opds/books/OL9999999M")
        data = resp.json()
        assert "detail" in data
