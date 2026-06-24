# Testing OPDS Locally with reader.archive.org

How to run the OPDS service locally (against real openlibrary.org data), expose it via Cloudflare tunnel, and verify it works end-to-end with reader.archive.org. Run this process whenever testing a pyopds2_openlibrary or opds.openlibrary.org change before merging.

---

## TL;DR

```bash
# 1. Run automated smoke tests (starts service, tests, tears down)
make test-e2e

# 2. Test a local pyopds2_openlibrary branch
make test-e2e LIB=~/Projects/pyopds2_openlibrary-<branch-slug>

# 3. Expose to reader.archive.org for manual end-to-end verification
make serve &
make tunnel
# Open: https://reader.archive.org/?opds=https://<slug>.trycloudflare.com
```

`make test-e2e` covers: health check, home feed groups, search results, availability facet regression guard (`numberOfItems` must be absent), pagination, book detail, author detail.

---

## Prerequisites

- Python 3.11+ with `uvicorn`, `httpx`, and service dependencies installed
- `cloudflared` CLI: `brew install cloudflare/cloudflare/cloudflared`
- The repo cloned at `~/Projects/opds.openlibrary.org` (or your worktree path)
- pyopds2_openlibrary checked out locally if testing a library branch

---

## Step 1: Install dependencies

```bash
cd ~/Projects/opds.openlibrary.org
pip install -r requirements.txt
```

To test a local branch of pyopds2_openlibrary instead of the published version, use `PYTHONPATH`:

```bash
# Point at your local checkout — no pip install needed
export PYTHONPATH=~/Projects/pyopds2_openlibrary-<branch-slug>

# Verify the right version loads
python3 -c "import pyopds2_openlibrary; print(pyopds2_openlibrary.__file__)"
```

---

## Step 2: Start the OPDS service

```bash
cd ~/Projects/opds.openlibrary.org

CACHE_ENABLED=false \
OL_BASE_URL=https://openlibrary.org \
uvicorn app.main:app --host 127.0.0.1 --port 8090
```

`CACHE_ENABLED=false` ensures every request hits OL directly — no stale cache responses during testing.

Verify the service is up:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8090/health
# Expected: 200
```

---

## Step 3: Smoke-test the service locally

```bash
# Home feed — should return 3+ groups with publications
curl -s "http://127.0.0.1:8090/" | python3 -c "
import json, sys
d = json.load(sys.stdin)
groups = d.get('groups', [])
print(f'{len(groups)} groups')
for g in groups:
    print(f'  {g[\"metadata\"][\"title\"]}: {len(g.get(\"publications\", []))} pubs')
"

# Search — verify numberOfItems absent from availability facets (regression guard)
curl -s "http://127.0.0.1:8090/search?query=tolkien" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'{d[\"metadata\"][\"numberOfItems\"]} results')
for f in d.get('facets', []):
    for link in f.get('links', []):
        ni = link.get('properties', {}).get('numberOfItems')
        if 'vailability' in f.get('metadata', {}).get('title', ''):
            print(f'  avail/{link[\"title\"]}: numberOfItems={ni}')
            assert ni is None, 'availability facet must NOT have numberOfItems'
print('availability facet check: PASS')
"

# Page 2
curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8090/?page=2"
# Expected: 200
```

---

## Step 4: Open a Cloudflare tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:8090
```

Wait for the tunnel URL to appear in the output:

```
Your quick Tunnel has been created! Visit it at:
https://<random-slug>.trycloudflare.com
```

Verify the tunnel is reachable:

```bash
curl -s -o /dev/null -w "%{http_code}" "https://<slug>.trycloudflare.com/health"
# Expected: 200

curl -s "https://<slug>.trycloudflare.com/" | python3 -c "
import json, sys; d = json.load(sys.stdin)
print(f'{len(d.get(\"groups\", []))} groups')
"
# Expected: 3 groups (or more)
```

---

## Step 5: Test with reader.archive.org

Open in a browser:

```
https://reader.archive.org/?opds=https://<slug>.trycloudflare.com
```

**What to verify:**

| Check | Pass condition |
|-------|---------------|
| Feed loads | Reader shows shelves / carousels |
| Covers render | Book cover images appear (not broken) |
| Navigation works | Tapping a shelf title shows more books |
| Search works | Entering a query returns results |
| Book opens | Tapping a book opens the detail / read view |
| No JS console errors | Open DevTools → Console; no red errors |

---

## Step 6: Tear down

```bash
# Kill the tunnel (Ctrl+C in that terminal, or:)
pkill cloudflared

# Kill the service
pkill -f "uvicorn app.main"
```

---

## Checklist (per PR)

```markdown
- [ ] Service starts cleanly (`/health` → 200)
- [ ] Home feed returns 3+ groups with publications
- [ ] Search returns results; availability facets have no `numberOfItems`
- [ ] Page 2 returns 200 with groups
- [ ] Cloudflare tunnel reachable externally (`/health` → 200)
- [ ] reader.archive.org loads the feed via tunnel URL
- [ ] Covers visible, navigation works, no JS console errors
- [ ] Unit tests pass: `python3 -m pytest tests/ -q` (skip e2e markers)
```

---

## Common issues

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError: pyopds2_openlibrary` | `PYTHONPATH` not set, or wrong path |
| Port 8090 already in use | `pkill -f "uvicorn app.main"` or change port |
| Tunnel URL not resolving yet | Wait 10–15 s after the URL appears before testing |
| `docker-compose.yml` fails if worktree is not named `opds.openlibrary.org` | Use `uvicorn` directly (this guide); the compose file hard-codes that path |
| reader.archive.org CORS error | Ensure `CORSMiddleware` is in `app/main.py` (added in PR #41) |
| Home feed returns 0 groups | OL is rate-limiting or unreachable; check `/health` and OL status |
