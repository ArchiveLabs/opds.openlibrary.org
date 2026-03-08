#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ -f "$ENV_FILE" ]]; then
    echo ".env already exists. Edit it directly or delete it to regenerate."
    exit 0
fi

cat > "$ENV_FILE" << 'EOF'
#OPDS_BASE_URL=https://opds.openlibrary.org
OL_BASE_URL=https://openlibrary.org
OL_USER_AGENT=OPDSBot/1.0 (opds.openlibrary.org; opds@openlibrary.org)
OL_REQUEST_TIMEOUT=30.0
EOF

chmod 600 "$ENV_FILE"
echo ".env created at $ENV_FILE"
echo "Review and adjust the values, then run: docker compose up --build"
