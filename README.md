# opds.openlibrary.org

A stand-alone, dockerized [FastAPI](https://fastapi.tiangolo.com/) service that implements an [OPDS 2.0](https://drafts.opds.io/opds-2.0) feed for [Open Library](https://openlibrary.org), backed by [pyopds2\_openlibrary](https://github.com/ArchiveLabs/pyopds2_openlibrary).

---

## Project structure

```
app/
  main.py              # FastAPI app + exception handlers
  author_descriptions.py  # Lookup of an author's one-line Wikidata description
  logger.py            # Central logging factory (stdout only)
  config/
    __init__.py        # Media types, env vars, featured subjects
  exceptions/
    __init__.py        # OPDSException, EditionNotFound, UpstreamError
  routes/
    opds.py            # All route handlers (/, /search, /books/*, /authors/*)
  data/
    author_descriptions.tsv.gz  # Every OL author's Wikidata description (built, committed)
docker/
  Dockerfile           # Docker image definition
  docker-compose.yml   # Compose service definition
scripts/
  configure.sh         # Generates .env from defaults
  build_author_descriptions.py  # Rebuilds app/data/author_descriptions.tsv.gz
```

---

## Endpoints

Interactive docs: `/docs` (Swagger UI) · `/redoc`

| Route | Parameters |
|---|---|
| `GET /` | `mode`, `language`, `page`, `media_type`, `access`, `limit` |
| `GET /search` | `query`, `limit`, `page`, `sort`, `mode`, `title`, `language`, `media_type`, `access` |
| `GET /authors/{olid}` | `mode`, `language`, `media_type`, `access`: the page about an author |
| `GET /authors/{olid}/books` | `page`, `limit`, `mode`, `language`, `media_type`, `access`, `sort`: all their books |
| `GET /works/{olid}` | `language`, `access`: the work, its editions as `rel=alternate` publications, best offer first |
| `GET /books/{olid}` | none: one edition, the document a work's alternate points at when the Archive does not hold it |
| `GET /rel/{relation}` | none: the HTML page a link relation the feed coins resolves to |

`language` is a comma-separated list of ISO 639-1 codes, the reader's
preferred language first: `language=en,fr` keeps works in either language
and prefers English editions. A code Open Library does not know is ignored;
with none left, no language filter applies. Every link the feed emits carries
the list as given, and the `rel="search"` template is
`/search{?query,language}` so a client can fill its languages in.

### Works and editions

A publication in a feed is a **work**: its own title, authors, description
and rating, and under `rel="alternate"` with type
`application/opds-publication+json` one link per **edition**, best offer
first — free, then a loan that can be taken now, then one whose state is
unknown, then one checked out, then for sale cheapest first. An edition the
Internet Archive holds links straight to the Archive's document; any other
links to `/books/{olid}` here. Each of those links carries a hint of what it
points at under `properties.edition` (see `/rel`), so a client lists the
editions without fetching any of them; the document behind the link is the
truth. A feed lists the one edition Open Library surfaced; `GET /works/{olid}`
composes the full list, one Open Library call per language and access tier,
and its `self` echoes `language` because the list depends on it.

### Link relations

Every link says what kind of link it is through its `rel`, one relation or
a list of them, so a client selects by relation rather than by title. Every
option link of a facet group carries the group's relation, the applied
option as `["self", <relation>]`:

| Facet group | Relation on each option link |
|---|---|
| Availability | `https://openlibrary.org/opds/rel/facet/availability` |
| Language | `https://openlibrary.org/opds/rel/facet/language` |
| Media Type | `https://openlibrary.org/opds/rel/facet/media-type` |
| Access | `https://openlibrary.org/opds/rel/facet/access` |

The Trending Books carousel's `self` link, and the Popular Books
carousel's on an author page, also carry the registered
`http://opds-spec.org/sort/popular`. An author page, `/authors/{olid}`, is
the page about the author, not the list of their books: it links to the
author's openlibrary.org page as `alternate`, carries the author's photo as
a feed-level `images` entry with `https://openlibrary.org/opds/rel/portrait`,
two texts about the author (`metadata.subtitle`, the one-line summary shown
under the portrait on openlibrary.org, the author's Wikidata description,
looked up in a table shipped with the app, see
[Author descriptions](#author-descriptions), never fetched per page; and
`metadata.description`, the full biography from the Open Library author
record), the Popular Books carousel, and one `navigation` entry, "All X's
books", with the registered `subsection` relation, leading to
`/authors/{olid}/books`: the paginated, faceted, sortable feed of all the
author's books, whose `up` link leads back to the page. Every book's author
contributor links to both the HTML page and the author page with `rel`
`author`. The feed's own relations are extension
relations (RFC 8288): fixed strings, the same on every deployment, that
resolve to [app/docs/link-relations.md](app/docs/link-relations.md), served
at `/rel/…`. Check a `rel` for membership, not equality: `has_rel` in
pyopds2 reads a string or a list.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) + [Docker Compose](https://docs.docker.com/compose/) — for the recommended Docker workflow
- Python 3.12+ — only needed for local development without Docker

Alternatively [Podman](https://podman.io/) can be used. In that case,
aardvark-dns is required.

---

## Quick start (Docker — recommended)

### 1. Clone the repo

```bash
git clone https://github.com/ArchiveLabs/opds.openlibrary.org.git
cd opds.openlibrary.org
```

### 2. Configure environment

```bash
./scripts/configure.sh
```

This creates a `.env` file with sensible defaults. Edit it to override any values before starting the service.

### 3. Build and start

```bash
docker compose up --build
```

The service is available at <http://localhost:8080>.

To run in the background:

```bash
docker compose up --build -d
```

### 4. Stop the service

```bash
docker compose down
```

---

## Environment variables

Managed via `.env` (generated by `configure.sh`). All are optional.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPDS_BASE_URL` | *(falls back to request base URL)* | Public URL of this OPDS service, used in self-referencing links. When unset, the base URL is inferred from each incoming request (suitable for local dev). Set this when running behind a reverse proxy. |
| `OL_BASE_URL` | `https://openlibrary.org` | OpenLibrary backend URL for API calls and alternate links. |
| `OL_USER_AGENT` | `OPDSBot/1.0 (opds.openlibrary.org; opds@openlibrary.org)` | User-Agent sent with every request to OpenLibrary. |
| `OL_REQUEST_TIMEOUT` | `30.0` | Timeout in seconds for requests to the OpenLibrary API. |

---

## Running locally (without Docker)

```bash
# 1. Create and activate a virtual environment
python -m venv env
source env/bin/activate   # Windows: env\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the server (cache disabled so every request hits the real fetch path)
make serve
# or: CACHE_ENABLED=false uvicorn app.main:app --host 127.0.0.1 --port 8090 --reload
```

> **Note on docker-compose:** `docker-compose.yml` hard-codes the build context path, so
> it only works when the repo is checked out as `opds.openlibrary.org` exactly. For
> worktrees or forks with different directory names, use `make serve` instead.

---

## Running tests

### Offline unit tests (no service required)

```bash
make test
# or: pytest tests/ -m "not e2e" -v
```

All unit tests mock network calls and run without a live service or Docker.

### End-to-end tests (live service required)

E2e tests hit a running instance and verify real response structure, including
behavioural invariants that protect against performance regressions — for
example, that availability facet links do not carry `numberOfItems` (which
would mean expensive per-mode Solr queries have been re-introduced).

```bash
# Start a local instance and run e2e tests in one command:
make test-e2e

# Or test against any running instance:
make test-e2e BASE_URL=https://openlibrary.org/opds

# Or run the service and tests separately:
make serve                              # terminal 1
pytest tests/test_e2e.py -m e2e -v    # terminal 2
```

E2e tests are skipped by default (`pytest` without `-m e2e`) to keep CI fast.
Run them manually before opening a PR or after deploying to any environment.

---

## Author descriptions

The one-line summary on an author page, the feed's `metadata.subtitle`, is
the English description of the author's Wikidata item, which no Open
Library API carries. Rather than ask
wikidata.org on every author page, the service ships the whole table:
`app/data/author_descriptions.tsv.gz`, one `OLID<TAB>description` line per
author, about 245,000 rows and 3 MB gzipped, read into memory once per
worker (about 15 MB). An author page never makes a request for it.

The table goes stale as Wikidata gains links and descriptions, so rebuild
it now and then and commit the result:

```bash
make author-descriptions
# or: python3 scripts/build_author_descriptions.py
```

It runs one SPARQL query against QLever's Wikidata mirror (about 15
seconds; the public query.wikidata.org endpoint times out halfway) and
writes a reproducible file, so an unchanged table gives no diff. An author
linked on Wikidata after the last build has no description until the next
one.

---

## Error responses

| Exception | HTTP status | Cause |
|-----------|-------------|-------|
| `EditionNotFound` | 404 | Edition OLID not found in OpenLibrary |
| `UpstreamError` | 502 | OpenLibrary returned an error or is unreachable |
| `AuthorNotFound` | 404 | Author OLID not found in OpenLibrary |

All errors are logged to stdout by the service logger.
