"""End-to-end tests for the running OPDS service.

These tests require a live service — they are skipped by default and only
run when you explicitly select the ``e2e`` marker:

    pytest -m e2e

Point them at any running instance via the ``BASE_URL`` environment variable
(defaults to ``http://127.0.0.1:8090``). Start a local instance with:

    make serve          # or: CACHE_ENABLED=false uvicorn app.main:app --port 8090

These tests document (and protect) behavioural invariants — not just "does the
service respond 200" but "does the response have the right shape and the right
absence of expensive fields."
"""
from __future__ import annotations

import os

import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8090").rstrip("/")


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_health(client: httpx.Client) -> None:
    r = client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Home feed
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_home_returns_groups(client: httpx.Client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    groups = body.get("groups", [])
    assert len(groups) > 0, "home feed must return at least one carousel group"
    # Every group must have at least one publication
    for g in groups:
        pubs = g.get("publications", [])
        assert len(pubs) > 0, f"group '{g.get('metadata', {}).get('title')}' has no publications"


@pytest.mark.e2e
def test_home_page2_returns_groups(client: httpx.Client) -> None:
    r = client.get("/?page=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body.get("groups", [])) > 0, "page 2 of home feed must return groups"


@pytest.mark.e2e
def test_home_no_duplicate_trending(client: httpx.Client) -> None:
    """Trending Books must appear at most once across all home-feed groups.

    Guards against regressions in pyopds2_openlibrary's build_home_feed that
    might duplicate a carousel group in the returned data.
    """
    r = client.get("/")
    assert r.status_code == 200
    trending_groups = [
        g for g in r.json().get("groups", [])
        if "trending" in g.get("metadata", {}).get("title", "").lower()
    ]
    assert len(trending_groups) <= 1, (
        f"Trending Books appears {len(trending_groups)} times — duplicate carousel bug"
    )


@pytest.mark.e2e
def test_home_trending_carousel_carries_sort_popular(client: httpx.Client) -> None:
    """The Trending Books carousel's self link carries OPDS's sort/popular
    besides self, so a client finds it by relation rather than by title."""
    r = client.get("/")
    assert r.status_code == 200
    trending = next(
        (g for g in r.json().get("groups", [])
         if g.get("metadata", {}).get("title") == "Trending Books"),
        None,
    )
    assert trending is not None, "Trending Books carousel missing from page 1"
    self_links = [l for l in trending.get("links", []) if _has_rel(l, "self")]
    assert len(self_links) == 1
    assert self_links[0]["rel"] == ["self", "http://opds-spec.org/sort/popular"]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_search_returns_results(client: httpx.Client) -> None:
    r = client.get("/search?query=tolkien")
    assert r.status_code == 200
    body = r.json()
    total = body.get("metadata", {}).get("numberOfItems", 0)
    assert total > 0, "search for 'tolkien' must return results"
    assert len(body.get("publications", [])) > 0


@pytest.mark.e2e
def test_search_availability_facets_present(client: httpx.Client) -> None:
    r = client.get("/search?query=tolkien")
    assert r.status_code == 200
    facets = r.json().get("facets", [])
    titles = [f.get("metadata", {}).get("title") for f in facets]
    assert "Availability" in titles, f"Availability facet group missing; got: {titles}"


FACET_RELS = {
    "Availability": "https://openlibrary.org/opds/rel/facet/availability",
    "Language": "https://openlibrary.org/opds/rel/facet/language",
    "Media Type": "https://openlibrary.org/opds/rel/facet/media-type",
    "Access": "https://openlibrary.org/opds/rel/facet/access",
}


def _has_rel(link: dict, rel: str) -> bool:
    value = link.get("rel")
    return rel in value if isinstance(value, list) else value == rel


@pytest.mark.e2e
@pytest.mark.parametrize("path", ["/search?query=tolkien&mode=ebooks", "/?media_type=ebook", "/authors/OL23919A"])
def test_facet_links_carry_their_group_rel(client: httpx.Client, path: str) -> None:
    """Every option link of a facet group carries the group's relation, the
    applied option ["self", <relation>] — what a client selects facets by."""
    r = client.get(path)
    assert r.status_code == 200
    facets = r.json().get("facets", [])
    assert [f["metadata"]["title"] for f in facets] == list(FACET_RELS), path
    for group in facets:
        rel = FACET_RELS[group["metadata"]["title"]]
        for link in group["links"]:
            assert _has_rel(link, rel), (path, group["metadata"]["title"], link)
        applied = [l for l in group["links"] if _has_rel(l, "self")]
        assert len(applied) == 1, (path, group["metadata"]["title"])
        assert applied[0]["rel"] == ["self", rel]


@pytest.mark.e2e
def test_facet_rels_resolve(client: httpx.Client) -> None:
    """A relation the feed coins is a URL that resolves to its documentation.
    Checked on the instance under test, at the path under its own base."""
    for rel in FACET_RELS.values():
        r = client.get(rel.removeprefix("https://openlibrary.org/opds"))
        assert r.status_code == 200, rel
        assert r.headers["content-type"].startswith("text/html")
        assert rel in r.text


@pytest.mark.e2e
def test_search_availability_facets_have_no_numberOfItems(client: httpx.Client) -> None:
    """Availability facet links must NOT carry numberOfItems.

    numberOfItems on availability links requires 4 extra OL Solr queries per
    search request (one per mode: everything/ebooks/open_access/buyable) with
    limit=0. These are expensive and reader.archive.org does not render them.
    If this test fails, those requests have been re-introduced — investigate.
    """
    r = client.get("/search?query=tolkien")
    assert r.status_code == 200
    facets = r.json().get("facets", [])
    availability = next(
        (f for f in facets if f.get("metadata", {}).get("title") == "Availability"),
        None,
    )
    assert availability is not None
    for link in availability.get("links", []):
        ni = link.get("properties", {}).get("numberOfItems")
        assert ni is None, (
            f"Availability facet link '{link.get('title')}' has numberOfItems={ni}; "
            "this means expensive per-mode Solr queries have been re-introduced"
        )


@pytest.mark.e2e
def test_search_language_facets_have_numberOfItems(client: httpx.Client) -> None:
    """Language facet links SHOULD carry numberOfItems (from the cheap languages.json cache).

    This is distinct from the expensive availability facet counts — language
    counts come from a single cached endpoint and do not add extra OL requests
    per search. If this test fails, the language count source has regressed.
    """
    r = client.get("/search?query=tolkien")
    assert r.status_code == 200
    facets = r.json().get("facets", [])
    lang = next(
        (f for f in facets if f.get("metadata", {}).get("title") == "Language"),
        None,
    )
    assert lang is not None, "Language facet group missing"
    # At least some language links should carry numberOfItems (not the "All" sentinel)
    counts = [
        link.get("properties", {}).get("numberOfItems")
        for link in lang.get("links", [])
        if link.get("title") != "All"
    ]
    assert any(c is not None for c in counts), (
        "No language facet links carry numberOfItems — language count source may have regressed"
    )


@pytest.mark.e2e
def test_search_pagination(client: httpx.Client) -> None:
    r1 = client.get("/search?query=tolkien&page=1&limit=5")
    r2 = client.get("/search?query=tolkien&page=2&limit=5")
    assert r1.status_code == 200
    assert r2.status_code == 200

    def self_hrefs(pubs: list) -> set:
        """Extract the 'self' link href from each publication — the stable OLID-based URL."""
        return {
            next((l["href"] for l in p.get("links", []) if l.get("rel") == "self"), None)
            for p in pubs
        } - {None}

    ids1 = self_hrefs(r1.json().get("publications", []))
    ids2 = self_hrefs(r2.json().get("publications", []))
    assert ids1, "page 1 returned no identifiable publications"
    assert ids2, "page 2 returned no identifiable publications"
    assert ids1.isdisjoint(ids2), "page 1 and page 2 results overlap — pagination is broken"


# ---------------------------------------------------------------------------
# Books / Authors (smoke only)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_book_detail(client: httpx.Client) -> None:
    # The Hobbit — stable OLID
    r = client.get("/books/OL7353617M")
    assert r.status_code in (200, 404), f"unexpected status {r.status_code}"
    if r.status_code == 200:
        assert "metadata" in r.json()


@pytest.mark.e2e
def test_author_detail(client: httpx.Client) -> None:
    # Tolkien — stable OLID
    r = client.get("/authors/OL26320A")
    assert r.status_code in (200, 404), f"unexpected status {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        assert "metadata" in body


@pytest.mark.e2e
def test_author_page_has_portrait_bio_and_popular_books(client: httpx.Client) -> None:
    # Ursula K. Le Guin: photo, bio and well-rated books
    r = client.get("/authors/OL31353A")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["metadata"]["title"] == "Ursula K. Le Guin"
    # The one-line Wikidata description from the shipped table is the
    # subtitle; the biography from the author API is the description.
    subtitle = body["metadata"]["subtitle"]
    assert subtitle and "\n" not in subtitle and len(subtitle) < 200
    assert "author" in subtitle.lower()
    description = body["metadata"]["description"]
    assert len(description) > len(subtitle)
    assert "Le Guin" in description
    [image] = body["images"]
    assert image["href"].startswith("https://covers.openlibrary.org/a/id/")
    assert image["rel"] == "https://openlibrary.org/opds/rel/portrait"
    assert image["width"] and image["height"]
    assert any(l["rel"] == "alternate" and l["type"] == "text/html" for l in body["links"])
    [group] = body["groups"]
    assert group["publications"]
    [self_link] = group["links"]
    assert self_link["rel"] == ["self", "http://opds-spec.org/sort/popular"]
    assert self_link["href"].endswith("/authors/OL31353A?sort=rating")
    author = body["publications"][0]["metadata"]["author"][0]
    assert all(l["rel"] == "author" for l in author["links"])


@pytest.mark.e2e
def test_author_page_sorted_by_rating_has_no_carousel(client: httpx.Client) -> None:
    r = client.get("/authors/OL31353A?sort=rating")
    assert r.status_code == 200
    assert "groups" not in r.json()
    assert client.get("/authors/OL31353A?sort=bogus").status_code == 422


@pytest.mark.e2e
def test_portrait_relation_resolves(client: httpx.Client) -> None:
    r = client.get("/rel/portrait")
    assert r.status_code == 200
    assert "portrait" in r.text
