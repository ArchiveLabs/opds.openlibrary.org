# Author Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/authors/{olid}` OPDS 2.0 catalog endpoint that returns an author's bio and paginated books, and link to it from publication author contributors.

**Architecture:** Add `AuthorNotFound` exception and register its 404 handler in `main.py`. Add `fetch_author_bio()` to `pyopds2_openlibrary/__init__.py`. Extend `get_authors()` in the same file to include an `application/opds+json` link per author. Add a new `GET /authors/{olid}` route in `app/routes/opds.py` that uses `asyncio.gather` to fetch bio and books in parallel, then builds a paginated catalog with manually appended pagination links (consistent with the homepage pattern, avoiding `Catalog.add_pagination` which hardcodes `SEARCH_URL`).

**Tech Stack:** FastAPI, pyopds2, httpx, asyncio, pytest

---

### Task 1: Add `AuthorNotFound` exception and register 404 handler

**Files:**
- Modify: `app/exceptions/__init__.py`
- Modify: `app/main.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py` (after the existing imports):

```python
class TestAuthorNotFound:
    def test_exception_message(self):
        from app.exceptions import AuthorNotFound
        exc = AuthorNotFound("OL123A")
        assert str(exc) == "Author not found: OL123A"
        assert exc.author_olid == "OL123A"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/roni/Developer/opds.openlibrary.org
.venv/bin/pytest tests/test_app.py::TestAuthorNotFound::test_exception_message -v
```

Expected: FAIL with `ImportError: cannot import name 'AuthorNotFound'`

- [ ] **Step 3: Add `AuthorNotFound` to `app/exceptions/__init__.py`**

Replace the entire file with:

```python
from __future__ import annotations

__all__ = [
    "OPDSException",
    "EditionNotFound",
    "AuthorNotFound",
    "UpstreamError",
]


class OPDSException(Exception):
    pass


class EditionNotFound(OPDSException):
    def __init__(self, edition_olid: str):
        self.edition_olid = edition_olid
        super().__init__(f"Edition not found: {edition_olid}")


class AuthorNotFound(OPDSException):
    def __init__(self, author_olid: str):
        self.author_olid = author_olid
        super().__init__(f"Author not found: {author_olid}")


class UpstreamError(OPDSException):
    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_app.py::TestAuthorNotFound::test_exception_message -v
```

Expected: PASS

- [ ] **Step 5: Register `AuthorNotFound` handler in `app/main.py`**

Update the imports line at the top of `app/main.py`:

```python
from app.exceptions import AuthorNotFound, EditionNotFound, UpstreamError
```

Add this handler immediately after `handle_edition_not_found`:

```python
@app.exception_handler(AuthorNotFound)
def handle_author_not_found(_: Request, exc: AuthorNotFound) -> JSONResponse:
    logger.warning("404 AuthorNotFound: %s", exc)
    return JSONResponse(status_code=404, content={"detail": str(exc)})
```

- [ ] **Step 6: Run full test suite to confirm no regressions**

```bash
.venv/bin/pytest tests/ -v
```

Expected: all existing tests pass

- [ ] **Step 7: Commit**

```bash
git add app/exceptions/__init__.py app/main.py tests/test_app.py
git commit -m "feat: add AuthorNotFound exception and 404 handler"
```

---

### Task 2: Add `fetch_author_bio()` helper

