#!/bin/sh
# Compose healthcheck for the bundled pi sibling.
#
# GET /health with the shared secret; the sidecar answers 200 with the pi
# version when up. The secret comes from PI_SIDECAR_SECRET_KEY, or — when
# that was left empty and index.ts generated one — from the pi-data volume.
set -u

SECRET_FILE="${PI_DATA_DIR:-/data}/server-secret.txt"

KEY="${PI_SIDECAR_SECRET_KEY:-}"
if [ -z "$KEY" ] && [ -s "$SECRET_FILE" ]; then
    KEY=$(tr -d '[:space:]' < "$SECRET_FILE")
fi

BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT

curl -fsS -m 10 \
    -H "X-Secret-Key: $KEY" \
    "http://localhost:${PI_PORT:-3300}/health" -o "$BODY" \
    || exit 1

grep -q '"status":"ok"' "$BODY" || exit 1

exit 0