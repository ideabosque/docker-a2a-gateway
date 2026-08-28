#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive chatbot test for the docker-a2a-gateway stack (Hermes bridge).

Opens an SSE connection for real-time streaming and lets you chat with the
Hermes Agent interactively. Each message you type flows through the full
pipeline:

    You (stdin)
      -> POST /{endpoint_id}/a2a  (message/send)
      -> SilvaEngine Gateway -> A2ADaemonExecutor -> Phase 10 bridge
      -> HermesAgentHandler -> Hermes API Server (POST /v1/runs + SSE)
      -> Token chunks broadcast to SSE stream
      -> Printed here in real-time

This script is a standalone examination harness for the A2A gateway image:
it loads ./env from this directory, mints/reuses a gateway JWT, health-checks
both the gateway and the Hermes API server, then runs an interactive REPL
against the native A2A JSON-RPC surface.

Prerequisites:
    - The stack is up:   docker compose --profile postgres --profile hermes up -d
    - Gateway on http://127.0.0.1:8765 (CONTAINER_PORT) and Hermes on 8642.
    - A hermes agent registered OR the env-var fallbacks in .env set:
        A2A_AI_AGENT_MODULE=a2a_daemon_engine.handlers.a2a_hermes_handler
        A2A_AI_AGENT_CLASS=HermesAgentHandler
        A2A_DEFAULT_AGENT_UUID=a2a-hermes-agent
        HERMES_API_URL / HERMES_API_KEY

Usage:
    python tests/hermes/test_chatbot.py
    python tests/hermes/test_chatbot.py --gateway-url http://127.0.0.1:8765
    python tests/hermes/test_chatbot.py --system "You are a pirate"
    python tests/hermes/test_chatbot.py --token <pre_minted_jwt>
    python tests/hermes/test_chatbot.py --no-sse          # HTTP-only (no streaming)

Only third-party dependency: requests
    pip install requests

