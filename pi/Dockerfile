# =============================================================================
# docker-a2a-gateway — bundled agent sibling image (compose profiles: pi, omp)
# =============================================================================
# Runs the agent sidecar (HTTP + SSE at / with X-Secret-Key auth), a small Node
# service that spawns `pi --mode rpc` (or `omp --mode rpc`) child processes
# over stdin/stdout JSONL — the agent RPC protocol. The A2A gateway bridges
# tasks to it via a2a_protocol_plugin handlers/pi_handler.py ->
# handlers/pi_client.py.
#
# One image, two dialects, selected by the AGENT_DIALECT build arg:
#   AGENT_DIALECT=pi   the pi coding-agent CLI, installed from npm and PINNED
#                      via the PI_VERSION build arg (unit tests replay the RPC
#                      shapes; re-run the live suites when the pinned version
#                      changes).
#   AGENT_DIALECT=omp  the standalone oh-my-pi binary from its GitHub release,
#                      PINNED via the OMP_VERSION build arg and verified
#                      against that release's SHA256SUMS.txt.
# The agent itself has NO network server; this container IS the boundary. The
# sidecar TypeScript is built in-image (npm ci + tsc); the runtime needs only
# node + dist (no runtime npm deps, stdlib-only server).
#
# Credentials come from .env at runtime (never baked in). The agents read
# standard provider env vars (ANTHROPIC_API_KEY, OPENAI_API_KEY,
# GEMINI_API_KEY, OPENROUTER_API_KEY, ...). Model selection rides
# PI_SIDE_MODEL / PI_SIDE_THINKING (omp: OMP_SIDE_MODEL / OMP_SIDE_THINKING),
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
#   omp-agent:/root/.omp/agent          — omp user config (models.yml for
#                                         custom endpoints)
#   ./www/pi-workspace -> /workspace    — the agent's scoped working dir
# =============================================================================

FROM node:24-bookworm-slim

ARG AGENT_DIALECT=pi
ARG PI_VERSION=1.4.0
# pi's npm distribution name + pinned version (the RPC protocol frames used by
# the sidecar and the handler tests are documented for this version).
ARG OMP_VERSION=18.18.2
ENV PI_VERSION=${PI_VERSION} OMP_VERSION=${OMP_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        && rm -rf /var/lib/apt/lists/*

# The agent binary, per dialect:
#   pi  — the pinned npm distribution (provides the `pi` binary the sidecar
#         spawns in RPC mode).
#   omp — the standalone binary from the oh-my-pi GitHub release, verified
#         against that release's SHA256SUMS.txt (a mismatch fails the build).
RUN set -eux; \
    if [ "$AGENT_DIALECT" = "omp" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends libstdc++6; \
        rm -rf /var/lib/apt/lists/*; \
        ARCH="${TARGETARCH:-$(dpkg --print-architecture)}"; \
        case "$ARCH" in \
            amd64) ASSET="omp-linux-x64" ;; \
            arm64) ASSET="omp-linux-arm64" ;; \
            *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;; \
        esac; \
        BASE="https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}"; \
        curl -fsSL -o /tmp/SHA256SUMS.txt "${BASE}/SHA256SUMS.txt"; \
        curl -fsSL -o "/tmp/${ASSET}" "${BASE}/${ASSET}"; \
        cd /tmp; \
        grep " ${ASSET}\$" SHA256SUMS.txt | sha256sum -c -; \
        install -m 0755 "/tmp/${ASSET}" /usr/local/bin/omp; \
        rm -f "/tmp/${ASSET}" /tmp/SHA256SUMS.txt; \
        omp --version; \
    else \
        npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
        && node -e "const v=require('child_process').execSync('pi --version').toString().trim(); \
            console.log('pi', v)" \
        && npm cache clean --force; \
    fi

# ── Sidecar app ──────────────────────────────────────────────────────────────
WORKDIR /app

# First-start provider auto-config writes here; sessions/secret live on the
# pi-data volume.
RUN mkdir -p /app /root/.pi/agent /root/.omp/agent /data/sessions /workspace /var/log

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
    OMP_PORT=3310 \
    OMP_DATA_DIR=/data \
    OMP_SESSION_DIR=/data/sessions \
    OMP_CWD=/workspace \
    NODE_ENV=production

EXPOSE 3300 3310

WORKDIR /workspace

# The entrypoint is the ONLY place that decides how the container starts —
# do not add a `command:` override in docker-compose.yml.
ENTRYPOINT ["/usr/local/bin/pi-entrypoint.sh"]