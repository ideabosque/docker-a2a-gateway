#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared helpers for the docker-a2a-gateway test scripts (Hermes + OpenClaw).

Lives at tests/a2a_test_utils.py — one level below the repo root, alongside
the per-bridge tests/hermes/ and tests/openclaw/ script directories. Those
scripts add this directory to sys.path before importing (see the bootstrap
comment at the top of any tests/hermes/*.py or tests/openclaw/*.py file).

Keeps every test script dependency-light (only `requests`) by centralising:
  - .env loading (project root)
  - JWT minting / ADMIN_STATIC_TOKEN resolution (stdlib HMAC, no jose)
  - health probes
  - A2A JSON-RPC request helpers
  - reply / state extraction
  - Hermes- and OpenClaw-specific config resolution + direct API probes

Import from the test scripts via:

    from a2a_test_utils import load_env, resolve_token, send_a2a, ...

Author: bibow
"""
from __future__ import print_function

import base64
import hashlib
import hmac
import json
import math
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Optional,
                    Set)

import requests

__author__ = "bibow"

# This file lives at tests/a2a_test_utils.py — one level below the repo
# root, where .env actually is (docker-compose.yml, .env.example, etc. all
# live at the root, not under tests/).
PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"

# Colours
G = "\033[92m"
R = "\033[91m"
C = "\033[96m"
Y = "\033[93m"
B = "\033[1m"
D = "\033[2m"
RST = "\033[0m"


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------
def load_env(path: Path = ENV_FILE) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if " #" in v:
                v = v.split(" #", 1)[0]
            v = v.strip()
            if k.strip():
                env[k.strip()] = v
    return env


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_jwt(secret: str, algorithm: str = "HS256",
             username: str = "a2a-test", role: str = "admin") -> str:
    """Mint an HS256 JWT with stdlib HMAC (no jose/pendulum needed)."""
    header = {"alg": algorithm, "typ": "JWT"}
    payload = {
        "username": username,
        "role": role,
        "perm": True,
        "iat": int(time.time()),
    }
    seg_h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    seg_p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{seg_h}.{seg_p}".encode()
    sig = _b64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{seg_h}.{seg_p}.{sig}"


def resolve_token(env: Dict[str, str], explicit: Optional[str] = None,
                  username: str = "a2a-test") -> str:
    """Token priority: explicit arg -> ADMIN_STATIC_TOKEN -> minted JWT."""
    if explicit:
        return explicit
    static = env.get("ADMIN_STATIC_TOKEN", "").strip()
    if static:
        return static
    secret = env.get("JWT_SECRET_KEY", "CHANGEME")
    algo = env.get("JWT_ALGORITHM", "HS256")
    if algo != "HS256":
        print(f"{R}JWT_ALGORITHM={algo} not supported by stdlib mint; "
              f"pass --token or set ADMIN_STATIC_TOKEN.{RST}")
        sys.exit(2)
    return mint_jwt(secret, algo, username=username)


# ---------------------------------------------------------------------------
# Config resolution (with CLI override helpers)
# ---------------------------------------------------------------------------
def _host_service_url(env: Dict[str, str], url_key: str, port_key: str,
                       default_port: str) -> str:
    """Derive a host-reachable URL for an in-container sibling service.

    {url_key} (e.g. HERMES_API_URL, OPENCLAW_API_URL) is the in-container
    address (e.g. http://hermes:8642) — the gateway reaches the sibling by
    that name on the docker network, but a test script running on the host
    cannot resolve a bare service name. Fall back to
    127.0.0.1:{port_key} (the host-published port) so health checks work
    from the host. The actual A2A calls still go through the gateway, which
    uses {url_key} internally.
    """
    url = env.get(url_key, "").strip()
    if url and "://" in url:
        from urllib.parse import urlparse

        host = urlparse(url).hostname
        # Service-name hosts (no dot, not localhost/127.0.0.1) aren't
        # resolvable from the host — swap for 127.0.0.1 + the host-published
        # port.
        if host and host not in ("localhost", "127.0.0.1") and "." not in host:
            port = env.get(port_key) or urlparse(url).port or default_port
            return f"http://127.0.0.1:{port}"
    return url or f"http://127.0.0.1:{env.get(port_key, default_port)}"


def _host_hermes_url(env: Dict[str, str]) -> str:
    return _host_service_url(env, "HERMES_API_URL", "HERMES_GATEWAY_PORT", "8642")


def _host_openclaw_url(env: Dict[str, str]) -> str:
    return _host_service_url(env, "OPENCLAW_API_URL", "OPENCLAW_PORT", "18789")


def _host_goose_url(env: Dict[str, str]) -> str:
    return _host_service_url(env, "GOOSE_API_URL", "GOOSE_PORT", "3284")


def _host_pi_url(env: Dict[str, str]) -> str:
    return _host_service_url(env, "PI_API_URL", "PI_PORT", "3300")


def _host_omp_url(env: Dict[str, str]) -> str:
    return _host_service_url(env, "OMP_API_URL", "OMP_PORT", "3310")


def _host_banyan_gateway_url(env: Dict[str, str]) -> str:
    """HOST-reachable URL of docker-banyanos-gateway.

    Banyanos is a separate compose stack (not a bundled sibling of THIS
gateway), so BANYANOS_GATEWAY_URL is normally already host-reachable
(e.g. http://127.0.0.1:8000 or a public URL). Only fall back when an
in-container service name was configured: swap to the host-published
BANYANOS_GATEWAY_PORT.
    """
    url = env.get("BANYANOS_GATEWAY_URL", "").strip()
    if url and "://" in url:
        from urllib.parse import urlparse

        host = urlparse(url).hostname
        if host and host not in ("localhost", "127.0.0.1") and "." not in host:
            port = env.get("BANYANOS_GATEWAY_PORT") or urlparse(url).port or "8000"
            return f"http://127.0.0.1:{port}"
    return url or f"http://127.0.0.1:{env.get('BANYANOS_GATEWAY_PORT', '8000')}"


def resolve_config(env: Dict[str, str], **overrides) -> Dict[str, Any]:
    """Build a config dict from .env + explicit overrides.

    Accepted overrides: gateway_url, hermes_url, openclaw_url, omp_url, token,
    endpoint_id, part_id, agent_uuid, bridge.

    ``hermes_url`` / ``openclaw_url`` / ``goose_url`` default to HOST-reachable URLs
    (127.0.0.1 + the host-published port), not the in-container
    HERMES_API_URL / OPENCLAW_API_URL (which use docker service names the
    host can't resolve). The gateway itself still uses HERMES_API_URL /
    OPENCLAW_API_URL to reach the bundled sibling services.

    ``agent_uuid`` defaults to the calling bridge's own seeded agent —
    A2A_HERMES_AGENT_UUID or A2A_OPENCLAW_AGENT_UUID, picked by the ``bridge``
    override ("hermes" / "openclaw") — so tests/openclaw/* really exercise
    OpenClaw even when A2A_DEFAULT_AGENT_UUID points at Hermes. Without a
    ``bridge`` it falls back to A2A_DEFAULT_AGENT_UUID. ``--agent-uuid``
    always wins.
    """
    container_port = env.get("CONTAINER_PORT", "8765")
    gateway_url = (overrides.get("gateway_url") or
                   f"http://127.0.0.1:{container_port}").rstrip("/")
    hermes_url = (overrides.get("hermes_url") or _host_hermes_url(env)).rstrip("/")
    openclaw_url = (overrides.get("openclaw_url") or _host_openclaw_url(env)).rstrip("/")
    goose_url = (overrides.get("goose_url") or _host_goose_url(env)).rstrip("/")
    pi_url = (overrides.get("pi_url") or _host_pi_url(env)).rstrip("/")
    banyanos_gateway_url = (
        overrides.get("banyanos_gateway_url") or _host_banyan_gateway_url(env)
    ).rstrip("/")
    omp_url = (overrides.get("omp_url") or _host_omp_url(env)).rstrip("/")
    endpoint_id = overrides.get("endpoint_id") or env.get("endpoint_id", "a2a")
    part_id = overrides.get("part_id") or env.get("part_id", "default")
    bridge = overrides.get("bridge")
    if bridge == "openclaw":
        default_agent = env.get("A2A_OPENCLAW_AGENT_UUID", "a2a-openclaw-agent")
    elif bridge == "hermes":
        default_agent = env.get("A2A_HERMES_AGENT_UUID", "a2a-hermes-agent")
    elif bridge == "goose":
        default_agent = env.get("A2A_GOOSE_AGENT_UUID", "a2a-goose-agent")
    elif bridge == "omp":
        default_agent = env.get("A2A_OMP_AGENT_UUID", "a2a-omp-agent")
    elif bridge == "pi":
        default_agent = env.get("A2A_PI_AGENT_UUID", "a2a-pi-agent")
    elif bridge == "banyanos":
        default_agent = (env.get("A2A_BANYANOS_AGENT_UUID")
                         or "a2a-banyanos-agent")
    else:
        default_agent = env.get("A2A_DEFAULT_AGENT_UUID", "a2a-hermes-agent")
    agent_uuid = overrides.get("agent_uuid") or default_agent
    token = resolve_token(env, overrides.get("token"))
    banyanos_endpoint_id = (
        overrides.get("banyanos_endpoint_id")
        or env.get("BANYANOS_ENDPOINT_ID")
        or "banyan"
    )
    banyanos_token = overrides.get("banyanos_token") or env.get("BANYANOS_TOKEN", "")
    banyanos_ws_url = (overrides.get("banyanos_ws_url")
                      or env.get("BANYANOS_WS_URL", "")).rstrip("/")
    # The plugin runs in Docker; these probes run on the host. Docker's
    # host alias is not available in every host DNS configuration.
    if banyanos_ws_url and not overrides.get("banyanos_ws_url"):
        from urllib.parse import urlsplit, urlunsplit
        parsed = urlsplit(banyanos_ws_url)
        if parsed.hostname == "host.docker.internal":
            netloc = "127.0.0.1"
            if parsed.port:
                netloc += f":{parsed.port}"
            banyanos_ws_url = urlunsplit(parsed._replace(netloc=netloc))
    return {
        "gateway_url": gateway_url,
        "hermes_url": hermes_url,
        "hermes_key": env.get("HERMES_API_KEY", ""),
        "openclaw_url": openclaw_url,
        "openclaw_key": env.get("OPENCLAW_API_KEY", ""),
        "goose_url": goose_url,
        "goose_key": env.get("GOOSE_SECRET_KEY", ""),
        "pi_url": pi_url,
        "pi_key": env.get("PI_SECRET_KEY", ""),
        "omp_url": omp_url,
        "omp_key": env.get("OMP_SECRET_KEY", ""),
        "banyanos_gateway_url": banyanos_gateway_url,
        "banyanos_ws_url": banyanos_ws_url,
        "banyanos_endpoint_id": banyanos_endpoint_id,
        "banyanos_token": banyanos_token,
        "banyanos_agent_id": env.get("BANYANOS_AGENT_ID", ""),
        "banyanos_email": env.get("BANYANOS_EMAIL", ""),
        "banyanos_password": env.get("BANYANOS_PASSWORD", ""),
        "endpoint_id": endpoint_id,
        "part_id": part_id,
        "agent_uuid": agent_uuid,
        "token": token,
    }


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------
def health_ok(label: str, url: str, timeout: int = 5,
              headers: Optional[Dict[str, str]] = None) -> bool:
    try:
        r = requests.get(f"{url}/health", headers=headers or {}, timeout=timeout)
        ok = r.status_code == 200
        mark = f"{G}OK{RST}" if ok else f"{R}FAIL{RST}"
        print(f"  {mark} {label} /health -> HTTP {r.status_code}  ({url})")
        return ok
    except Exception as e:
        print(f"  {R}FAIL{RST} {label} /health error: {e}  ({url})")
        return False


# ---------------------------------------------------------------------------
# OpenClaw direct API probes
# ---------------------------------------------------------------------------
def openclaw_models_ok(openclaw_url: str, openclaw_key: str = "",
                       timeout: int = 15) -> bool:
    """Check that OpenClaw /v1/models returns a model list."""
    try:
        r = requests.get(
            f"{openclaw_url}/v1/models",
            headers={"Authorization": f"Bearer {openclaw_key}"} if openclaw_key else {},
            timeout=timeout,
        )
        if r.status_code != 200:
            return False
        data = r.json().get("data", [])
        return any(m.get("id") for m in data if isinstance(m, dict))
    except Exception:
        return False


def openclaw_chat_ok(openclaw_url: str, openclaw_key: str = "",
                     model: str = "openclaw/default",
                     timeout: int = 30) -> bool:
    """Check that OpenClaw /v1/chat/completions works (non-streaming)."""
    try:
        r = requests.post(
            f"{openclaw_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Say OK"}],
                "stream": False,
            },
            headers={
                "Authorization": f"Bearer {openclaw_key}" if openclaw_key else "",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# goose ACP server probe (goose serve — ACP over Streamable HTTP)
# ---------------------------------------------------------------------------
def goose_acp_ok(goose_url: str, goose_key: str = "",
                 timeout: int = 15) -> bool:
    """POST a minimal ACP initialize; expect HTTP 200 with agentCapabilities."""
    try:
        r = requests.post(
            f"{goose_url}/acp",
            json={
                "jsonrpc": "2.0", "method": "initialize",
                "params": {"protocolVersion": 1}, "id": 1,
            },
            headers={
                "Content-Type": "application/json",
                "X-Secret-Key": goose_key or "probe",
                "Acp-Connection-Id": "a2a-test-probe",
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return False
        return "agentCapabilities" in (r.json().get("result") or {})
    except Exception:
        return False


# ---------------------------------------------------------------------------
# pi sidecar probe (the bundled pi sibling — HTTP + SSE over `pi --mode rpc`)
# ---------------------------------------------------------------------------
def pi_sidecar_ok(pi_url: str, pi_key: str = "",
                  timeout: int = 15) -> bool:
    """GET /health with the shared secret; expect {"status": "ok", ...}."""
    try:
        r = requests.get(
            f"{pi_url}/health",
            headers={"X-Secret-Key": pi_key},
            timeout=timeout,
        )
        if r.status_code != 200:
            return False
        return r.json().get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# omp sidecar probe (the bundled omp sibling — HTTP + SSE over `omp --mode rpc`)
# ---------------------------------------------------------------------------
def omp_sidecar_ok(omp_url: str, omp_key: str = "",
                   timeout: int = 15) -> bool:
    """GET /health with the shared secret; expect {"status": "ok", ...}."""
    try:
        r = requests.get(
            f"{omp_url}/health",
            headers={"X-Secret-Key": omp_key},
            timeout=timeout,
        )
        if r.status_code != 200:
            return False
        return r.json().get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Banyanos (docker-banyanos-gateway) direct probes
# ---------------------------------------------------------------------------
def banyanos_gateway_ok(banyanos_url: str, timeout: int = 10) -> bool:
    """GET /health on the Banyanos gateway (SilvaEngine gateway image)."""
    try:
        r = requests.get(f"{banyanos_url.rstrip('/')}/health", timeout=timeout)
        print(f"  banyan gateway /health -> HTTP {r.status_code}  ({banyanos_url})")
        return r.status_code == 200
    except Exception as e:
        print(f"  {R}FAIL{RST} banyan gateway /health error: {e}  ({banyanos_url})")
        return False


def banyanos_graphql_ok(banyanos_url: str, banyanos_endpoint_id: str,
                      banyanos_token: str, part_id: str = "",
                      timeout: int = 30) -> bool:
    """Prove the Banyanos GraphQL route answers with a valid (authed) response.

    Introspection checks the deployed schema without creating a task.
    HTTP 200 alone is insufficient: GraphQL auth and validation errors also
    use that status code.
    """
    url = f"{banyanos_url.rstrip('/')}/{banyanos_endpoint_id}/agent_engine_graphql"
    headers = {
        "Authorization": f"Bearer {banyanos_token}",
        "Content-Type": "application/json",
    }
    if part_id:
        headers["Part-Id"] = part_id
    try:
        r = requests.post(url, json={
            "query": (
                "query Probe { __typename }"
            ),
        }, headers=headers, timeout=timeout)
        print(f"  banyan GraphQL probe -> HTTP {r.status_code}  ({url})")
        body = r.json() if r.status_code == 200 else None
        if (isinstance(body, dict) and not body.get("errors")
                and isinstance(body.get("data"), dict)
                and body["data"].get("__typename")):
            return True
        print("    GraphQL probe failed (HTTP, errors, or missing data)")
        return False
    except Exception as e:
        print(f"  {R}FAIL{RST} banyan GraphQL probe error: {e}")
        return False


def banyanos_login(banyanos_url: str, banyanos_endpoint_id: str, email: str,
                 password: str, part_id: str = "", timeout: int = 30) -> str:
    """Banyanos JWT via user_engine's Login mutation (empty string on failure).

    The backend route is the user_engine_graphql endpoint; auth_token is the
    Bearer credential perm_engine checks for the agent_engine routes.
    """
    url = f"{banyanos_url.rstrip('/')}/{banyanos_endpoint_id}/user_engine_graphql"
    headers = {"Content-Type": "application/json"}
    if part_id:
        headers["Part-Id"] = part_id
    try:
        r = requests.post(url, json={
            "query": (
                "mutation Login($input: LoginInput!, $idempotencyKey: ID!) { "
                "login(input: $input, idempotencyKey: $idempotencyKey) { "
                "authToken requiresMfa } }"
            ),
            "variables": {
                "input": {"email": email, "password": password},
                "idempotencyKey": f"banyanos-login-{uuid.uuid4().hex[:12]}",
            },
        }, headers=headers, timeout=timeout)
        data = (r.json() or {}).get("data") or {}
        result = data.get("login") or {}
        if result.get("requiresMfa"):
            print("  ! Banyanos login reports MFA required - set BANYANOS_TOKEN manually")
            return ""
        token = result.get("authToken") or result.get("auth_token") or ""
        if token:
            print(f"  banyan login OK ({email})")
        else:
            print(f"  ! banyan login failed: HTTP {r.status_code}")
        return token
    except Exception as e:
        print(f"  ! banyan login error: {e}")
        return ""


def banyanos_ws_ok(banyanos_ws_url: str, banyanos_endpoint_id: str,
                 banyanos_token: str, part_id: str = "", timeout: float = 15.0
                 ) -> bool:
    """banyan_ws handshake probe - raw RFC 6455, no extra dependency.

    Opens ``/{endpoint_id}/banyan_ws?token=...&part_id=...`` and waits for
    the gateway welcome frame {"type":"connection_ack","connection_id":...}.
    Closes immediately after; no subscription is triggered.
    """
    conn = BanyanosRawWebSocket(banyanos_ws_url, banyanos_endpoint_id,
                              banyanos_token, part_id, timeout=timeout)
    try:
        ack = conn.connect()
        if not ack:
            return False
        print(f"  banyan_ws connection_ack OK (id={ack[:12]}...)")
        return True
    except _BanyanosWSHandshakeRejected as exc:
        print(f"  ! banyan_ws handshake rejected: {exc.frame!r}")
        return False
    except Exception as e:
        print(f"  {R}FAIL{RST} banyan_ws probe error: {e}")
        return False
    finally:
        if conn.sock is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Banyanos raw WebSocket (RFC 6455) client - no extra dependency
# ---------------------------------------------------------------------------
class _BanyanosWSHandshakeRejected(Exception):
    def __init__(self, frame):
        self.frame = frame
        super().__init__(str(frame))


class BanyanosRawWebSocket:
    """Minimal stdlib WebSocket client for the banyan_ws channel.

    Implements just enough of RFC 6455 for the gateway push surface:
    client->server messages are masked text frames; server frames are read
    unmasked. Frames are not fragmented clientside. Used by the banyan
    live tests to (a) verify the handshake and (b) consume
    ``agentInvokeStream`` subscription events natively.
    """

    def __init__(self, banyanos_ws_url: str, banyanos_endpoint_id: str,
                 banyanos_token: str, part_id: str = "",
                 timeout: float = 15.0) -> None:
        self.timeout = float(timeout)
        self.sock = None
        self._buf = b""
        self.path = None
        self.host = None
        self.port = None
        self.secure = False

        from urllib.parse import parse_qsl, urlencode, urlparse

        parsed = urlparse(banyanos_ws_url.rstrip("/"))
        if parsed.scheme not in ("ws", "wss"):
            url = (
                f"{banyanos_ws_url.rstrip('/')}/{banyanos_endpoint_id}/banyan_ws"
            )
            url = url.replace("http://", "ws://").replace("https://", "wss://")
            parsed = urlparse(url)
        self.secure = parsed.scheme == "wss"
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.secure else 80)
        path = (parsed.path or "")
        if not path.endswith("banyan_ws"):
            path += f"/{banyanos_endpoint_id}/banyan_ws"
        query = [(k, v) for k, v in parse_qsl(parsed.query)
                 if k not in ("token", "part_id")]
        if banyanos_token:
            query.append(("token", banyanos_token))
        if part_id:
            query.append(("part_id", part_id))
        if query:
            path += "?" + urlencode(query)
        self.path = path

    def connect(self) -> str:
        """Upgrade the connection; return the gateway connection_id."""
        import base64
        import socket as _socket
        import ssl as _ssl

        sock = _socket.create_connection((self.host, self.port),
                                         timeout=self.timeout)
        if self.secure:
            ctx = _ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        self.sock = sock
        key = base64.b64encode(uuid.uuid4().bytes).decode()
        crlf = "\r\n"
        req = crlf.join([
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "",
            "",
        ])
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        first_line = head.split(b"\r\n", 1)[0]
        if b" 101" not in first_line:
            frame = first_line.decode("utf-8", "replace")
            try:
                sock.close()
            except Exception:
                pass
            self.sock = None
            raise _BanyanosWSHandshakeRejected(frame)
        headers = dict(
            line.decode("latin-1").split(":", 1)
            for line in head.split(b"\r\n")[1:] if b":" in line
        )
        headers = {k.lower(): v.strip() for k, v in headers.items()}
        expected_accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        if (headers.get("sec-websocket-accept") != expected_accept
                or headers.get("upgrade", "").lower() != "websocket"):
            self.close()
            raise _BanyanosWSHandshakeRejected("invalid WebSocket upgrade headers")
        self._buf = rest
        # Welcome frame: {"type": "connection_ack", "connection_id": ...}
        frame = self.recv_json(self.timeout)
        if not isinstance(frame, dict) or frame.get("type") != "connection_ack":
            try:
                sock.close()
            except Exception:
                pass
            self.sock = None
            raise _BanyanosWSHandshakeRejected(str(frame)[:200])
        self.connection_id = str(frame.get("connection_id") or "")
        return self.connection_id

    def recv_json(self, timeout: float = 15.0) -> Any:
        """Receive the next complete text frame as parsed JSON."""
        import struct

        self.sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            payload = self._buf
            if len(payload) >= 2:
                opcode = payload[0] & 0x0F
                ln = payload[1] & 0x7F
                offset = 2
                if ln == 126:
                    if len(payload) < 4:
                        ln = None
                    else:
                        ln = struct.unpack(">H", payload[2:4])[0]
                        offset = 4
                elif ln == 127:
                    if len(payload) < 10:
                        ln = None
                    else:
                        ln = struct.unpack(">Q", payload[2:10])[0]
                        offset = 10
                if ln is not None and len(payload) >= offset + ln:
                    self._buf = payload[offset + ln:]
                    if opcode == 8:
                        return None  # close frame
                    if opcode == 9:
                        self._send_frame(payload[offset:offset + ln], opcode=10)
                        continue
                    if opcode != 1:
                        continue  # ping/pong/pong-style control frames
                    text = payload[offset:offset + ln].decode("utf-8", "replace")
                    return json.loads(text)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self._buf += chunk

    def send_text(self, text: str) -> None:
        """Send one masked text frame."""
        self._send_frame(text.encode("utf-8"))

    def _send_frame(self, data: bytes, opcode: int = 1) -> None:
        import os
        import struct

        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        self.sock.sendall(bytes(header) + mask + masked)

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None


# ---------------------------------------------------------------------------
# A2A JSON-RPC transport
# ---------------------------------------------------------------------------
def a2a_headers(token: str, part_id: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Part-Id": part_id,
    }


def send_a2a(gateway_url: str, token: str, endpoint_id: str, part_id: str,
             method: str, params: Dict[str, Any], request_id: str = "1",
             timeout: int = 180) -> requests.Response:
    """POST a JSON-RPC 2.0 request to /{endpoint_id}/a2a."""
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}
    return requests.post(
        f"{gateway_url}/{endpoint_id}/a2a",
        json=body,
        headers=a2a_headers(token, part_id),
        timeout=timeout,
    )


def message_send_params(text: str, agent_uuid: str, task_id: str,
                         task_type: str = "a2a_test",
                         stream: bool = True,
                         system_prompt: Optional[str] = None,
                         conversation_history: Optional[list] = None,
                         thread_uuid: Optional[str] = None,
                         show_reasoning: Optional[bool] = None,
                         *,
                         return_mode: str = "blocking") -> Dict[str, Any]:
    """Build params for A2A message/send.

    ``show_reasoning`` (when not None) is sent as ``metadata.show_reasoning``,
    the per-request override of the gateway's A2A_SHOW_REASONING (default on).

    ``return_mode`` selects the client-visible response contract and is
    independent of the transport streaming flag:

      * ``"blocking"`` (default) - the gateway answers with the final
        Message/Task; the params are byte-identical to the historic shape
        (no ``configuration`` key at all).
      * ``"immediate"`` (alias ``"async"``) - the params carry
        ``configuration: {"returnImmediately": true}`` so the gateway
        accepts the work and answers with a nonterminal Task carrying a
        stable task id.

    Any other value raises ``ValueError``.
    """
    metadata: Dict[str, Any] = {
        "operation": "task_execution",
        "agent_uuid": agent_uuid,
        "stream": stream,
        "task_data": {"task_id": task_id, "task_type": task_type},
    }
    if system_prompt:
        metadata["system_prompt"] = system_prompt
    if conversation_history:
        metadata["conversation_history"] = conversation_history
    if thread_uuid:
        metadata["thread_uuid"] = thread_uuid
    if show_reasoning is not None:
        metadata["show_reasoning"] = bool(show_reasoning)
    params: Dict[str, Any] = {
        "message": {"role": "ROLE_USER", "parts": [{"text": text}]},
        "metadata": metadata,
    }
    if normalize_return_mode(return_mode) == RETURN_MODE_IMMEDIATE:
        params["configuration"] = {"returnImmediately": True}
    return params


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def unwrap_response(resp: requests.Response) -> Dict[str, Any]:
    """Parse a JSON-RPC response, unwrapping API-Gateway-style envelopes."""
    try:
        body = resp.json()
    except Exception:
        return {"_raw_text": resp.text, "_status": resp.status_code}
    if isinstance(body, dict) and isinstance(body.get("body"), str):
        try:
            inner = json.loads(body["body"])
            if isinstance(inner, dict):
                body = inner
        except Exception:
            pass
    if not isinstance(body, dict):
        return {"_raw": body, "_status": resp.status_code}
    return body


def extract_text(body: Dict[str, Any]) -> str:
    """Extract agent reply text from a JSON-RPC result."""
    result = body.get("result")
    if isinstance(result, dict):
        parts = result.get("parts", []) or result.get("artifacts", [])
        text = ""
        for p in parts:
            if isinstance(p, dict):
                text += p.get("text", "") or ""
            elif isinstance(p, str):
                text += p
        if not text:
            text = result.get("text", "") or result.get("content", "")
        return text
    return ""


def is_reasoning_artifact(artifact: Any) -> bool:
    """True for an SSE artifact chunk the gateway tagged as model reasoning.

    The bridge streams reasoning (e.g. goose agent_thought_chunk, Hermes
    reasoning deltas) tagged in ``artifact.metadata`` (``data_format:
    reasoning``, or a message_group_id suffix containing "rs"). It is not part
    of the answer, so listeners must not print or count it as reply text.
    """
    meta = artifact.get("metadata") if isinstance(artifact, dict) else None
    return isinstance(meta, dict) and any(
        "reason" in str(v).lower() for v in meta.values()
    )


class StreamSections:
    """Print streamed chunks in tagged reasoning / response sections.

    Same layout as silvaengine_gateway's tests/chat_websocket.py:

        >>> REASONING START
        ...model reasoning...
        <<< REASONING END
        >>> RESPONSE START
        ...answer...
        <<< RESPONSE END

    Reasoning is printed dimmed; ``show_reasoning=False`` hides it locally.
    """

    _LABELS = {"reasoning": ("REASONING", Y), "response": ("RESPONSE", C)}

    def __init__(self, show_reasoning: bool = True, response_color: str = "") -> None:
        self.show_reasoning = show_reasoning
        self.response_color = response_color
        self.mode: Optional[str] = None
        self.reasoning = ""

    def reasoning_chunk(self, text: str) -> None:
        self.reasoning += text
        if not self.show_reasoning:
            return
        self._switch("reasoning")
        print(f"{D}{text}{RST}", end="", flush=True)

    def response_chunk(self, text: str) -> None:
        self._switch("response")
        print(f"{self.response_color}{text}{RST}", end="", flush=True)

    def _switch(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.close()
        label, color = self._LABELS[mode]
        print(f"\n{color}>>> {label} START{RST}", flush=True)
        self.mode = mode

    def close(self) -> None:
        """End the open section (call once per turn, after the stream)."""
        if self.mode:
            label, color = self._LABELS[self.mode]
            print(f"\n{color}<<< {label} END{RST}", flush=True)
        self.mode = None

    def reset(self) -> None:
        self.close()
        self.reasoning = ""


def extract_state(body: Dict[str, Any]) -> str:
    """Extract a task state name from a JSON-RPC result (if present)."""
    result = body.get("result")
    if isinstance(result, dict):
        state = result.get("status", {})
        if isinstance(state, dict):
            return state.get("state", "") or state.get("status", "")
        if isinstance(state, str):
            return state
    return ""


def jsonrpc_error(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return body.get("error") if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Long-running task harness (docs/LONG_RUNNING_TASKS_TEST_PLAN.md)
#
# Additive helpers shared by the offline harness suite
# (tests/unit/test_long_running_harness.py) and the live long-running
# scenarios:
#
#   - explicit blocking / immediate (configuration.returnImmediately) modes
#   - envelope validation (direct Message reply or A2A Task)
#   - bounded tasks/get polling with a monotonic deadline
#   - a real SSE parser (one HTTP chunk is never one event)
#   - redaction and content-free metrics
# ---------------------------------------------------------------------------

RETURN_MODE_BLOCKING = "blocking"
RETURN_MODE_IMMEDIATE = "immediate"
RETURN_MODE_ASYNC = "async"
RETURN_MODES = (RETURN_MODE_BLOCKING, RETURN_MODE_IMMEDIATE, RETURN_MODE_ASYNC)

# Task states that will never change again - the poll loop stops on them.
TERMINAL_TASK_STATES = frozenset({
    "completed", "failed", "canceled", "rejected",
})

REDACTION_PLACEHOLDER = "***REDACTED***"

# Keys that may carry a backend invocation correlation id.
CORRELATION_ID_KEYS = (
    "correlation_id", "correlationId",
    "invocation_id", "invocationId",
    "backend_invocation_id", "backendInvocationId",
    "backend_correlation_id", "backendCorrelationId",
    "request_id", "requestId",
)

_STATE_ALIASES = {
    "cancelled": "canceled",
    "cancel": "canceled",
    "complete": "completed",
    "failure": "failed",
    "error": "failed",
    "reject": "rejected",
}


def normalize_return_mode(return_mode: Any) -> str:
    """Canonicalize a return mode to "blocking" or "immediate"."""
    text = str(return_mode if return_mode is not None else "").strip().lower()
    if text in ("", RETURN_MODE_BLOCKING, "sync", "wait"):
        return RETURN_MODE_BLOCKING
    if text in (RETURN_MODE_IMMEDIATE, RETURN_MODE_ASYNC,
                "return_immediately", "returnimmediately"):
        return RETURN_MODE_IMMEDIATE
    raise ValueError(
        f"unknown return_mode {return_mode!r} (expected one of {RETURN_MODES})")


def normalize_task_state(state: Any) -> str:
    """Normalize an A2A task state to a canonical lower-case name.

    Accepts the protobuf enum names ("TASK_STATE_COMPLETED"), the plain
    JSON names ("completed") and the usual spelling variants.
    """
    if state is None:
        return ""
    if isinstance(state, dict):
        state = state.get("state") or state.get("status") or ""
    text = str(state).strip().lower()
    if not text:
        return ""
    if text.startswith("task_state_"):
        text = text[len("task_state_"):]
    return _STATE_ALIASES.get(text, text)


def is_terminal_state(state: Any) -> bool:
    """True when the task state can no longer change."""
    return normalize_task_state(state) in TERMINAL_TASK_STATES


class A2AEnvelopeError(Exception):
    """Raised when a gateway response is not a usable A2A envelope."""


@dataclass
class TaskEnvelope:
    """The public task data extracted from an A2A JSON-RPC response."""

    task_id: str = ""
    context_id: str = ""
    state: str = ""
    raw_state: str = ""
    kind: str = ""
    text: str = ""
    result: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return is_terminal_state(self.state)


def _first_str(payload: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


def _parts_text(parts: Any) -> str:
    chunks: List[str] = []
    if not isinstance(parts, list):
        return ""
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
        elif isinstance(part, str) and part:
            chunks.append(part)
    return "".join(chunks)


def extract_task_output(result: Dict[str, Any]) -> str:
    """Extract the persisted answer text of a Task.

    The artifacts are authoritative; the status message is only used when
    the task carries no artifact text (it usually mirrors the last
    artifact and would otherwise duplicate the answer).
    """
    if not isinstance(result, dict):
        return ""
    chunks: List[str] = []
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, str):
                chunks.append(artifact)
                continue
            if not isinstance(artifact, dict):
                continue
            text = artifact.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
            chunks.append(_parts_text(artifact.get("parts")))
    output = "".join(chunks)
    if output:
        return output
    status = result.get("status")
    if isinstance(status, dict):
        message = status.get("message")
        if isinstance(message, dict):
            return _parts_text(message.get("parts"))
    return ""


def _describe_jsonrpc_error(error: Any) -> str:
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or error.get("data") or ""
        if code is not None and message:
            return f"{code}: {message}"
        if message:
            return str(message)
        return str(error)
    return str(error)


def _describe_graphql_errors(errors: Any) -> str:
    if isinstance(errors, dict):
        errors = [errors]
    parts: List[str] = []
    for item in errors or []:
        if isinstance(item, dict):
            parts.append(str(item.get("message") or item))
        else:
            parts.append(str(item))
    return "; ".join(parts) or "unknown GraphQL error"


def decode_jsonrpc_response(resp: Any, *,
                             require_http_ok: bool = True) -> Dict[str, Any]:
    """Decode a JSON-RPC response body, raising on HTTP and shape errors."""
    status = int(getattr(resp, "status_code", 200) or 200)
    body = unwrap_response(resp)
    if require_http_ok and status >= 400:
        detail = ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = f": {error.get('message') or error.get('code') or ''}"
            elif isinstance(error, str):
                detail = f": {error}"
        raise A2AEnvelopeError(f"HTTP {status} from the A2A gateway{detail}")
    if not isinstance(body, dict):
        raise A2AEnvelopeError(
            "malformed JSON-RPC envelope: expected an object, got "
            f"{type(body).__name__}")
    return body


def parse_task_envelope(body: Any, *, require_task_id: bool = True,
                        require_non_terminal: bool = False) -> TaskEnvelope:
    """Validate a decoded JSON-RPC body and extract the public task data.

    Accepts a direct A2A Message reply as well as an A2A Task. Raises
    A2AEnvelopeError for a malformed envelope, a JSON-RPC error body, a
    GraphQL error body, a missing task id and - when
    ``require_non_terminal`` is set - an already terminal task.
    """
    if not isinstance(body, dict):
        raise A2AEnvelopeError(
            "malformed JSON-RPC envelope: expected an object, got "
            f"{type(body).__name__}")
    error = body.get("error")
    if error is not None:
        raise A2AEnvelopeError(
            f"JSON-RPC error: {_describe_jsonrpc_error(error)}")
    graphql_errors = body.get("errors")
    if graphql_errors:
        raise A2AEnvelopeError(
            f"GraphQL error: {_describe_graphql_errors(graphql_errors)}")
    if "result" not in body:
        raise A2AEnvelopeError(
            "malformed JSON-RPC envelope: missing 'result'")
    result = body.get("result")
    if not isinstance(result, dict):
        raise A2AEnvelopeError(
            "malformed JSON-RPC envelope: 'result' must be an object, got "
            f"{type(result).__name__}")
    status = result.get("status")
    if isinstance(status, (dict, str)):
        kind = "task"
    elif "parts" in result or result.get("kind") == "message":
        kind = "message"
    else:
        kind = "task"
    if kind == "message":
        task_id = _first_str(result, "taskId", "task_id")
    else:
        task_id = _first_str(result, "id", "taskId", "task_id")
    context_id = _first_str(result, "contextId", "context_id")
    raw_state = ""
    if isinstance(status, dict):
        raw_state = str(status.get("state") or status.get("status") or "")
    elif isinstance(status, str):
        raw_state = status
    state = normalize_task_state(raw_state)
    if kind == "task":
        text = extract_task_output(result)
    else:
        text = _parts_text(result.get("parts"))
    if require_task_id and not task_id:
        raise A2AEnvelopeError("malformed A2A envelope: missing task id")
    if require_non_terminal:
        if kind != "task":
            raise A2AEnvelopeError(
                "expected an A2A Task, got a direct Message reply")
        if is_terminal_state(state):
            raise A2AEnvelopeError(
                f"expected a nonterminal task, got terminal state {state!r}")
    return TaskEnvelope(task_id=task_id, context_id=context_id, state=state,
                        raw_state=raw_state, kind=kind, text=text,
                        result=result)


@dataclass
class PollResult:
    """The outcome of a bounded tasks/get poll loop."""

    task_id: str = ""
    context_id: str = ""
    state: str = ""
    output: str = ""
    task: Dict[str, Any] = field(default_factory=dict)
    polls: int = 0
    elapsed: float = 0.0
    timed_out: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the task reached a terminal state without an error."""
        return bool(self.state) and is_terminal_state(self.state) \
            and not self.error


def poll_task(gateway_url: str, token: str, endpoint_id: str, part_id: str,
              task_id: str, *, agent_uuid: str = "",
              timeout: float = 60.0, interval: float = 1.0,
              request_timeout: float = 15.0,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic,
              terminal_states: Optional[Iterable[str]] = None) -> PollResult:
    """Poll ``tasks/get`` until the task reaches a terminal state.

    The overall deadline is monotonic (``clock``), the poll interval is
    configurable and every request carries its own bounded timeout, so
    the loop can never run away. The authoritative persisted output is
    read from the task itself, never from the SSE stream.

    A JSON-RPC / GraphQL error body is a permanent failure and raises
    A2AEnvelopeError. Transport failures (connection loss, HTTP 5xx,
    HTTP 429) are retried until the deadline; a permanent HTTP 4xx stops
    the loop immediately and is reported in ``error``.
    """
    terminal = {normalize_task_state(state)
                for state in (terminal_states or TERMINAL_TASK_STATES)}
    terminal.discard("")
    interval = float(interval)
    if interval <= 0:
        raise ValueError("poll interval must be > 0 seconds")
    timeout = max(0.0, float(timeout))
    max_polls = int(math.ceil(timeout / interval)) + 1
    started = clock()
    deadline = started + timeout
    polls = 0
    last_error = ""
    state = ""
    context_id = ""
    task_payload: Dict[str, Any] = {}
    while polls < max_polls:
        polls += 1
        params: Dict[str, Any] = {"id": task_id}
        if agent_uuid:
            params["metadata"] = {"agent_uuid": agent_uuid}
        response = None
        try:
            response = send_a2a(
                gateway_url, token, endpoint_id, part_id, "tasks/get", params,
                request_id=f"poll-{task_id}-{polls}",
                timeout=request_timeout,
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if response is not None:
            status_code = int(getattr(response, "status_code", 200) or 200)
            if status_code >= 400:
                last_error = f"HTTP {status_code} from tasks/get"
                if status_code < 500 and status_code != 429:
                    return PollResult(
                        task_id=task_id, context_id=context_id, state=state,
                        output=extract_task_output(task_payload),
                        task=task_payload, polls=polls,
                        elapsed=clock() - started, error=last_error)
            else:
                body = decode_jsonrpc_response(response)
                envelope = parse_task_envelope(body, require_task_id=False)
                task_payload = envelope.result
                state = envelope.state
                context_id = envelope.context_id
                last_error = ""
                if state in terminal:
                    return PollResult(
                        task_id=envelope.task_id or task_id,
                        context_id=context_id, state=state,
                        output=envelope.text, task=task_payload, polls=polls,
                        elapsed=clock() - started)
        now = clock()
        if now >= deadline:
            break
        sleep(min(interval, deadline - now))
    return PollResult(task_id=task_id, context_id=context_id, state=state,
                      output=extract_task_output(task_payload),
                      task=task_payload, polls=polls,
                      elapsed=clock() - started, timed_out=True,
                      error=last_error)


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------
@dataclass
class SSEEvent:
    """One complete server-sent event."""

    id: str = ""
    event: str = ""
    data: str = ""
    raw: str = ""

    def payload(self) -> Dict[str, Any]:
        """The JSON payload of the frame (empty when it is not JSON)."""
        if not self.data:
            return {}
        try:
            value = json.loads(self.data)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @property
    def type(self) -> str:
        """The gateway frame type (JSON "type", else the SSE event name)."""
        payload = self.payload()
        for key in ("type", "event"):
            value = payload.get(key)
            if value:
                return str(value)
        return self.event


def is_heartbeat_event(event: SSEEvent) -> bool:
    """True for a keep-alive frame (no data payload, or type "heartbeat")."""
    if not event.data.strip():
        return True
    return event.type == "heartbeat"


def _apply_sse_field(name: str, value: str, state: Dict[str, Any]) -> None:
    if name == "data":
        state["data"].append(value)
    elif name == "event":
        state["event"] = value
    elif name == "id":
        if "\x00" not in value:
            state["id"] = value
    # "retry" and unknown fields are ignored on purpose.


def _parse_sse_chunks(chunks: Iterable[Any]) -> Iterator[SSEEvent]:
    """Parse raw SSE chunks into complete events (buffered by block)."""
    buffer = b""
    state: Dict[str, Any] = {"data": [], "event": "", "id": "", "raw": []}

    def _dispatch() -> Optional[SSEEvent]:
        data_lines = state["data"]
        if not (data_lines or state["event"] or state["id"]):
            state["data"] = []
            state["raw"] = []
            return None
        event = SSEEvent(
            id=state["id"],
            event=state["event"],
            data="\n".join(data_lines),
            raw="\n".join(state["raw"]),
        )
        state["data"] = []
        state["event"] = ""
        state["id"] = ""
        state["raw"] = []
        return event

    for chunk in chunks or ():
        if chunk is None:
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        buffer += chunk
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            line = buffer[:index]
            buffer = buffer[index + 1:]
            if line.endswith(b"\r"):
                line = line[:-1]
            text = line.decode("utf-8", "replace")
            if text == "":
                event = _dispatch()
                if event is not None:
                    yield event
                continue
            state["raw"].append(text)
            if text.startswith(":"):
                continue  # comment / keep-alive line
            name, _, value = text.partition(":")
            if value.startswith(" "):
                value = value[1:]
            _apply_sse_field(name, value, state)
    # EOF: flush a trailing partial line and the pending event.
    if buffer:
        text = buffer.decode("utf-8", "replace")
        buffer = b""
        if not text.startswith(":"):
            state["raw"].append(text)
            name, _, value = text.partition(":")
            if value.startswith(" "):
                value = value[1:]
            _apply_sse_field(name, value, state)
    event = _dispatch()
    if event is not None:
        yield event


def sse_dedupe_key(event: SSEEvent) -> str:
    """Deduplication key: the SSE id when present, else event + data."""
    if event.id:
        return f"id:{event.id}"
    return f"data:{event.event}\x00{event.data}"


def dedupe_sse_events(events: Iterable[SSEEvent], *,
                      key: Optional[Callable[[SSEEvent], str]] = None
                      ) -> Iterator[SSEEvent]:
    """Drop repeated events (replayed frames) keeping the first occurrence."""
    key_fn = key or sse_dedupe_key
    seen: Set[str] = set()
    for event in events:
        marker = key_fn(event)
        if marker in seen:
            continue
        seen.add(marker)
        yield event


def iter_sse_events(chunks: Iterable[Any], *,
                    dedupe: bool = False) -> Iterator[SSEEvent]:
    """Parse raw SSE chunks into complete events.

    The chunks are buffered until a blank-line boundary, so one HTTP
    chunk is never assumed to be one event. Multi-line ``data:`` fields
    are joined with newlines, ``id:`` is optional (the gateway adds it
    in parallel) and the pending block is flushed at EOF.

    ``SSEEvent.id`` is the id the frame itself carries - a frame without
    an id never inherits the id of an earlier frame, so deduplication
    cannot drop a frame. The reconnect cursor is the last non-empty id
    of the stream, see ``last_event_id``.
    """
    events = _parse_sse_chunks(chunks)
    if dedupe:
        return dedupe_sse_events(events)
    return events


def last_event_id(events: Iterable[SSEEvent]) -> str:
    """The last non-empty SSE event id (the reconnect cursor)."""
    last = ""
    for event in events:
        if event.id:
            last = event.id
    return last


def summarize_sse_events(events: Iterable[SSEEvent]) -> Dict[str, Any]:
    """Summarize a stream: frame counts, terminal state and event ids."""
    counts: Dict[str, int] = {}
    event_ids: List[str] = []
    task_ids: List[str] = []
    terminal_state = ""
    artifacts = 0
    heartbeats = 0
    for event in events:
        etype = event.type or "message"
        counts[etype] = counts.get(etype, 0) + 1
        if event.id:
            event_ids.append(event.id)
        if is_heartbeat_event(event):
            heartbeats += 1
        payload = event.payload()
        task_id = payload.get("task_id") or payload.get("taskId")
        if task_id:
            task_ids.append(str(task_id))
        if etype in ("task_status", "status"):
            state = normalize_task_state(
                payload.get("state") or payload.get("status"))
            if state:
                terminal_state = state
        if etype == "task_artifact":
            artifacts += 1
    return {
        "counts": counts,
        "event_ids": event_ids,
        "task_ids": task_ids,
        "terminal_state": terminal_state,
        "artifacts": artifacts,
        "heartbeats": heartbeats,
    }


# ---------------------------------------------------------------------------
# Redaction and content-free metrics
# ---------------------------------------------------------------------------
def redact(text: Any, secrets: Optional[Iterable[Any]] = None, *,
           placeholder: str = REDACTION_PLACEHOLDER) -> str:
    """Replace every occurrence of each secret with a placeholder."""
    if text is None:
        return ""
    out = str(text)
    for secret in (secrets or ()):
        if secret is None:
            continue
        token = str(secret)
        if not token:
            continue
        out = out.replace(token, placeholder)
    return out


def redact_structure(value: Any, secrets: Optional[Iterable[Any]] = None, *,
                      placeholder: str = REDACTION_PLACEHOLDER) -> Any:
    """Return a copy of a JSON-like structure with every secret redacted."""
    if isinstance(value, dict):
        return {key: redact_structure(item, secrets, placeholder=placeholder)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_structure(item, secrets, placeholder=placeholder)
                for item in value]
    if isinstance(value, tuple):
        return tuple(redact_structure(item, secrets, placeholder=placeholder)
                     for item in value)
    if isinstance(value, str):
        return redact(value, secrets, placeholder=placeholder)
    return value


def extract_correlation_id(payload: Any, _depth: int = 0) -> str:
    """Find a backend invocation correlation id in a result or event."""
    if not isinstance(payload, dict) or _depth > 3:
        return ""
    for key in CORRELATION_ID_KEYS:
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    for nested in ("metadata", "task", "result", "status", "artifact"):
        child = payload.get(nested)
        if isinstance(child, dict):
            found = extract_correlation_id(child, _depth + 1)
            if found:
                return found
    return ""


@dataclass
class LongRunningMetrics:
    """Timing and correlation metrics of a long-running task observation.

    The metrics never hold bearer tokens, provider keys or answer
    content - only the length of the final output.
    """

    submission_latency_ms: Optional[float] = None
    acceptance_latency_ms: Optional[float] = None
    first_artifact_ms: Optional[float] = None
    terminal_ms: Optional[float] = None
    event_ids: List[str] = field(default_factory=list)
    output_length: int = 0
    correlation_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "submission_latency_ms": self.submission_latency_ms,
            "acceptance_latency_ms": self.acceptance_latency_ms,
            "first_artifact_ms": self.first_artifact_ms,
            "terminal_ms": self.terminal_ms,
            "event_ids": list(self.event_ids),
            "output_length": self.output_length,
            "correlation_id": self.correlation_id,
        }


def build_long_running_metrics(*, submission_latency: Optional[float] = None,
                                submitted_at: Optional[float] = None,
                                accepted_at: Optional[float] = None,
                                first_artifact_at: Optional[float] = None,
                                terminal_at: Optional[float] = None,
                                event_ids: Optional[Iterable[Any]] = None,
                                output: Optional[str] = None,
                                output_length: Optional[int] = None,
                                correlation_id: str = "") -> LongRunningMetrics:
    """Build the content-free metrics of a long-running task observation.

    ``submission_latency`` is the duration of the submit request in
    seconds; the other timestamps are points of the same monotonic clock
    as ``submitted_at`` and are reported as offsets from it.
    """
    def _offset(mark: Optional[float]) -> Optional[float]:
        if mark is None or submitted_at is None:
            return None
        return round((float(mark) - float(submitted_at)) * 1000.0, 3)

    if output_length is None:
        output_length = len(output) if isinstance(output, str) else 0
    ids = [str(item) for item in (event_ids or ()) if item]
    return LongRunningMetrics(
        submission_latency_ms=(None if submission_latency is None
                               else round(float(submission_latency) * 1000.0, 3)),
        acceptance_latency_ms=_offset(accepted_at),
        first_artifact_ms=_offset(first_artifact_at),
        terminal_ms=_offset(terminal_at),
        event_ids=ids,
        output_length=int(output_length),
        correlation_id=str(correlation_id or ""),
    )


__all__ = [
    "G", "R", "C", "Y", "B", "D", "RST",
    "PROJECT_DIR", "ENV_FILE",
    "load_env", "mint_jwt", "resolve_token", "resolve_config",
    "health_ok", "openclaw_models_ok", "openclaw_chat_ok", "goose_acp_ok",
    "pi_sidecar_ok", "omp_sidecar_ok", "banyanos_gateway_ok", "banyanos_graphql_ok",
    "banyanos_login", "banyanos_ws_ok",
    "a2a_headers", "send_a2a", "message_send_params",
    "unwrap_response", "extract_text", "extract_state", "jsonrpc_error",
    "is_reasoning_artifact", "StreamSections",
    # Long-running task harness
    "RETURN_MODE_BLOCKING", "RETURN_MODE_IMMEDIATE", "RETURN_MODE_ASYNC",
    "RETURN_MODES", "TERMINAL_TASK_STATES", "REDACTION_PLACEHOLDER",
    "CORRELATION_ID_KEYS",
    "normalize_return_mode", "normalize_task_state", "is_terminal_state",
    "A2AEnvelopeError", "TaskEnvelope", "decode_jsonrpc_response",
    "parse_task_envelope", "extract_task_output",
    "PollResult", "poll_task",
    "SSEEvent", "iter_sse_events", "dedupe_sse_events", "sse_dedupe_key",
    "is_heartbeat_event", "last_event_id", "summarize_sse_events",
    "redact", "redact_structure", "extract_correlation_id",
    "LongRunningMetrics", "build_long_running_metrics",
]
