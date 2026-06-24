.PHONY: install serve test test-e2e tunnel lint

# Install dependencies into the active environment
install:
	pip install -r requirements.txt

# Start the service locally against real openlibrary.org.
# Cache is disabled so every request exercises the full fetch path.
# CORS is enabled because there's no fronting nginx locally — browser clients
# (e.g. reader.archive.org via the Cloudflare tunnel) need the headers.
# Use port 8090 to avoid conflicting with OL Docker on 8080.
serve:
	CACHE_ENABLED=false CORS_ENABLED=true OL_BASE_URL=https://openlibrary.org \
	  uvicorn app.main:app --host 127.0.0.1 --port 8090 --reload

# Offline unit tests — no live service required
test:
	pytest tests/ -m "not e2e" -v

# End-to-end tests — starts a local service, runs tests, tears down.
# Override BASE_URL to test a remote instance instead:
#   make test-e2e BASE_URL=https://opds.openlibrary.org
# To test a local pyopds2_openlibrary branch:
#   make test-e2e LIB=~/Projects/pyopds2_openlibrary-<slug>
BASE_URL ?= http://127.0.0.1:8090
LIB ?=
_PYPATH = $(if $(LIB),PYTHONPATH=$(LIB) ,)

test-e2e:
	@echo "Starting local service on $(BASE_URL)..."
	$(_PYPATH)CACHE_ENABLED=false OL_BASE_URL=https://openlibrary.org \
	  uvicorn app.main:app --host 127.0.0.1 --port 8090 &
	@sleep 3
	BASE_URL=$(BASE_URL) pytest tests/test_e2e.py -m e2e -v; \
	  STATUS=$$?; \
	  kill $$(lsof -ti:8090) 2>/dev/null; \
	  exit $$STATUS

# Expose the local service to reader.archive.org via a Cloudflare tunnel.
# Requires: brew install cloudflare/cloudflare/cloudflared
# Usage: run `make serve` first (or `make test-e2e` to start in background),
#        then `make tunnel` in a second terminal.
# Open: https://reader.archive.org/?opds=https://<slug>.trycloudflare.com
tunnel:
	cloudflared tunnel --url http://127.0.0.1:8090

lint:
	pre-commit run --all-files
