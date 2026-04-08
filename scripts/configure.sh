#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ -f "$ENV_FILE" ]]; then
    echo ".env already exists. Edit it directly or delete it to regenerate."
    exit 0
fi

cat > "$ENV_FILE" << 'EOF'
# Set to "development" to use the request base URL instead (e.g. localhost)
ENVIRONMENT=production

OPDS_BASE_URL=https://openlibrary.org/opds
OL_BASE_URL=https://openlibrary.org
OL_USER_AGENT=OPDSBot/1.0 (opds.openlibrary.org; opds@openlibrary.org)
OL_REQUEST_TIMEOUT=30.0

SENTRY_DSN=https://8d8cab445edc9b4e452ba06d0be46dcb@sentry.archive.org/73
SENTRY_TRACES_SAMPLE_RATE=0.1
SENTRY_PROFILE_SESSION_SAMPLE_RATE=0.1

# Number of uvicorn worker processes. Each worker is an independent process
# with its own in-memory cache — no shared state, no thread-safety concerns.
# Recommended: 2-4 for most deployments. Default: 1 (safe for low-memory envs).
WEB_CONCURRENCY=3
EOF

chmod 600 "$ENV_FILE"
echo ".env created at $ENV_FILE"
echo "Review and adjust the values, then run: docker compose up --build"
