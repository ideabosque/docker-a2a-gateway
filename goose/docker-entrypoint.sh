#!/bin/sh
# Runs once at container start, before supervisord/goose serve.
#
# ─────────────────────────────────────────────────────────────────────────────
# First-start provider auto-configuration (mirrors the OpenClaw sibling)
# ─────────────────────────────────────────────────────────────────────────────
# goose reads provider/model settings from config.yaml, env vars, or the CLI.
# For a headless container we drive everything through env vars at RUNTIME:
#
#   GOOSE_MODEL_PROVIDER (first start only, needs a missing config.yaml):
#     openai_compat → GOOSE_PROVIDER=openai + OPENAI_BASE_URL/OPENAI_API_KEY
#                     (the generic OpenAI-compatible endpoint shared with the
#                     Hermes and OpenClaw siblings via OPENAI_COMPAT_* vars)
#     anthropic     → GOOSE_PROVIDER=anthropic + ANTHROPIC_API_KEY
#     openrouter    → GOOSE_PROVIDER=openrouter + OPENROUTER_API_KEY
#     ollama        → GOOSE_PROVIDER=ollama + OLLAMA_HOST (GOOSE_MODEL)
#     gemini        → GOOSE_PROVIDER=gemini + GEMINI_API_KEY
#
#   GOOSE_MODEL            → default model id (goose also reads GOOSE_MODEL
#                            directly at runtime, so this var works every start)
#   GOOSE_MODE             → auto | approve | smart_approve | chat
#                            (server-side tool approval; the A2A bridge can
#                            further gate per session via its
#                            GOOSE_PERMISSION_MODE)
#   GOOSE_SERVER__SECRET_KEY → if unset on first start, a random one is
#                            generated, printed once and ALSO written to
#                            /root/.config/goose/server-secret.txt so it can
#                            be recovered with:
#                              docker exec container-goose cat \
#                                /root/.config/goose/server-secret.txt
#                            Set the same value as GOOSE_SECRET_KEY in .env
#                            for the A2A gateway. The key is stored only in
#                            the goose-data volume (goose config dir).
#
# Builtin extensions: supervisor keeps the entrypoint out of the way; enable
# extras with EXTRA_GOOSE_SERVE_ARGS (e.g.
# `EXTRA_GOOSE_SERVE_ARGS=--with-builtin developer` in .env).
# ============================================================================
set -e

CONFIG_DIR="/root/.config/goose"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
SECRET_FILE="$CONFIG_DIR/server-secret.txt"
LOG_DIR="/var/www/logs"

mkdir -p "$CONFIG_DIR" "$LOG_DIR" /workspace

# ── Server secret ────────────────────────────────────────────────────────────
if [ -z "${GOOSE_SERVER__SECRET_KEY:-}" ]; then
    if [ -s "$SECRET_FILE" ]; then
        GOOSE_SERVER__SECRET_KEY=$(cat "$SECRET_FILE" | tr -d '[:space:]')
        export GOOSE_SERVER__SECRET_KEY
        echo "=== goose server secret recovered from $SECRET_FILE ==="
    else
        GOOSE_SERVER__SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
        export GOOSE_SERVER__SECRET_KEY
        printf '%s\n' "$GOOSE_SERVER__SECRET_KEY" > "$SECRET_FILE"
        chmod 600 "$SECRET_FILE"
        echo ""
        echo "=== goose server secret (first start — copy into .env) ==="
        echo ""
        echo "GOOSE_SERVER__SECRET_KEY=$GOOSE_SERVER__SECRET_KEY"
        echo ""
        echo "...and set GOOSE_SECRET_KEY to the SAME value so the A2A"
        echo "gateway authenticates without any post-start copy."
        echo "================================================================"
        echo ""
    fi
elif [ ! -s "$SECRET_FILE" ]; then
    # Persist the operator-supplied secret for restarts of the same volume.
    printf '%s\n' "$GOOSE_SERVER__SECRET_KEY" > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
fi

# ── First-start provider auto-config ─────────────────────────────────────────
PROVIDER="${GOOSE_MODEL_PROVIDER:-}"
MODEL="${GOOSE_MODEL:-}"

if [ -n "$PROVIDER" ] && [ ! -f "$CONFIG_FILE" ]; then
    echo "=== goose first-start auto-configuration ==="
    echo "GOOSE_MODEL_PROVIDER=$PROVIDER"

    case "$PROVIDER" in
        openai_compat|openai|custom)
            GOOSE_PROVIDER="openai"
            ;;
        anthropic)
            GOOSE_PROVIDER="anthropic"
            ;;
        openrouter)
            GOOSE_PROVIDER="openrouter"
            ;;
        ollama)
            GOOSE_PROVIDER="ollama"
            ;;
        gemini)
            GOOSE_PROVIDER="gemini"
            ;;
        *)
            echo "WARNING: Unknown GOOSE_MODEL_PROVIDER '$PROVIDER'."
            echo "         Supported: openai_compat | anthropic | openrouter | ollama | gemini"
            echo "         Configure manually: docker exec container-goose goose configure"
            GOOSE_PROVIDER=""
            ;;
    esac

    if [ -n "$GOOSE_PROVIDER" ]; then
        # Minimal runtime config. Provider credentials stay in env vars; the
        # keyring is disabled, so goose must not try to touch a keyring daemon.
        {
            echo "GOOSE_PROVIDER: $GOOSE_PROVIDER"
            if [ -n "$MODEL" ]; then
                echo "GOOSE_MODEL: $MODEL"
            fi
            echo "GOOSE_MODE: ${GOOSE_MODE:-auto}"
            echo "GOOSE_DISABLE_KEYRING: true"
        } > "$CONFIG_FILE"
        echo "Wrote $CONFIG_FILE (GOOSE_PROVIDER=$GOOSE_PROVIDER${MODEL:+, GOOSE_MODEL=$MODEL})"
    fi
elif [ -f "$CONFIG_FILE" ]; then
    echo "=== goose config already exists — skipping first-start auto-config ==="
fi

# ── `goose serve` wrapper (used by supervisord) ─────────────────────────────
# Binds 0.0.0.0 for inter-container access, always loads the builtin developer
# extension (the A2A bridge's tool workhorse), plus any EXTRA_GOOSE_SERVE_ARGS
# from .env. Unquoted expansion is intentional (word splitting of extra args).
cat > /usr/local/bin/goose-serve.sh <<'EOF'
#!/bin/sh
set -e
: "${GOOSE_SERVE_HOST:=0.0.0.0}"
: "${GOOSE_SERVE_PORT:=3284}"
exec goose serve --host "$GOOSE_SERVE_HOST" --port "$GOOSE_SERVE_PORT" \
    --with-builtin developer \
    $EXTRA_GOOSE_SERVE_ARGS
EOF
chmod +x /usr/local/bin/goose-serve.sh

exec "$@"