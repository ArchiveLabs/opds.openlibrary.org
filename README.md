# opds.openlibrary.org

A stand-alone, dockerized [FastAPI](https://fastapi.tiangolo.com/) service that
implements an [OPDS 2.0](https://drafts.opds.io/opds-2.0) feed for
[Open Library](https://openlibrary.org), backed by
[pyopds2\_openlibrary](https://github.com/ArchiveLabs/pyopds2_openlibrary).

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/opds` | Homepage catalog with featured subjects and curated shelves |
| `GET` | `/opds/search` | Search Open Library (query params: `query`, `limit`, `page`, `sort`) |
| `GET` | `/opds/books/{edition_olid}` | Single-edition publication record (e.g. `OL7353617M`) |

Interactive API docs are available at `/docs` (Swagger UI) and `/redoc` once
the server is running.

---

## Running locally (Python)

```bash
# 1. Clone the repo
git clone https://github.com/ArchiveLabs/opds.openlibrary.org.git
cd opds.openlibrary.org

# 2. Create and activate a virtual environment
python -m venv env
source env/bin/activate   # Windows: env\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the server
uvicorn app.main:app --reload --port 8080
```

The service is now available at <http://localhost:8080>.

---

## Running with Docker

### Docker Compose (recommended)

```bash
docker compose up --build
```

### Plain Docker

```bash
docker build -t opds-openlibrary .
docker run -p 8080:8080 opds-openlibrary
```

---

## Testing the endpoints

Once the server is running (locally or via Docker), verify each endpoint:

```bash
# Homepage catalog
curl -s http://localhost:8080/opds | python -m json.tool | head -30

# Search (returns OPDS 2.0 catalog)
curl -s "http://localhost:8080/opds/search?query=Python&limit=5" | python -m json.tool | head -30

# Single edition (replace with a real OL edition ID)
curl -s http://localhost:8080/opds/books/OL7353617M | python -m json.tool | head -30

# 404 for a non-existent edition
curl -s http://localhost:8080/opds/books/OL0000000M
# → {"detail": "Edition not found"}
```

Expected responses:
- `/opds` — JSON with `metadata.title == "Open Library"`, `navigation` array of
  featured subjects, and `groups` array of curated shelves.
- `/opds/search` — JSON with `metadata.title == "Search Results"` and a
  `publications` array.
- `/opds/books/{olid}` — JSON publication record with `metadata`, `links`, and
  optionally `images`.

---

## Running the automated tests

```bash
# Install test dependencies (httpx is required by FastAPI's TestClient)
pip install httpx pytest

# Run the full test suite
pytest -v
```

All tests are offline (network calls to Open Library are mocked).

Expected output:

```
tests/test_app.py::TestOpdsHome::test_returns_200 PASSED
tests/test_app.py::TestOpdsHome::test_content_type PASSED
tests/test_app.py::TestOpdsHome::test_metadata_title PASSED
tests/test_app.py::TestOpdsHome::test_navigation_has_featured_subjects PASSED
tests/test_app.py::TestOpdsHome::test_groups_present PASSED
tests/test_app.py::TestOpdsHome::test_links_include_self_and_search PASSED
tests/test_app.py::TestOpdsSearch::test_returns_200 PASSED
tests/test_app.py::TestOpdsSearch::test_content_type PASSED
tests/test_app.py::TestOpdsSearch::test_metadata_title PASSED
tests/test_app.py::TestOpdsSearch::test_publications_in_response PASSED
tests/test_app.py::TestOpdsSearch::test_pagination_params_forwarded PASSED
tests/test_app.py::TestOpdsSearch::test_invalid_limit_rejected PASSED
tests/test_app.py::TestOpdsSearch::test_invalid_page_rejected PASSED
tests/test_app.py::TestOpdsBooks::test_returns_200_for_known_edition PASSED
tests/test_app.py::TestOpdsBooks::test_content_type PASSED
tests/test_app.py::TestOpdsBooks::test_returns_404_for_unknown_edition PASSED
tests/test_app.py::TestOpdsBooks::test_404_body_has_detail PASSED

17 passed in 0.XX s
```
