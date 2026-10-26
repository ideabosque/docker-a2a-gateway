# =============================================================================
# docker-a2a-gateway — bundled pi sibling image (compose profile: pi)
# =============================================================================
# Runs the pi sidecar (HTTP + SSE at / with X-Secret-Key auth), a small Node
# service that spawns `pi --mode rpc` child processes over stdin/stdout JSONL
# — the official pi RPC protocol (see pi/docs/rpc.md et al). The A2A gateway
# bridges tasks to it via a2a_protocol_plugin handlers/pi_handler.py ->
# handlers/pi_client.py.
#
# pi itself has NO network server; this container IS the boundary:
#   - pi (the coding-agent CLI) is installed from npm, PINNED via the
#     PI_VERSION build arg (unit tests replay the RPC shapes; re-run the live
#     suites when the pinned version changes).
#   - The sidecar TypeScript is built in-image (npm ci + tsc); runtime needs
#     only node + dist (no runtime npm deps, stdlib-only server).
#
# Credentials come from .env at runtime (never baked in). pi reads standard
# provider env vars (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY,
# OPENROUTER_API_KEY, ... — see pi docs providers.md). Model selection rides
# PI_MODEL (provider/modelId) / PI_PROVIDER / PI_MODEL_ID / PI_THINKING,
# applied per spawned session by the sidecar (and overridable per call via
# POST /sessions).
#
# Volumes (see docker-compose.yml):
#   pi-data:/data                       — server-secret.txt, session files
#                                         (--session-dir /data/sessions) and
#                                         sessions-index.json (resume after
#                                         sidecar restarts)
#   pi-agent:/root/.pi/agent            — pi user config (settings.json,
#                                         models.json for custom endpoints)
#   ./www/pi-workspace -> /workspace    — the agent's scoped working dir
# =============================================================================

FROM node:24-bookworm-slim

ARG PI_VERSION=1.4.0
# pi's npm distribution name + pinned version (the RPC protocol frames used by
# the sidecar and the handler tests are documented for this version).
ENV PI_VERSION=${PI_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        && rm -rf /var/lib/apt/lists/*

# Install the pinned pi CLI globally (provides the `pi` binary the sidecar
# spawns in RPC mode).
RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && node -e "const v=require('child_process').execSync('pi --version').toString().trim(); \
        console.log('pi', v)" \
    && npm cache clean --force

# ── Sidecar app ──────────────────────────────────────────────────────────────
WORKDIR /app

# First-start provider auto-config writes here; sessions/secret live on the
# pi-data volume.
RUN mkdir -p /app /root/.pi/agent /data/sessions /workspace /var/log

COPY package.json package-lock.json tsconfig.json ./
COPY src ./src
COPY test ./test

# Bundled pi extensions (tool-approval.js) — installed at RUNTIME into the
# agent dir by the entrypoint (see below): /root/.pi/agent is a volume, and a
# build-time copy would be shadowed by it.
COPY extensions /opt/pi-extensions

# Build the sidecar. devDependencies (typescript, vitest) are installed only
# for the build and pruned afterwards — the runtime server is stdlib-only.
# npm ci deterministically installs from the committed package-lock.json.
RUN npm ci --include=dev --no-audit --no-fund \
    && npm run build \
    && npm prune --omit=dev \
    && rm -rf test src

COPY docker-entrypoint.sh /usr/local/bin/pi-entrypoint.sh
# Strip CR: a Windows checkout (core.autocrlf) gives CRLF, and a CRLF shebang
# fails with 'exec ... no such file or directory'.
RUN sed -i 's/\r$//' /usr/local/bin/pi-entrypoint.sh && chmod +x /usr/local/bin/pi-entrypoint.sh

COPY healthcheck.sh /usr/local/bin/pi-healthcheck.sh
RUN sed -i 's/\r$//' /usr/local/bin/pi-healthcheck.sh && chmod +x /usr/local/bin/pi-healthcheck.sh

ENV PI_PORT=3300 \
    PI_DATA_DIR=/data \
    PI_SESSION_DIR=/data/sessions \
    PI_CWD=/workspace \
    NODE_ENV=production

EXPOSE 3300

WORKDIR /workspace

# The entrypoint is the ONLY place that decides how the container starts —
# do not add a `command:` override in docker-compose.yml.
ENTRYPOINT ["/usr/local/bin/pi-entrypoint.sh"]