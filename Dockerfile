# =============================================================================
# docker-a2a-gateway — image
# =============================================================================
# A slim Python 3.12 image that runs the SilvaEngine Gateway with ONLY the
# a2a_daemon_engine module registered, exposing the native A2A protocol
# surface (JSON-RPC, GraphQL, SSE, agent-card) and bridging A2A tasks to a
# Hermes Agent API Server and/or an OpenClaw Gateway over HTTP + SSE — see
# docker-compose.yml, where both are optional profile-gated sibling services.
#
# All packages (silvaengine_gateway, a2a_daemon_engine, and the shared
# SilvaEngine libraries) are pip-installed from git INTO the image over SSH
# (mirrors ../docker-mcp-kg-gateway / ../docker-silvaengine-gateway) — needed
# because these are PRIVATE ideabosque repos. Configuration is entirely
# env-driven at runtime via .env (see .env.example).
#
# Drop a deploy key (with read access to the ideabosque repos below) into
# ./.ssh before building — see README "Private repos over SSH". The key
# material itself is never baked into any image layer beyond this build
# stage's filesystem; this is a single-stage image, so treat the built image
# like you would any host with SSH access to those repos (don't publish it
# to a public registry).
# =============================================================================

FROM python:3.12-slim

WORKDIR /app

# ── System deps + uv ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    supervisor \
    curl \
    openssh-client \
    git \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

# SSH setup — the gateway pulls private ideabosque repos over git+ssh.
# Drop a deploy key into ./.ssh before building (see README).
ADD .ssh /root/.ssh
RUN chmod 700 /root/.ssh && \
    (chmod 600 /root/.ssh/* 2>/dev/null || true) && \
    ssh-keyscan github.com >> /root/.ssh/known_hosts

ENV PATH="/root/.local/bin:$PATH"

# ── Python dependencies ──────────────────────────────────────────────────────
# requirements.txt installs the third-party deps AND the shared SilvaEngine
# libraries (silvaengine_utility, silvaengine_dynamodb_base, ...) from git over
# SSH. requirements-modules.txt then installs silvaengine_gateway and
# a2a_daemon_engine --no-deps: their metadata declares engines / bare names
# not on PyPI (and intentionally absent from this A2A-only image); their real
# deps are already satisfied by requirements.txt.
COPY requirements.txt requirements-modules.txt ./

RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python -r requirements.txt && \
    uv pip install --python /opt/venv/bin/python --no-deps -r requirements-modules.txt

ENV PATH="/opt/venv/bin:$PATH"

# ── PostgreSQL is the sole persistence backend ──────────────────────────────
# a2a_daemon_engine's Config defaults DB_BACKEND to "dynamodb"; we force
# "postgresql" here so the gateway always wires the SQLAlchemy session even if
# .env omits db_backend. Override only if you know why.
ENV db_backend=postgresql

# ── Route manifest (all modules are drop-in addons) ──────────────────────────
# The packaged gateway ships a routes.yaml registering every engine module
# (KGE, RFQ, MCP, A2A, ...). This image's routes.yaml is just a loader: one
# permanent !include line that pulls in whatever's under ./addons/ — merged
# at container startup by docker-entrypoint.sh (see
# scripts/merge_addon_routes.py and addons/README.md). This image's one core
# module (a2a_daemon_engine) is itself an addon file
# (addons/a2a_daemon_engine.yaml) — NOT baked into the image (addons/ is
# bind-mounted only, see docker-compose.yml). GATEWAY_ROUTES_CONFIG_PATH
# points at routes.yaml below. Running this image without the compose bind
# mounts (routes.yaml + addons/) registers ZERO modules — this image is
# meant to run via docker-compose, not standalone `docker run`.
COPY routes.yaml /app/routes.yaml
COPY scripts/merge_addon_routes.py /app/scripts/merge_addon_routes.py

# ── Supervisor ───────────────────────────────────────────────────────────────
RUN mkdir -p /var/log/supervisor
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# ── Non-root user ────────────────────────────────────────────────────────────
RUN useradd -m -u 1000 gateway && \
    mkdir -p /app/data && \
    chown -R gateway:gateway /app

EXPOSE 8000

# Default route manifest is the loader baked above. Override via .env.
ENV GATEWAY_ROUTES_CONFIG_PATH=/app/routes.yaml

# Entrypoint runs the addon merge step, then execs supervisord. This is the
# ONLY place that decides how the container starts — do not add a
# `command:` override in docker-compose.yml, or it silently bypasses this.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

CMD ["/usr/local/bin/docker-entrypoint.sh"]
