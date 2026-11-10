#!/bin/sh
# Runs once at container start, before the agent sidecar.
#
# The container serves one dialect, selected by AGENT_DIALECT (pi | omp):
#   pi  — the bundled pi sibling: seeds /root/.pi/agent/models.json and
#         installs the bundled tool-approval extension.
#   omp — the bundled omp sibling: seeds /root/.omp/agent/models.yml. omp
#         gates its tools natively (--approval-mode), so the pi extension is
#         NOT installed for it.
#
# ─────────────────────────────────────────────────────────────────────────────
# First-start configuration (mirrors the goose sibling entrypoint):
#
#   <P>_SIDECAR_SECRET_KEY → REQUIRED by clients as X-Secret-Key. If unset on
#     first start, the sidecar's index.ts generates one, prints it once and
#     writes it to $<P>_DATA_DIR/server-secret.txt so it can be recovered with:
#       docker exec container-pi cat /data/server-secret.txt
#     Set the same value as PI_SECRET_KEY / OMP_SECRET_KEY in .env for the A2A
#     gateway. This script only echoes state — the Node process in index.ts
#     owns generation.
#
#   <P>_SIDE_MODEL (pi: provider/modelId, omp: the whole spec), <P>_SIDE_THINKING,
#   <P>_SIDE_TOOLS, <P>_SIDE_EXTRA_ARGS → applied to every spawned session by
#   the sidecar. Provider credentials are plain environment variables read by
#   the agent itself (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY,
#   OPENROUTER_API_KEY, ...).
#
#   Custom OpenAI-compatible endpoints: pi reads <agent-dir>/models.json
#   (pi docs models.md "Configure a compatible endpoint"), omp reads
#   <agent-dir>/models.yml (omp docs models.md). If PI_COMPAT_BASE_URL /
#   OMP_COMPAT_BASE_URL is set on first start (no model file yet), a minimal
#   entry targeting that endpoint is written so `--provider compat` /
#   `compat/<id>` resolves. The API key never lands in the file: pi
#   interpolates "${PI_COMPAT_API_KEY}", omp reads the environment variable
#   NAMED in `apiKey` (omp docs models.md — the value is an env var name
#   first, the literal string only as fallback).
# ============================================================================
set -e

AGENT_DIALECT="${AGENT_DIALECT:-pi}"
BUNDLED_EXTENSIONS_SRC="/opt/pi-extensions"

case "$AGENT_DIALECT" in
pi)
    AGENT_DIR="${PI_AGENT_DIR:-/root/.pi/agent}"
    MODELS_FILE="$AGENT_DIR/models.json"
    BUNDLED_EXTENSIONS_DIR="$AGENT_DIR/extensions"
    mkdir -p "$AGENT_DIR" /data/sessions /workspace "$BUNDLED_EXTENSIONS_DIR"

    # ── Bundled pi extensions (no-clobber: an operator file wins) ────────────
    # (tool-approval.js — gated tool calls open per-call dialogs, answered by
    # the A2A bridge per pi_permission_mode; see extensions/tool-approval.js.)
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

    # ── Optional first-start compatible-endpoint config ──────────────────────
    # (models.json — pi docs models.md; skipped when the operator provided
    #  their own file, e.g. via a bind mount or a previous start.)
    if [ -n "${PI_COMPAT_BASE_URL:-}" ] && [ ! -f "$MODELS_FILE" ]; then
        MODEL_ID="${PI_SIDE_MODEL_ID:-${PI_COMPAT_MODEL:-local-model}}"
        # '$<NAME>' is pi's env interpolation in models.json; we bake
        # PI_COMPAT_API_KEY as an interpolation so the literal secret never
        # lands in the model file.
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
        } > "$MODELS_FILE"
        echo "=== pi sidecar: wrote $MODELS_FILE"
        echo "    compat endpoint -> ${PI_COMPAT_BASE_URL} (model ${MODEL_ID})"
        echo "    use PI_SIDE_MODEL=compat/${MODEL_ID} (or PI_SIDE_PROVIDER=compat PI_SIDE_MODEL_ID=${MODEL_ID})"
    elif [ -f "$MODELS_FILE" ]; then
        echo "=== pi config already exists — skipping first-start models.json"
    fi
    ;;
omp)
    AGENT_DIR="${OMP_AGENT_DIR:-/root/.omp/agent}"
    MODELS_FILE="$AGENT_DIR/models.yml"
    mkdir -p "$AGENT_DIR" /data/sessions /workspace

    # omp gates its tools natively (--approval-mode always-ask) — the bundled
    # pi tool-approval extension is a pi extension and is never installed here.

    # ── Optional first-start compatible-endpoint config ──────────────────────
    # (models.yml — omp docs models.md; skipped when the operator provided
    #  their own file, e.g. via a bind mount or a previous start.)
    if [ -n "${OMP_COMPAT_BASE_URL:-}" ] && [ ! -f "$MODELS_FILE" ]; then
        MODEL_ID="${OMP_COMPAT_MODEL:-${OMP_SIDE_MODEL_ID:-local-model}}"
        # omp resolves `apiKey` as the NAME of an environment variable first
        # (omp docs models.md) — the secret stays in the environment.
        {
            echo 'providers:'
            echo '  compat:'
            echo "    baseUrl: ${OMP_COMPAT_BASE_URL}"
            echo '    api: openai-completions'
            echo '    apiKey: OMP_COMPAT_API_KEY'
            echo '    models:'
            echo "      - id: ${MODEL_ID}"
            echo '        reasoning: true'
            echo '        input: [text]'
        } > "$MODELS_FILE"
        echo "=== omp sidecar: wrote $MODELS_FILE"
        echo "    compat endpoint -> ${OMP_COMPAT_BASE_URL} (model ${MODEL_ID})"
        echo "    use OMP_SIDE_MODEL=compat/${MODEL_ID}"
    elif [ -f "$MODELS_FILE" ]; then
        echo "=== omp config already exists — skipping first-start models.yml"
    fi
    ;;
*)
    echo "=== sidecar: unknown AGENT_DIALECT '$AGENT_DIALECT' (expected pi or omp)"
    exit 1
    ;;
esac

# Headless container sanity: the sidecar runs the agent's tools with the
# container's own permissions (pi has no built-in approval prompts — the
# container IS the security boundary; see docs/rpc-extension-ui.md).
# --no-approve is applied by the sidecar unless PI_SIDE_APPROVE=1.
exec node /app/dist/index.js
