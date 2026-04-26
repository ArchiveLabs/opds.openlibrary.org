# Author Catalog Feature Design

**Date:** 2026-04-26
**Issue:** #70 — Add support for showing books by author

---

## Overview

Add a dedicated `/authors/{olid}` OPDS 2.0 catalog endpoint that returns an author's bio and a paginated list of their books. Update publication author links to point to this new endpoint. Update WAF rules and README.

---

## Files Changed

| File | Change |
|---|---|
| `pyopds2_openlibrary/__init__.py` | Add `fetch_author_bio()` helper; add OPDS link in `get_authors()` |
| `app/routes/opds.py` | Add `GET /authors/{olid}` route |
| `WAF_ENDPOINTS.md` | New row + updated combined regex |
| `README.md` | New endpoint row + curl example |

---

## 1. Author link in publication metadata

In `pyopds2_openlibrary/__init__.py`, `get_authors()` currently produces one `Contributor` per author with a single `text/html` link to the OL author page. For each author that has a key, a second link is added:

```json
{
  "type": "application/opds+json",
  "href": "{base}/authors/{olid}"
}
```

Authors with no key (name-only records) are unchanged — they keep their current single link or no link at all.

**Before:**
```json
{
  "name": "James McBride",
  "links": [
    { "type": "text/html", "rel": "author", "href": "https://openlibrary.org/authors/OL1234A" }
  ]
}
```

**After:**
```json
{
  "name": "James McBride",
  "links": [
    { "type": "text/html", "rel": "author", "href": "https://openlibrary.org/authors/OL1234A" },
    { "type": "application/opds+json", "href": "{base}/authors/OL1234A" }
  ]
}
```

---

## 2. `fetch_author_bio(olid)` helper

New function in `pyopds2_openlibrary/__init__.py`.

- Calls `GET https://openlibrary.org/authors/{olid}.json` using the existing `_get()` helper (with retry logic).
- Returns `tuple[str | None, str | None]` — `(name, bio)`.
- Normalizes `bio` from either:
  - Plain string: `"bio": "Some text"`
  - Object: `"bio": {"type": "/type/text", "value": "Some text"}`
- Passes bio through `strip_markdown()` (same as book descriptions).
- Returns `(None, None)` on any failure (404, network error, malformed response) — never raises.

---

## 3. `GET /authors/{olid}` route

### Validation

`olid` must match the regex `^OL\d+A$`. Return HTTP 400 with `{"detail": "Invalid author OLID"}` for any non-matching value.

### Parallel fetch

Uses `asyncio.gather` with two `asyncio.to_thread` calls (same pattern as `opds_search`):

1. `fetch_author_bio(olid)` — fetches name + bio from OL author API
2. `_search(provider, query=f"author_key:{olid}", limit=limit, offset=(page-1)*limit)` — fetches paginated books

Both run concurrently; total latency = max(bio fetch, book search).

### Error handling

- Bio fetch failure: degrade gracefully — return books-only catalog with `title` = olid, no description.
- 0 books AND no bio: raise `AuthorNotFound(olid)` — a new exception class in `app/exceptions/__init__.py` that maps to HTTP 404, consistent with `EditionNotFound`.
- Book search failure: raise `UpstreamError` (same as other routes).

### Catalog structure

Built with `Catalog.create(response=search_response, paginate=False)` to avoid `add_pagination`'s use of `SEARCH_URL`. Pagination links are appended manually:

- `self` — `/authors/{olid}?page=N`
- `first` — `/authors/{olid}?page=1`
- `previous` — `/authors/{olid}?page={N-1}` (only if `page > 1`)
- `next` — `/authors/{olid}?page={N+1}` (only if `has_more`)

This is consistent with how the homepage builds its pagination links in `_home_page_href`.

Standard links also included: `search` template, shelf, profile.

### Metadata

- `title`: author name from bio fetch; fallback to olid if bio fetch failed.
- `description`: stripped bio string if present; omitted if not.
- `numberOfItems`, `itemsPerPage`, `currentPage`: set from search response totals.

### Query parameters

| Param | Default | Description |
|---|---|---|
| `page` | `1` | 1-based page number |
| `limit` | `25` | Results per page (1–100) |
| `mode` | `everything` | Availability filter (same values as `/search`) |

---

## 4. WAF update (`WAF_ENDPOINTS.md`)

New individual route row:

| Route | Method | Regex |
|---|---|---|
| Author catalog | GET | `^/authors/OL[0-9]+A(\?.*)?$` |

Updated combined allow-list:
```
^/(search(\?.*)?|books/OL[0-9]+M|authors/OL[0-9]+A(\?.*)?|health|sw\.js)?$
```

---

## 5. README update

New endpoint in the endpoints section and a new curl example:

```bash
# Author catalog
curl -s "http://localhost:8080/authors/OL1A" | python -m json.tool | head -30
```

New error response row:

| Exception | HTTP status | Cause |
|---|---|---|
| `AuthorNotFound` | 404 | Author OLID not found in OpenLibrary |

---

## Testing

- Unit tests mock both `fetch_author_bio` and `_search` to stay offline.
- Cases to cover:
  - Happy path: bio + books returned
  - Bio fetch fails: books-only catalog, no 500
  - 0 books + no bio: 404
  - Invalid OLID format: 400
  - Pagination: `next`/`previous` links present/absent correctly
