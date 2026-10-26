#!/bin/sh
# Runs once at container start, before the pi sidecar.
#
# ─────────────────────────────────────────────────────────────────────────────
# First-start configuration (mirrors the goose sibling entrypoint):
#
#   PI_SIDECAR_SECRET_KEY → REQUIRED by clients as X-Secret-Key. If unset on
#     first start, the sidecar's index.ts generates one, prints it once and
#     writes it to $PI_DATA_DIR/server-secret.txt so it can be recovered with:
#       docker exec container-pi cat /data/server-secret.txt
#     Set the same value as PI_SECRET_KEY in .env for the A2A gateway. This
#     script only echoes state — the Node process in index.ts owns generation.
#
#   PI_SIDE_MODEL (provider/modelId), PI_SIDE_PROVIDER, PI_SIDE_MODEL_ID, PI_SIDE_THINKING,
#   PI_SIDE_TOOLS, PI_SIDE_EXTRA_ARGS → applied to every spawned `pi --mode rpc`
#   session by the sidecar. Provider credentials are plain environment
#   variables read by pi itself (ANTHROPIC_API_KEY, OPENAI_API_KEY,
#   GEMINI_API_KEY, OPENROUTER_API_KEY, ... — see pi docs providers.md).
#
#   Custom OpenAI-compatible endpoints: pi reads <agent-dir>/models.json
#   (pi docs models.md "Configure a compatible endpoint"). If
#   PI_COMPAT_BASE_URL is set on first start (no models.json yet), a minimal
#   entry targeting ${PI_COMPAT_BASE_URL} with model id ${PI_SIDE_MODEL_ID} is
#   written into /root/.pi/agent/models.json so `--provider compat`/
#   PI_SIDE_MODEL="compat/<id>" resolves; apiKey uses $PI_COMPAT_API_KEY
#   environment interpolation.
# ============================================================================
set -e

AGENT_DIR="${PI_AGENT_DIR:-/root/.pi/agent}"
MODELS_JSON="$AGENT_DIR/models.json"
BUNDLED_EXTENSIONS_SRC="/opt/pi-extensions"
BUNDLED_EXTENSIONS_DIR="$AGENT_DIR/extensions"
mkdir -p "$AGENT_DIR" /data/sessions /workspace "$BUNDLED_EXTENSIONS_DIR"

# ── Bundled pi extensions (no-clobber: an operator file wins) ────────────────
# (tool-approval.js — gated tool calls open per-call dialogs, answered by the
#  A2A bridge per pi_permission_mode; see extensions/tool-approval.js.)
if [ -d "$BUNDLED_EXTENSIONS_SRC" ]; then
    for ext in "$BUNDLED_EXTENSIONS_SRC"/*.js; do
        [ -e "$ext" ] || continue
        target="$BUNDLED_EXTENSIONS_DIR/$(basename "$ext")"
        if [ -e "$target" ]; then
            echo "=== pi sidecar: $target already exists — keeping operator copy"
        else
            cp "$ext" "$target"
            echo "=== pi sidecar: installed $(basename "$ext")"
        fi
    done
fi

# ── Optional first-start compatible-endpoint config ──────────────────────────
# (models.json — pi docs models.md; skipped when the operator provided their
#  own file, e.g. via a bind mount or a previous start.)
if [ -n "${PI_COMPAT_BASE_URL:-}" ] && [ ! -f "$MODELS_JSON" ]; then
    MODEL_ID="${PI_SIDE_MODEL_ID:-${PI_COMPAT_MODEL:-local-model}}"
    # '$<NAME>' is pi's env interpolation in models.json; we bake PI_COMPAT_API_KEY
    # as an interpolation so the literal secret never lands in the model file.
    {
        echo '{'
        echo '  "providers": {'
        echo '    "compat": {'
        echo "      \"baseUrl\": \"${PI_COMPAT_BASE_URL}\","
        echo '      "api": "openai-completions",'
        echo '      "apiKey": "${PI_COMPAT_API_KEY}",'
        echo '      "models": ['
        echo "        { \"id\": \"${MODEL_ID}\" }"
        echo '      ]'
        echo '    }'
        echo '  }'
        echo '}'
    } > "$MODELS_JSON"
    echo "=== pi sidecar: wrote $MODELS_JSON"
    echo "    compat endpoint -> ${PI_COMPAT_BASE_URL} (model ${MODEL_ID})"
    echo "    use PI_SIDE_MODEL=compat/${MODEL_ID} (or PI_SIDE_PROVIDER=compat PI_SIDE_MODEL_ID=${MODEL_ID})"
elif [ -f "$MODELS_JSON" ]; then
    echo "=== pi config already exists — skipping first-start models.json"
fi

# Headless container sanity: the sidecar runs pi's tools with the container's
# own permissions (pi has no built-in approval prompts — the container IS the
# security boundary; see docs/rpc-extension-ui.md). --no-approve is applied by
# the sidecar unless PI_SIDE_APPROVE=1.
exec node /app/dist/index.js