Author: bibow
"""
from __future__ import print_function

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

__author__ = "bibow"

# This file lives at tests/hermes/test_chatbot.py — two levels below the
# repo root, where .env actually is.
PROJECT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_DIR / ".env"

# Colours
G = "\033[92m"   # green
R = "\033[91m"   # red
C = "\033[96m"   # cyan
Y = "\033[93m"   # yellow
B = "\033[1m"    # bold
D = "\033[2m"    # dim
RST = "\033[0m"


# ---------------------------------------------------------------------------
# .env loader (minimal: no shell expansion, strips inline comments)
# ---------------------------------------------------------------------------
def load_env(path=ENV_FILE):
    env = {}
    if not path.exists():
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            # Strip an inline " #..." comment but keep "#" inside quoted values
            if " #" in v:
                v = v.split(" #")[0]
            v = v.strip()
            if k.strip():
                env[k.strip()] = v
    return env


def _host_hermes_url(env):
    """Derive a host-reachable Hermes URL for the health check.

    HERMES_API_URL is the in-container address (e.g. http://hermes:8641) — the
    gateway reaches Hermes by that name on the docker network, but a script
    running on the host cannot resolve `hermes`. Fall back to
    127.0.0.1:HERMES_GATEWAY_PORT (the host-published port) so the health
    check works from the host. The actual A2A calls still go through the
    gateway, which uses HERMES_API_URL internally.
    """
    from urllib.parse import urlparse

    hermes_url = env.get("HERMES_API_URL", "").strip()
    if hermes_url and "://" in hermes_url:
        host = urlparse(hermes_url).hostname
        if host and host not in ("localhost", "127.0.0.1") and "." not in host:
            port = env.get("HERMES_GATEWAY_PORT") or urlparse(hermes_url).port or "8642"
            return f"http://127.0.0.1:{port}"
    return hermes_url or f"http://127.0.0.1:{env.get('HERMES_GATEWAY_PORT', '8642')}"


# ---------------------------------------------------------------------------
# Token: prefer ADMIN_STATIC_TOKEN from .env; else mint an HS256 JWT with
# stdlib HMAC so the script only needs `requests` (no jose/pendulum).
# ---------------------------------------------------------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_jwt(secret: str, algorithm: str = "HS256") -> str:
    header = {"alg": algorithm, "typ": "JWT"}
    payload = {
        "username": "chatbot",
        "role": "admin",
        "perm": True,
        "iat": int(time.time()),
    }
    seg_header = _b64url(json.dumps(header, separators=(",", ":")).encode())
    seg_payload = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{seg_header}.{seg_payload}".encode()
    digest = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    sig = _b64url(digest)
    return f"{seg_header}.{seg_payload}.{sig}"


def resolve_token(env, explicit=None):
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
    return mint_jwt(secret, algo)


# ---------------------------------------------------------------------------
# SSE listener
# ---------------------------------------------------------------------------
class SSEListener:
    """Background SSE listener that prints streaming chunks in real-time."""

    def __init__(self, gateway_url, token, endpoint_id, part_id):
        self.gateway_url = gateway_url
        self.token = token
        self.endpoint_id = endpoint_id
        self.part_id = part_id
        self.stop = False
        self.thread = None
        self.current_task_id = None
        self.full_text = ""
        self.done = threading.Event()
        self._turn = 0
        self._active_turn = -1

    def start(self):
        def _listen():
            active_turn = -1
            local_text = ""
            try:
                r = requests.get(
                    f"{self.gateway_url}/{self.endpoint_id}/a2a_sse",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Part-Id": self.part_id,
                        "Accept": "text/event-stream",
                    },
                    stream=True,
                    timeout=300,
                )
                if r.status_code != 200:
                    print(f"{R}SSE connection failed: HTTP {r.status_code}{RST}")
                    return

                for line in r.iter_lines(decode_unicode=True):
                    if self.stop:
                        break
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type", event.get("event", ""))

                        if etype == "task_artifact":
                            tid = event.get("task_id", "")
                            if tid == "streaming-task" or tid == self.current_task_id:
                                if self._active_turn != active_turn:
                                    active_turn = self._active_turn
                                    local_text = ""

                                artifact = event.get("artifact", {})
                                if isinstance(artifact, dict) and artifact.get("text"):
                                    text = artifact["text"]
                                    # Skip accumulated / duplicate chunks
                                    if local_text and (
                                        text == local_text
                                        or text.startswith(local_text)
                                        or local_text.startswith(text)
                                    ):
                                        pass
                                    else:
                                        print(f"{Y}{text}{RST}", end="", flush=True)
                                        local_text += text
                                        self.full_text = local_text

                        if etype in ("task_status", "status"):
                            state = event.get("state", event.get("status", ""))
                            if state in ("completed", "COMPLETED"):
                                self.done.set()

                        if etype == "error" or "error" in str(event).lower()[:20]:
                            err = event.get("error", event.get("message", ""))
                            if err:
                                print(f"\n{R}Error: {err}{RST}", flush=True)
                                self.done.set()

                    elif line.startswith("event: "):
                        pass

            except requests.exceptions.ConnectionError:
                pass
            except Exception as e:
                if not self.stop:
                    print(f"{R}SSE error: {e}{RST}")

        self.thread = threading.Thread(target=_listen, daemon=True)
        self.thread.start()
        time.sleep(1)

    def set_task(self, task_id):
        self.current_task_id = task_id
        self.full_text = ""
        self.done.clear()
        self._turn += 1
        self._active_turn = self._turn

    def stop_listening(self):
        self.stop = True


# ---------------------------------------------------------------------------
# A2A JSON-RPC message/send
# ---------------------------------------------------------------------------
def send_message(gateway_url, token, endpoint_id, part_id, text, task_id,
                 agent_uuid, system_prompt=None, conversation_history=None):
    """Send a message/send and return the HTTP response."""
    parts = [{"text": text}]

    metadata = {
        "operation": "task_execution",
        "agent_uuid": agent_uuid,
        "stream": True,
        "task_data": {"task_id": task_id, "task_type": "hermes_chatbot"},
    }
    if system_prompt:
        metadata["system_prompt"] = system_prompt
    if conversation_history:
        metadata["conversation_history"] = conversation_history

    body = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {"role": "ROLE_USER", "parts": parts},
            "metadata": metadata,
        },
        "id": f"chat-{task_id}",
    }

    return requests.post(
        f"{gateway_url}/{endpoint_id}/a2a",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Part-Id": part_id,
        },
        timeout=180,
    )


def extract_text_from_response(body):
    """Extract text from a JSON-RPC response."""
    result = body.get("result", {})
    if isinstance(result, dict):
        parts = result.get("parts", [])
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in parts
        )
    return ""


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
def health_check(label, url):
    try:
        r = requests.get(f"{url}/health", timeout=5)
        if r.status_code != 200:
            print(f"{R}{label} not healthy: HTTP {r.status_code}{RST}")
            return False
        print(f"{G}OK{RST} {label}: {url}")
        return True
    except Exception as e:
        print(f"{R}Cannot reach {label} at {url}: {e}{RST}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Interactive chatbot: Hermes Agent through A2A Daemon via Gateway"
    )
    parser.add_argument("--gateway-url", default=None,
                        help="Gateway base URL (default: http://127.0.0.1:$CONTAINER_PORT)")
    parser.add_argument("--hermes-url", default=None,
                        help="Hermes API Server base URL (health check only)")
    parser.add_argument("--token", default=None,
                        help="Pre-minted gateway JWT (else ADMIN_STATIC_TOKEN / minted from JWT_SECRET_KEY)")
    parser.add_argument("--endpoint-id", default=None,
                        help="A2A endpoint id (default: 'a2a' or from .env)")
    parser.add_argument("--part-id", default=None,
                        help="Tenant partition id, sent as Part-Id header (default: 'default')")
    parser.add_argument("--agent-uuid", default=None,
                        help="A2A agent uuid (default: A2A_DEFAULT_AGENT_UUID from .env)")
    parser.add_argument("--system", default=None,
                        help="System prompt for the agent")
    parser.add_argument("--no-sse", action="store_true",
                        help="Disable SSE streaming; use HTTP response only")
    parser.add_argument("--no-health", action="store_true",
                        help="Skip startup health checks")
    args = parser.parse_args()

    env = load_env()
    container_port = env.get("CONTAINER_PORT", "8765")
    gateway_url = (args.gateway_url or
                   f"http://127.0.0.1:{container_port}").rstrip("/")
    hermes_url = (args.hermes_url or _host_hermes_url(env)).rstrip("/")
    endpoint_id = args.endpoint_id or env.get("endpoint_id", "a2a")
    part_id = args.part_id or env.get("part_id", "default")
    agent_uuid = args.agent_uuid or env.get("A2A_DEFAULT_AGENT_UUID",
                                            "a2a-hermes-agent")
    token = resolve_token(env, args.token)

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Hermes Agent Chatbot -- A2A Daemon via Gateway{RST}")
    print(f"{B}{'=' * 70}{RST}\n")

    if not args.no_health:
        if not health_check("Gateway", gateway_url):
            return
        if not health_check("Hermes API Server", hermes_url):
            print(f"{Y}Hermes health check failed; continuing (it may be "
                  f"external or on a different host).{RST}")
    print(f"{G}OK{RST} Endpoint: {endpoint_id}/{part_id}  agent: {agent_uuid}")
    if args.system:
        print(f"{G}OK{RST} System prompt: {args.system[:60]}...")
    print()

    sse = None
    if not args.no_sse:
        sse = SSEListener(gateway_url, token, endpoint_id, part_id)
        sse.start()
        print(f"{D}SSE stream connected. Type a message and press Enter to chat.{RST}")
    else:
        print(f"{D}SSE disabled; using HTTP response only.{RST}")
    print(f"{D}Type 'quit' or 'exit' to leave. Type 'clear' to reset history.{RST}\n")

    conversation_history = []
    turn = 0

    while True:
        try:
            user_input = input(f"{B}{C}You>{RST} ")
        except (EOFError, KeyboardInterrupt):
            break

        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", ":q"):
            break
        if user_input.lower() == "clear":
            conversation_history = []
            print(f"{D}Conversation history cleared.{RST}\n")
            continue

        turn += 1
        task_id = f"chat-{uuid.uuid4().hex[:8]}"
        if sse:
            sse.set_task(task_id)

        print(f"{B}Agent>{RST} ", end="", flush=True)

        r = send_message(
            gateway_url, token, endpoint_id, part_id,
            user_input, task_id,
            agent_uuid=agent_uuid,
            system_prompt=args.system,
            conversation_history=conversation_history if conversation_history else None,
        )

        streamed_text = ""
        if sse:
            sse.done.wait(timeout=30)
            streamed_text = sse.full_text

        if not streamed_text:
            try:
                body = r.json()
                streamed_text = extract_text_from_response(body)
            except Exception:
                streamed_text = ""

        print()

        if streamed_text:
            if not (sse and sse.full_text):
                print(f"{G}{streamed_text}{RST}")
            print(f"{D}({len(streamed_text)} chars){RST}")
            conversation_history.append({"role": "user", "content": user_input})
            conversation_history.append({"role": "assistant", "content": streamed_text})
        else:
            try:
                body = r.json()
                if body.get("error"):
                    print(f"{R}Error: {body['error'].get('message', '')}{RST}")
                else:
                    print(f"{R}No response received{RST}")
            except Exception:
                print(f"{R}HTTP {r.status_code}: {r.text[:200]}{RST}")

        print()

    if sse:
        sse.stop_listening()
    print(f"\n{D}Goodbye! ({turn} turns){RST}")


if __name__ == "__main__":
    main()