**Files:**
- Modify: `pyopds2_openlibrary/__init__.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add `MagicMock` to the mock import in `tests/test_app.py`**

Find this line near the top of `tests/test_app.py`:

```python
from unittest.mock import patch
```

Replace with:

```python
from unittest.mock import MagicMock, patch
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_app.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_app.py::TestFetchAuthorBio -v
```

Expected: all 5 fail with `ImportError: cannot import name 'fetch_author_bio'`

- [ ] **Step 4: Implement `fetch_author_bio` in `pyopds2_openlibrary/__init__.py`**

Add this function after `strip_markdown` (around line 385) and before `marc_language_to_iso_639_1`:

```python
def fetch_author_bio(olid: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch author name and bio from the OpenLibrary author API.

    Returns ``(name, bio)`` where bio has been stripped of Markdown/HTML.
    Returns ``(None, None)`` on any failure — never raises.
    """
    try:
        r = _get(f"{OpenLibraryDataProvider.BASE_URL}/authors/{olid}.json")
        data = r.json()
        name: Optional[str] = data.get("name") or data.get("personal_name")
        raw_bio = data.get("bio")
        if isinstance(raw_bio, dict):
            raw_bio = raw_bio.get("value")
        bio: Optional[str] = strip_markdown(raw_bio) if raw_bio else None
        return name, bio
    except Exception:
        return None, None
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_app.py::TestFetchAuthorBio -v
```

Expected: all 5 pass

- [ ] **Step 6: Commit**

```bash
git add pyopds2_openlibrary/__init__.py tests/test_app.py
git commit -m "feat: add fetch_author_bio() helper"
```

---

### Task 3: Add OPDS link to author contributors

**Files:**
- Modify: `pyopds2_openlibrary/__init__.py` (update `get_authors()` inside `metadata()`)
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_app.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_app.py::TestAuthorOpdsLink -v
```

Expected: `test_author_has_opds_link` and `test_opds_link_href_contains_olid` FAIL; `test_name_only_author_has_no_opds_link` PASSES

- [ ] **Step 3: Update `get_authors()` in `pyopds2_openlibrary/__init__.py`**

Find `get_authors()` inside the `metadata()` method (around line 171). Replace it with:

```python
def get_authors() -> Optional[List[Contributor]]:
    if self.author_name and self.author_key:
        opds_base = OpenLibraryDataProvider.OPDS_BASE_URL or OpenLibraryDataProvider.BASE_URL
        return [
            Contributor(
                name=name,
                links=[
                    Link(
                        href=f"{OpenLibraryDataProvider.BASE_URL}/authors/{key}",
                        type="text/html",
                        rel="author",
                    ),
                    Link(
                        href=f"{opds_base}/authors/{key}",
                        type="application/opds+json",
                    ),
                ],
            )
            for name, key in zip(self.author_name, self.author_key)
        ]
    if self.author_name:
        return [Contributor(name=name) for name in self.author_name]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_app.py::TestAuthorOpdsLink -v
```

Expected: all 3 pass

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
.venv/bin/pytest tests/ -v
```

Expected: all existing tests pass

- [ ] **Step 6: Commit**

```bash
git add pyopds2_openlibrary/__init__.py tests/test_app.py
git commit -m "feat: add OPDS catalog link to publication author contributors"
```

---

### Task 4: Add `GET /authors/{olid}` route

**Files:**
- Modify: `app/routes/opds.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_app.py` (after the existing constant definitions near the top):

```python
FETCH_AUTHOR_BIO_PATCH_TARGET = "app.routes.opds.fetch_author_bio"
```

Then add the test class:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_app.py::TestOpdsAuthors -v
```

Expected: all fail — 422 for invalid OLIDs (FastAPI returns 422 for unmatched path patterns), 404 for valid OLIDs (route not yet registered)

- [ ] **Step 3: Update imports in `app/routes/opds.py`**

Change the FastAPI import line:

```python
from fastapi import APIRouter, Path, Query, Request
```

Change the stdlib imports to add `urlencode`:

```python
from urllib.parse import urlencode
```

Change the pyopds2_openlibrary import line:

```python
from pyopds2_openlibrary import OpenLibraryDataProvider, fetch_author_bio
```

Change the exceptions import line:

```python
from app.exceptions import AuthorNotFound, EditionNotFound, UpstreamError
```

- [ ] **Step 4: Add the route to `app/routes/opds.py`**

Append to the end of `app/routes/opds.py`:

```python
@router.get("/authors/{olid}", summary="OPDS 2.0 author catalog")
async def opds_authors(
    request: Request,
    olid: str = Path(..., pattern=r"^OL\d+A$"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=100),
    mode: str = Query(default="everything"),
):
    logger.info("GET /authors/%s page=%s limit=%s mode=%s", olid, page, limit, mode)
    base = _base_url(request)
    provider = get_provider(base)

    (author_name, author_bio), search_response = await asyncio.gather(
        asyncio.to_thread(fetch_author_bio, olid),
        asyncio.to_thread(
            _search, provider,
            query=f"author_key:{olid}",
            limit=limit,
            offset=(page - 1) * limit,
            facets={"mode": mode},
            require_cover=False,
        ),
    )

    if not search_response.records and author_name is None and author_bio is None:
        raise AuthorNotFound(olid)

    def _author_page_href(p: int) -> str:
        params: dict[str, str] = {}
        if p > 1:
            params["page"] = str(p)
        if limit != 25:
            params["limit"] = str(limit)
        if mode != "everything":
            params["mode"] = mode
        return f"{base}/authors/{olid}?{urlencode(params)}" if params else f"{base}/authors/{olid}"

    catalog_links: list[Link] = [
        Link(rel="self", href=_author_page_href(page), type=OPDS_MEDIA_TYPE),
        Link(rel="first", href=_author_page_href(1), type=OPDS_MEDIA_TYPE),
        *_common_links(base),
    ]
    if page > 1:
        catalog_links.append(Link(rel="previous", href=_author_page_href(page - 1), type=OPDS_MEDIA_TYPE))
    if search_response.has_more:
        catalog_links.append(Link(rel="next", href=_author_page_href(page + 1), type=OPDS_MEDIA_TYPE))

    catalog = Catalog.create(
        metadata=Metadata(
            title=author_name or olid,
            description=author_bio,
            numberOfItems=search_response.total,
            itemsPerPage=limit,
            currentPage=page,
        ),
        response=search_response,
        paginate=False,
        links=catalog_links,
    )
    return opds_response(catalog.model_dump())
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_app.py::TestOpdsAuthors -v
```

Expected: all 15 pass

- [ ] **Step 6: Run full test suite to check for regressions**

```bash
.venv/bin/pytest tests/ -v
```

Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add app/routes/opds.py tests/test_app.py
git commit -m "feat: add GET /authors/{olid} OPDS 2.0 author catalog route"
```

---

### Task 5: Update WAF rules and README

**Files:**
- Modify: `WAF_ENDPOINTS.md`
- Modify: `README.md`

- [ ] **Step 1: Update `WAF_ENDPOINTS.md`**

Replace the Individual Routes table:

```markdown
## Individual Routes

| Route | Method | Regex |
|---|---|---|
| Homepage | GET | `^/$` |
| Search | GET | `^/search(\?.*)?$` |
| Single Edition | GET | `^/books/OL[0-9]+M$` |
| Author Catalog | GET | `^/authors/OL[0-9]+A(\?.*)?$` |
| Health Check | GET | `^/health$` |
| Service Worker | GET | `^/sw\.js$` |
```

Replace the Combined Allow-List block:

```
^/(search(\?.*)?|books/OL[0-9]+M|authors/OL[0-9]+A(\?.*)?|health|sw\.js)?$
```

- [ ] **Step 2: Update `README.md` — project structure comment**

Find this line:

```
    opds.py            # All route handlers (/, /search, /books/*)
```

Replace with:

```
    opds.py            # All route handlers (/, /search, /books/*, /authors/*)
```

- [ ] **Step 3: Update `README.md` — curl examples**

Find this block in the "Testing the endpoints" section:

```bash
# Single edition
curl -s http://localhost:8080/books/OL7353617M | python -m json.tool | head -30
```

Add after it:

```bash
# Author catalog (bio + paginated books)
curl -s "http://localhost:8080/authors/OL1A" | python -m json.tool | head -30
```

- [ ] **Step 4: Update `README.md` — error responses table**

Find this line in the Error responses table:

```
| `UpstreamError` | 502 | OpenLibrary returned an error or is unreachable |
```

Add after it:

```
| `AuthorNotFound` | 404 | Author OLID not found in OpenLibrary |
```

- [ ] **Step 5: Commit**

```bash
git add WAF_ENDPOINTS.md README.md
git commit -m "docs: update WAF rules and README for /authors/{olid} route"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `fetch_author_bio()` helper — name, bio, normalization, failure | Task 2 |
| Author OPDS link in `get_authors()` | Task 3 |
| `AuthorNotFound` exception, 404 handler | Task 1 |
| `GET /authors/{olid}` route | Task 4 |
| Parallel fetch with `asyncio.gather` | Task 4, Step 4 |
| Bio fetch failure → 200 with books only | Task 4, `test_bio_fetch_failure_still_returns_200` |
| 0 books + no bio → 404 | Task 4, `test_no_books_and_no_bio_returns_404` |
| Invalid OLID → 422 | Task 4, `test_invalid_olid_*` |
| `self`, `first`, `search`, shelf, profile links | Task 4, `test_self_link_present`, `test_first_link_present` + route code |
| `next`/`previous` pagination links | Task 4, pagination tests |
| Metadata: title, description, numberOfItems | Task 4, `test_metadata_*` |
| WAF regex update | Task 5 |
| README curl example + error table | Task 5 |

All sections covered. ✓

**Placeholder scan:** No TBDs or incomplete steps. All code blocks are complete. ✓

**Type consistency:**
- `fetch_author_bio(olid: str) -> tuple[Optional[str], Optional[str]]` — destructured as `(author_name, author_bio)` in Task 4. ✓
- `AuthorNotFound(olid)` — `olid: str` matches `__init__(self, author_olid: str)`. ✓
- `_search(provider, query=..., limit=..., offset=..., facets=..., require_cover=...)` — matches `_search(provider, **kwargs)` signature in `opds.py`. ✓
- `Catalog.create(metadata=..., response=..., paginate=False, links=...)` — matches `Catalog.create` signature from `pyopds2/models.py:196`. ✓
- `search_response.has_more` and `search_response.total` — defined on `DataProvider.SearchResponse` in `pyopds2/provider.py`. ✓
- `Metadata(title=..., description=..., numberOfItems=..., itemsPerPage=..., currentPage=...)` — `Metadata` has `extra = "allow"` so extra kwargs are stored. ✓
