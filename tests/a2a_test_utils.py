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
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

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


def resolve_config(env: Dict[str, str], **overrides) -> Dict[str, Any]:
    """Build a config dict from .env + explicit overrides.

    Accepted overrides: gateway_url, hermes_url, openclaw_url, token,
    endpoint_id, part_id, agent_uuid.

    ``hermes_url`` / ``openclaw_url`` default to HOST-reachable URLs
    (127.0.0.1 + the host-published port), not the in-container
    HERMES_API_URL / OPENCLAW_API_URL (which use docker service names the
    host can't resolve). The gateway itself still uses HERMES_API_URL /
    OPENCLAW_API_URL to reach the bundled sibling services.

    ``agent_uuid`` defaults to A2A_DEFAULT_AGENT_UUID from .env (the bridge
    fallback default configured there — see .env.example). Pass
    ``--agent-uuid`` explicitly to target the OTHER bridge's agent when both
    are registered (e.g. a2a-openclaw-agent) and the env default points at
    Hermes, or vice versa.
    """
    container_port = env.get("CONTAINER_PORT", "8765")
    gateway_url = (overrides.get("gateway_url") or
                   f"http://127.0.0.1:{container_port}").rstrip("/")
    hermes_url = (overrides.get("hermes_url") or _host_hermes_url(env)).rstrip("/")
    openclaw_url = (overrides.get("openclaw_url") or _host_openclaw_url(env)).rstrip("/")
    endpoint_id = overrides.get("endpoint_id") or env.get("endpoint_id", "a2a")
    part_id = overrides.get("part_id") or env.get("part_id", "default")
    agent_uuid = (overrides.get("agent_uuid") or
                  env.get("A2A_DEFAULT_AGENT_UUID", "a2a-hermes-agent"))
    token = resolve_token(env, overrides.get("token"))
    return {
        "gateway_url": gateway_url,
        "hermes_url": hermes_url,
        "hermes_key": env.get("HERMES_API_KEY", ""),
        "openclaw_url": openclaw_url,
        "openclaw_key": env.get("OPENCLAW_API_KEY", ""),
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
                         thread_uuid: Optional[str] = None) -> Dict[str, Any]:
    """Build params for A2A message/send."""
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
    return {
        "message": {"role": "ROLE_USER", "parts": [{"text": text}]},
        "metadata": metadata,
    }


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


__all__ = [
    "G", "R", "C", "Y", "B", "D", "RST",
    "PROJECT_DIR", "ENV_FILE",
    "load_env", "mint_jwt", "resolve_token", "resolve_config",
    "health_ok", "openclaw_models_ok", "openclaw_chat_ok",
    "a2a_headers", "send_a2a", "message_send_params",
    "unwrap_response", "extract_text", "extract_state", "jsonrpc_error",
]
