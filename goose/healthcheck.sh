#!/bin/sh
# Compose healthcheck for the bundled goose sibling.
#
# Sends an ACP `initialize` to goose serve and expects agentCapabilities back,
# then closes the ACP connection it opened (DELETE /acp) so a probe every 30 s
# does not leave connections behind.
#
# The secret comes from GOOSE_SERVER__SECRET_KEY, or — when that was left
# empty and the entrypoint generated one — from the goose-data volume.
set -u

SECRET_FILE="/root/.config/goose/server-secret.txt"
URL="http://localhost:${GOOSE_SERVE_PORT:-3284}/acp"

KEY="${GOOSE_SERVER__SECRET_KEY:-}"
if [ -z "$KEY" ] && [ -s "$SECRET_FILE" ]; then
    KEY=$(tr -d '[:space:]' < "$SECRET_FILE")
fi

HEADERS=$(mktemp)
BODY=$(mktemp)
trap 'rm -f "$HEADERS" "$BODY"' EXIT

curl -fsS -m 8 -X POST "$URL" \
    -D "$HEADERS" -o "$BODY" \
    -H 'Content-Type: application/json' \
    -H "X-Secret-Key: $KEY" \
    -H "Acp-Connection-Id: $(cat /proc/sys/kernel/random/uuid)" \
    -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":1},"id":1}' \
    || exit 1

grep -q agentCapabilities "$BODY" || exit 1

# goose returns its own canonical connection id; close that one.
CONN_ID=$(grep -i '^acp-connection-id:' "$HEADERS" | head -n 1 | cut -d: -f2- | tr -d ' \r')
if [ -n "$CONN_ID" ]; then
    curl -sS -m 5 -o /dev/null -X DELETE "$URL" \
        -H "X-Secret-Key: $KEY" \
        -H "Acp-Connection-Id: $CONN_ID" || true
fi

exit 0
