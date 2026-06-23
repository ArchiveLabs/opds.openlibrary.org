.PHONY: install serve test test-e2e lint

# Install dependencies into the active environment
install:
	pip install -r requirements.txt

# Start the service locally against real openlibrary.org.
# Cache is disabled so every request exercises the full fetch path.
# Use port 8090 to avoid conflicting with OL Docker on 8080.
serve:
	CACHE_ENABLED=false OL_BASE_URL=https://openlibrary.org \
	  uvicorn app.main:app --host 127.0.0.1 --port 8090 --reload

# Offline unit tests — no live service required
test:
	pytest tests/ -m "not e2e" -v

# End-to-end tests — requires a running service.
# Starts a local instance, runs e2e tests against it, stops the instance.
# Override BASE_URL to test a remote instance instead:
#   make test-e2e BASE_URL=https://opds.openlibrary.org
BASE_URL ?= http://127.0.0.1:8090

test-e2e:
	@echo "Starting local service on $(BASE_URL)..."
	CACHE_ENABLED=false OL_BASE_URL=https://openlibrary.org \
	  uvicorn app.main:app --host 127.0.0.1 --port 8090 &
	@sleep 3
	BASE_URL=$(BASE_URL) pytest tests/test_e2e.py -m e2e -v; \
	  STATUS=$$?; \
	  kill $$(lsof -ti:8090) 2>/dev/null; \
	  exit $$STATUS

lint:
	pre-commit run --all-files
