# =============================================================================
# docker-a2a-gateway — bundled goose sibling image (compose profile: goose)
# =============================================================================
# Runs `goose serve` (ACP — Agent Client Protocol — over HTTP + SSE at /acp)
# so the A2A gateway can bridge tasks to a goose agent
# (a2a_protocol_plugin handlers/goose_handler.py -> POST/GET/DELETE /acp).
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
# Set automatically by BuildKit (amd64 / arm64), but only visible inside the
# stage when declared here — without this line it is empty and every build
# downloads the x86_64 binary.
ARG TARGETARCH

ENV GOOSE_VERSION=${GOOSE_VERSION}

# Base OS: supervisor (process management like the openclaw sibling),
# curl/certs for the release download, python3 for first-start YAML/token
# generation, git for goose's own runtime git operations, bzip2 for tarball
# extraction.
RUN apt-get update && apt-get install -y --no-install-recommends \
        supervisor \
        ca-certificates \
        curl \
        bzip2 \
        git \
        python3 \
        python3-venv \
        python3-pip \
        && rm -rf /var/lib/apt/lists/*

# Install the pinned goose CLI release (static binary from the release
# tarball; TARGETARCH selects x86_64 / aarch64 — matching the release assets
# goose-x86_64-unknown-linux-gnu.tar.{gz,bz2} / goose-aarch64-unknown-linux-gnu).
RUN case "$TARGETARCH" in \
        arm64|aarch64) GOOSE_ARCH="aarch64" ;; \
        *)             GOOSE_ARCH="x86_64" ;; \
    esac \
    && curl -fsSL \
        "https://github.com/aaif-goose/goose/releases/download/v${GOOSE_VERSION}/goose-${GOOSE_ARCH}-unknown-linux-gnu.tar.bz2" \
        -o /tmp/goose.tar.bz2 \
    && tar -xjf /tmp/goose.tar.bz2 -C /tmp \
    && find /tmp -maxdepth 2 -type f -name goose -exec mv {} /usr/local/bin/goose \; \
    && chmod +x /usr/local/bin/goose \
    && rm -rf /tmp/goose.tar.bz2 /tmp/goose* \
    && goose --version

ENV GOOSE_CWD=/workspace \
    GOOSE_DISABLE_KEYRING=1

# Workspace the goose agent operates in (A2A agents' cwd is GOOSE_CWD).
RUN mkdir -p /workspace /var/log/supervisor /root/.config/goose /var/www/logs
WORKDIR /workspace

# supervisord program for `goose serve` (Debian's default include is *.conf)
ADD goose.ini /etc/supervisor/conf.d/goose.conf
RUN chmod 755 /etc/supervisor/conf.d/goose.conf

# Entrypoint: first-start provider auto-config from .env, secret-key
# generation when unset, then exec supervisord (the ONLY place that decides
# how the container starts — do not add a `command:` override in compose).
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Strip CR: a Windows checkout (core.autocrlf) gives CRLF, and a CRLF
# shebang fails with 'exec ... no such file or directory'.
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod +x /usr/local/bin/docker-entrypoint.sh

# Compose healthcheck: ACP initialize probe that also works with a generated
# secret and closes the connection it opens.
COPY healthcheck.sh /usr/local/bin/goose-healthcheck.sh
# Strip CR: a Windows checkout (core.autocrlf) gives CRLF, and a CRLF
# shebang fails with 'exec ... no such file or directory'.
RUN sed -i 's/\r$//' /usr/local/bin/goose-healthcheck.sh && chmod +x /usr/local/bin/goose-healthcheck.sh

EXPOSE 3284

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["supervisord", "-n"]