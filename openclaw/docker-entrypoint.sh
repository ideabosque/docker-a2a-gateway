#!/bin/sh
# Runs once at container start, before openclaw/supervisord.
#
# The repo-root deploy key (see ../docker-compose.yml SSH_HOST_DIR) is
# bind-mounted read-only at /root/.ssh-src so OpenClaw's own git operations
# can share the same key as the main gateway image without it ever being
# baked into this image. It can't be used from there directly: a read-only
# mount can't be chmod'd in place, and OpenSSH refuses key files that are
# group/world-readable — a real risk on a Windows host, where NTFS
# permissions don't map cleanly to POSIX modes through Docker's bind mount.
#
# So: copy it into a normal, writable, container-owned ~/.ssh and fix
# permissions before anything else runs.
#
# ─────────────────────────────────────────────────────────────────────────────
# First-start model/provider auto-configuration
# ─────────────────────────────────────────────────────────────────────────────
# When OPENCLAW_MODEL_PROVIDER is non-empty AND openclaw.json does not yet exist
# (i.e. this is the very first container start with an empty bind-mount), the
# entrypoint runs `openclaw onboard --non-interactive` to configure the model
# provider and default model from .env values — mirroring the .env-driven setup
# that Hermes already enjoys. On subsequent starts the existing openclaw.json is
# left untouched so manual CLI changes survive restarts.
#
# Supported providers (OPENCLAW_MODEL_PROVIDER):
#   openai_compat → OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_API_KEY /
#                   OPENAI_COMPAT_MODEL (generic OpenAI-compatible — shared with Hermes)
#   anthropic  — ANTHROPIC_API_KEY
#   openai     — OPENAI_API_KEY (codex plugin is preinstalled first)
#   openrouter — OPENROUTER_API_KEY (configured as a custom OpenAI-compatible provider)
#   gemini     — GEMINI_API_KEY
#   mistral    — MISTRAL_API_KEY
#   moonshot   — MOONSHOT_API_KEY
#   ollama     — OLLAMA_HOST / OLLAMA_API_KEY
#   custom     — OPENCLAW_CUSTOM_BASE_URL / OPENCLAW_CUSTOM_API_KEY /
#                OPENCLAW_CUSTOM_MODEL_ID / OPENCLAW_CUSTOM_PROVIDER_ID /
#                OPENCLAW_CUSTOM_COMPATIBILITY
#
# After onboarding, if OPENCLAW_MODEL is set it pins the default model via
# `openclaw models set`. The auto-generated gateway auth token is printed to
# stdout so it can be copied into OPENCLAW_API_KEY in .env.
#
# If OPENCLAW_GATEWAY_TOKEN is set, it is used as the gateway auth token instead
# of letting onboarding generate a random one — set OPENCLAW_API_KEY to the same
# value in .env so the A2A gateway can authenticate without a post-start copy.
set -e

