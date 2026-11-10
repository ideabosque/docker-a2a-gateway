#!/bin/sh
# Compose healthcheck for the bundled agent siblings (pi, omp).
#
# GET /health with the shared secret; the sidecar answers 200 with the agent
# version when up. The secret comes from the dialect's SIDECAR_SECRET_KEY
# (OMP_SIDECAR_SECRET_KEY / PI_SIDECAR_SECRET_KEY), or — when that was left
# empty and index.ts generated one — from the data volume.
set -u

case "${AGENT_DIALECT:-pi}" in
omp)
    KEY="${OMP_SIDECAR_SECRET_KEY:-}"
    PORT="${OMP_PORT:-3310}"
    DATA_DIR="${OMP_DATA_DIR:-/data}"
    ;;
*)
    KEY="${PI_SIDECAR_SECRET_KEY:-}"
    PORT="${PI_PORT:-3300}"
    DATA_DIR="${PI_DATA_DIR:-/data}"
    ;;
esac

SECRET_FILE="$DATA_DIR/server-secret.txt"
if [ -z "$KEY" ] && [ -s "$SECRET_FILE" ]; then
    KEY=$(tr -d '[:space:]' < "$SECRET_FILE")
fi

BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT

curl -fsS -m 10 \
    -H "X-Secret-Key: $KEY" \
    "http://localhost:${PORT}/health" -o "$BODY" \
    || exit 1

grep -q '"status":"ok"' "$BODY" || exit 1

exit 0
