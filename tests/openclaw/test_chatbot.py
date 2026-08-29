#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive chatbot test for the docker-a2a-gateway stack (OpenClaw bridge).

Opens an SSE connection for real-time streaming and lets you chat with the
OpenClaw Agent interactively. Each message you type flows through the full
pipeline:

    You (stdin)
      -> POST /{endpoint_id}/a2a  (message/send)
      -> SilvaEngine Gateway -> A2ADaemonExecutor -> Phase 10 bridge
      -> OpenClawAgentHandler -> OpenClaw Gateway (POST /v1/chat/completions
         with stream:true -> SSE)
      -> Token chunks broadcast to SSE stream
      -> Printed here in real-time

This script is a standalone examination harness for the A2A gateway image:
it loads ./env from this directory, mints/reuses a gateway JWT, health-checks
both the gateway and the OpenClaw Gateway, then runs an interactive REPL
against the native A2A JSON-RPC surface.

Prerequisites:
    - The stack is up:   docker compose --profile postgres --profile openclaw up -d
    - Gateway on http://127.0.0.1:8765 (CONTAINER_PORT) and OpenClaw on 18789.
    - An openclaw agent registered OR the env-var fallbacks in .env set:
        A2A_AI_AGENT_TYPE=openclaw
        A2A_DEFAULT_AGENT_UUID=a2a-openclaw-agent
        OPENCLAW_API_URL / OPENCLAW_API_KEY

Usage:
    python tests/openclaw/test_chatbot.py
    python tests/openclaw/test_chatbot.py --gateway-url http://127.0.0.1:8765
    python tests/openclaw/test_chatbot.py --system "You are a pirate"
    python tests/openclaw/test_chatbot.py --token <pre_minted_jwt>
    python tests/openclaw/test_chatbot.py --no-sse          # HTTP-only (no streaming)

Only third-party dependency: requests
    pip install requests

Author: bibow
"""
from __future__ import print_function

import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

# tests/openclaw/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    load_env, resolve_token, resolve_config,
    health_ok, openclaw_models_ok,
)

__author__ = "bibow"


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
        "task_data": {"task_id": task_id, "task_type": "openclaw_chatbot"},
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
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Interactive chatbot: OpenClaw Agent through A2A Daemon via Gateway"
    )
    parser.add_argument("--gateway-url", default=None,
                        help="Gateway base URL (default: http://127.0.0.1:$CONTAINER_PORT)")
    parser.add_argument("--openclaw-url", default=None,
                        help="OpenClaw Gateway base URL (health check only)")
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
    cfg = resolve_config(
        env,
        gateway_url=args.gateway_url,
        openclaw_url=args.openclaw_url,
        token=args.token,
        endpoint_id=args.endpoint_id,
        part_id=args.part_id,
        agent_uuid=args.agent_uuid,
    )

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}OpenClaw Agent Chatbot -- A2A Daemon via Gateway{RST}")
    print(f"{B}{'=' * 70}{RST}\n")

    if not args.no_health:
        if not health_ok("Gateway", cfg["gateway_url"]):
            return
        o_ok = openclaw_models_ok(cfg["openclaw_url"], cfg["openclaw_key"])
        if not o_ok:
            print(f"{Y}OpenClaw health check failed; continuing (it may be "
                  f"external or on a different host).{RST}")
        else:
            print(f"{G}OK{RST} OpenClaw Gateway: {cfg['openclaw_url']}")

    print(f"{G}OK{RST} Endpoint: {cfg['endpoint_id']}/{cfg['part_id']}  agent: {cfg['agent_uuid']}")
    if args.system:
        print(f"{G}OK{RST} System prompt: {args.system[:60]}...")
    print()

    sse = None
    if not args.no_sse:
        sse = SSEListener(cfg["gateway_url"], cfg["token"],
                          cfg["endpoint_id"], cfg["part_id"])
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
            cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
            user_input, task_id,
            agent_uuid=cfg["agent_uuid"],
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