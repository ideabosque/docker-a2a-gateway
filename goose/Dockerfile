# =============================================================================
# docker-a2a-gateway — bundled goose sibling image (compose profile: goose)
# =============================================================================
# Runs `goose serve` (ACP over HTTP + WebSocket) so the A2A gateway can bridge
# tasks to a goose agent (handlers/goose_handler.py -> POST/GET /acp).
#
# goose is installed as a static binary from the aaif-goose/goose GitHub
# release (pinned via the GOOSE_VERSION build arg). Provider credentials and
# the server secret come from .env at runtime (never baked in):
#   GOOSE_SERVER__SECRET_KEY  -> required; clients send it as X-Secret-Key
#   GOOSE_MODEL_PROVIDER      -> openai_compat | anthropic | openrouter |
#                                ollama | gemini (first-start auto-config)
#   GOOSE_MODEL               -> default model id
#   GOOSE_MODE                -> auto | approve | smart_approve | chat
#                                (server-side tool approval policy)
#   GOOSE_DISABLE_KEYRING     -> 1 (headless: no keyring daemon)
#
# Volumes (see docker-compose.yml):
#   goose-data:/root/.config/goose       — config.yaml + server state
#   goose-share:/root/.local/share/goose — sessions database
#   ./www/goose-workspace -> /workspace  — the agent's scoped working dir
# =============================================================================

FROM debian:bookworm-slim

ARG GOOSE_VERSION=1.53.0

ENV GOOSE_VERSION=${GOOSE_VERSION}

# Base OS: supervisor (process management like the openclaw sibling),
# curl/certs for the release download, python3 for first-start YAML/token
# generation, git for goose's own runtime git operations.
RUN apt-get update && apt-get install -y --no-install-recommends \
        supervisor \
        ca-certificates \
        curl \
        git \
        python3 \
        && rm -rf /var/lib/apt/lists/*

# Install the pinned goose CLI release. download_cli.sh installs into
# ~/.local/bin; set GOOSE_INSTALL_PATH? The script honours GOOSE_INSTALL_PATH
# when provided — bake a stable location into /usr/local/bin on PATH.
RUN curl -fsSL \
        "https://github.com/aaif-goose/goose/releases/download/v${GOOSE_VERSION}/download_cli.sh" \
        | GOOSE_INSTALL_PATH=/usr/local/bin/goose bash \
    && chmod +x /usr/local/bin/goose \
    && goose --version

ENV GOOSE_CWD=/workspace \
    GOOSE_DISABLE_KEYRING=1

# Workspace the goose agent operates in (A2A agents' cwd is GOOSE_CWD).
RUN mkdir -p /workspace /var/log/supervisor /root/.config/goose /var/www/logs
WORKDIR /workspace

# supervisord program for `goose serve`
ADD goose.ini /etc/supervisor/conf.d/goose.ini
RUN chmod 755 /etc/supervisor/conf.d/goose.ini

# Entrypoint: first-start provider auto-config from .env, secret-key
# generation when unset, then exec supervisord (the ONLY place that decides
# how the container starts — do not add a `command:` override in compose).
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 3284

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["supervisord", "-n"]