# ── SSH key setup ────────────────────────────────────────────────────────────
if [ -d /root/.ssh-src ] && [ "$(ls -A /root/.ssh-src 2>/dev/null)" ]; then
    mkdir -p /root/.ssh
    cp -f /root/.ssh-src/* /root/.ssh/ 2>/dev/null || true
    chmod 700 /root/.ssh
    chmod 600 /root/.ssh/* 2>/dev/null || true
fi

# ── First-start model/provider auto-configuration ────────────────────────────
OPENCLAW_CONFIG_FILE="/root/.openclaw/openclaw.json"
PROVIDER="${OPENCLAW_MODEL_PROVIDER:-}"
MODEL="${OPENCLAW_MODEL:-}"

# Only run onboarding when the user opted in via OPENCLAW_MODEL_PROVIDER AND
# the config file does not yet exist (first start with an empty bind-mount).
if [ -n "$PROVIDER" ] && [ ! -f "$OPENCLAW_CONFIG_FILE" ]; then
    echo "=== OpenClaw first-start auto-configuration ==="
    echo "OPENCLAW_MODEL_PROVIDER=$PROVIDER"

    # Common flags: non-interactive, no daemon (supervisord manages the process),
    # no channels/skills/search/ui/hooks — this is a headless A2A gateway.
    FLAGS="--non-interactive --accept-risk --skip-health --skip-daemon"
    FLAGS="$FLAGS --skip-channels --skip-skills --skip-search --skip-ui --skip-hooks"
    FLAGS="$FLAGS --mode local --gateway-bind lan --gateway-auth token"

    # If the user provided a gateway token, use it instead of a random one.
    if [ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
        FLAGS="$FLAGS --gateway-token $OPENCLAW_GATEWAY_TOKEN"
    fi

    case "$PROVIDER" in
        openai_compat)
            # Generic OpenAI-compatible endpoint (shared with Hermes via
            # OPENAI_COMPAT_* vars). Configured as a OpenClaw custom provider.
            FLAGS="$FLAGS --auth-choice custom-api-key"
            FLAGS="$FLAGS --custom-compatibility openai"
            if [ -n "${OPENAI_COMPAT_BASE_URL:-}" ]; then
                FLAGS="$FLAGS --custom-base-url $OPENAI_COMPAT_BASE_URL"
            fi
            if [ -n "${OPENAI_COMPAT_MODEL:-}" ]; then
                FLAGS="$FLAGS --custom-model-id $OPENAI_COMPAT_MODEL"
            fi
            if [ -n "${OPENAI_COMPAT_API_KEY:-}" ]; then
                FLAGS="$FLAGS --custom-api-key $OPENAI_COMPAT_API_KEY"
            fi
            ;;
        anthropic)
            FLAGS="$FLAGS --auth-choice apiKey"
            if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
                FLAGS="$FLAGS --anthropic-api-key $ANTHROPIC_API_KEY"
            fi
            ;;
        openai)
            # OpenAI provider needs the Codex runtime plugin.
            echo "Preinstalling codex plugin for OpenAI provider..."
            openclaw plugins install codex --accept-capabilities 2>/dev/null || {
                echo "WARNING: codex plugin install failed."
                echo "         OpenAI provider may not work without it."
            }
            FLAGS="$FLAGS --auth-choice openai-api-key"
            ;;
        openrouter)
            # OpenRouter is configured as a custom OpenAI-compatible provider.
            FLAGS="$FLAGS --auth-choice custom-api-key"
            FLAGS="$FLAGS --custom-base-url https://openrouter.ai/api/v1"
            FLAGS="$FLAGS --custom-provider-id openrouter"
            FLAGS="$FLAGS --custom-compatibility openai"
            if [ -n "${OPENROUTER_API_KEY:-}" ]; then
                FLAGS="$FLAGS --custom-api-key $OPENROUTER_API_KEY"
            fi
            if [ -n "$MODEL" ]; then
                FLAGS="$FLAGS --custom-model-id $MODEL"
            fi
            ;;
        gemini)
            FLAGS="$FLAGS --auth-choice gemini-api-key"
            if [ -n "${GEMINI_API_KEY:-}" ]; then
                FLAGS="$FLAGS --gemini-api-key $GEMINI_API_KEY"
            fi
            ;;
        mistral)
            FLAGS="$FLAGS --auth-choice mistral-api-key"
            if [ -n "${MISTRAL_API_KEY:-}" ]; then
                FLAGS="$FLAGS --mistral-api-key $MISTRAL_API_KEY"
            fi
            ;;
        moonshot)
            FLAGS="$FLAGS --auth-choice moonshot-api-key"
            if [ -n "${MOONSHOT_API_KEY:-}" ]; then
                FLAGS="$FLAGS --moonshot-api-key $MOONSHOT_API_KEY"
            fi
            ;;
        ollama)
            FLAGS="$FLAGS --auth-choice ollama"
            FLAGS="$FLAGS --custom-base-url ${OLLAMA_HOST:-http://127.0.0.1:11434}"
            if [ -n "$MODEL" ]; then
                FLAGS="$FLAGS --custom-model-id $MODEL"
            fi
            ;;
        custom)
            FLAGS="$FLAGS --auth-choice custom-api-key"
            if [ -n "${OPENCLAW_CUSTOM_BASE_URL:-}" ]; then
                FLAGS="$FLAGS --custom-base-url $OPENCLAW_CUSTOM_BASE_URL"
            fi
            if [ -n "${OPENCLAW_CUSTOM_MODEL_ID:-}" ]; then
                FLAGS="$FLAGS --custom-model-id $OPENCLAW_CUSTOM_MODEL_ID"
            fi
            if [ -n "${OPENCLAW_CUSTOM_API_KEY:-}" ]; then
                FLAGS="$FLAGS --custom-api-key $OPENCLAW_CUSTOM_API_KEY"
            fi
            if [ -n "${OPENCLAW_CUSTOM_PROVIDER_ID:-}" ]; then
                FLAGS="$FLAGS --custom-provider-id $OPENCLAW_CUSTOM_PROVIDER_ID"
            fi
            if [ -n "${OPENCLAW_CUSTOM_COMPATIBILITY:-}" ]; then
                FLAGS="$FLAGS --custom-compatibility $OPENCLAW_CUSTOM_COMPATIBILITY"
            fi
            ;;
        *)
            echo "WARNING: Unknown OPENCLAW_MODEL_PROVIDER '$PROVIDER'."
            echo "         Skipping auto-config. Configure manually with:"
            echo "         docker exec container-openclaw openclaw onboard"
            FLAGS=""
            ;;
    esac

    if [ -n "$FLAGS" ]; then
        echo "Running: openclaw onboard $FLAGS"
        # shellcheck disable=SC2086
        openclaw onboard $FLAGS || {
            echo "WARNING: openclaw onboard failed. OpenClaw will start with"
            echo "         default config. Configure manually via:"
            echo "         docker exec container-openclaw openclaw onboard"
        }

        # Pin the default model if OPENCLAW_MODEL is set and onboarding succeeded.
        # (Skip for custom/openrouter/ollama — they set the model during onboarding
        # via --custom-model-id.)
        if [ -n "$MODEL" ] && [ -f "$OPENCLAW_CONFIG_FILE" ]; then
            case "$PROVIDER" in
                custom|openrouter|ollama|openai_compat)
                    # Model was already set via --custom-model-id during onboarding.
                    ;;
                *)
                    echo "Setting default model: $MODEL"
                    openclaw models set "$MODEL" || {
                        echo "WARNING: openclaw models set '$MODEL' failed."
                        echo "         Set it manually with:"
                        echo "         docker exec container-openclaw openclaw models set '$MODEL'"
                    }
                    ;;
            esac
        fi

        # Enable the OpenAI-compatible /v1/chat/completions endpoint so the
        # A2A gateway (and external clients) can use it. Disabled by default.
        if [ -f "$OPENCLAW_CONFIG_FILE" ]; then
            echo "Enabling OpenAI-compatible chat completions endpoint..."
            openclaw config set gateway.http.endpoints.chatCompletions.enabled true 2>/dev/null || {
                echo "WARNING: could not enable chatCompletions endpoint."
                echo "         Enable it manually with:"
                echo "         docker exec container-openclaw openclaw config set gateway.http.endpoints.chatCompletions.enabled true"
            }
        fi

        # Print the gateway auth token so the user can copy it into
        # OPENCLAW_API_KEY in .env (unless they pre-set OPENCLAW_GATEWAY_TOKEN).
        if [ -f "$OPENCLAW_CONFIG_FILE" ] && [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
            echo ""
            echo "=== OpenClaw gateway auth token (set as OPENCLAW_API_KEY in .env) ==="
            TOKEN=$(python3 -c "
import json
try:
    with open('$OPENCLAW_CONFIG_FILE') as f:
        cfg = json.load(f)
    print(cfg.get('gateway', {}).get('auth', {}).get('token', ''))
except Exception:
    pass
" 2>/dev/null || true)
            if [ -n "$TOKEN" ]; then
                echo "$TOKEN"
            else
                echo "(could not extract token — read it with:)"
                echo "docker exec container-openclaw cat /root/.openclaw/openclaw.json | \\"
                echo "  python3 -c \"import sys,json;print(json.load(sys.stdin)['gateway']['auth']['token'])\""
            fi
            echo "========================================================================="
            echo ""
        elif [ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
            echo ""
            echo "Gateway auth token set from OPENCLAW_GATEWAY_TOKEN."
            echo "Make sure OPENCLAW_API_KEY in .env matches."
            echo ""
        fi
    fi
elif [ -n "$PROVIDER" ]; then
    echo "=== OpenClaw config already exists — skipping first-start auto-config ==="
fi

exec "$@"