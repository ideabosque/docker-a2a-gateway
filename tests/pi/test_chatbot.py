#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive chatbot test for the docker-a2a-gateway stack (pi bridge).

Opens an SSE connection for real-time streaming and lets you chat with the
pi Agent interactively. Each message you type flows through the full
pipeline:

    You (stdin)
      -> POST /{endpoint_id}/a2a  (message/send)
      -> SilvaEngine Gateway -> A2ADaemonExecutor -> Phase 10 bridge
      -> PiAgentHandler -> pi sidecar (POST /v1/chat/completions
         with stream:true -> SSE)
      -> Token chunks broadcast to SSE stream
      -> Printed here in real-time

This script is a standalone examination harness for the A2A gateway image:
it loads ./env from this directory, mints/reuses a gateway JWT, health-checks
both the gateway and the pi sidecar, then runs an interactive REPL
against the native A2A JSON-RPC surface.

Prerequisites:
    - The stack is up:   docker compose --profile postgres --profile pi up -d
    - Gateway on http://127.0.0.1:8765 (CONTAINER_PORT) and pi on 18789.
    - An pi agent registered OR the env-var fallbacks in .env set:
        A2A_AI_AGENT_TYPE=pi
        A2A_DEFAULT_AGENT_UUID=a2a-pi-agent
        PI_API_URL / PI_SECRET_KEY

Usage:
    python tests/pi/test_chatbot.py
    python tests/pi/test_chatbot.py --gateway-url http://127.0.0.1:8765
    python tests/pi/test_chatbot.py --system "You are a pirate"
    python tests/pi/test_chatbot.py --token <pre_minted_jwt>
    python tests/pi/test_chatbot.py --no-sse          # HTTP-only (no streaming)

Only third-party dependency: requests
    pip install requests

Author: bibow
"""
from __future__ import print_function

# Windows consoles default to a legacy ANSI codepage; pi replies can
# include emoji, so force UTF-8 with replacement instead of crashing.
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import sys
import uuid
from pathlib import Path

import requests

# tests/pi/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    load_env, resolve_token, resolve_config,
    health_ok, pi_sidecar_ok,
)

__author__ = "bibow"


# ---------------------------------------------------------------------------
# SSE listener
# ---------------------------------------------------------------------------
from chatbot_stream import SSEListener, interactive_request


# ---------------------------------------------------------------------------
# A2A JSON-RPC message/send
# ---------------------------------------------------------------------------
def send_message(gateway_url, token, endpoint_id, part_id, text, task_id,
                 agent_uuid, system_prompt=None, conversation_history=None,
                 thread_uuid=None, show_reasoning=None):
    """Send a message/send and return the HTTP response."""
    parts = [{"text": text}]

    metadata = {
        "operation": "task_execution",
        "agent_uuid": agent_uuid,
        "stream": True,
        "task_data": {"task_id": task_id, "task_type": "pi_chatbot"},
    }
    if system_prompt:
        metadata["system_prompt"] = system_prompt
    if conversation_history:
        metadata["conversation_history"] = conversation_history
    if thread_uuid:
        metadata["thread_uuid"] = thread_uuid
    if show_reasoning is not None:
        metadata["show_reasoning"] = bool(show_reasoning)

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
        description="Interactive chatbot: pi Agent through A2A Daemon via Gateway"
    )
    parser.add_argument("--gateway-url", default=None,
                        help="Gateway base URL (default: http://127.0.0.1:$CONTAINER_PORT)")
    parser.add_argument("--pi-url", default=None,
                        help="pi sidecar base URL (health check only)")
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
    parser.add_argument("--hide-reasoning", action="store_true",
                        help="Hide model reasoning (sends metadata.show_reasoning=false, so the gateway does not stream it)")
    args = parser.parse_args()

    env = load_env()
    cfg = resolve_config(
        env,
        bridge="pi",
        gateway_url=args.gateway_url,
        pi_url=args.pi_url,
        token=args.token,
        endpoint_id=args.endpoint_id,
        part_id=args.part_id,
        agent_uuid=args.agent_uuid,
    )

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}pi Agent Chatbot -- A2A Daemon via Gateway{RST}")
    print(f"{B}{'=' * 70}{RST}\n")

    if not args.no_health:
        if not health_ok("Gateway", cfg["gateway_url"]):
            return
        o_ok = pi_sidecar_ok(cfg["pi_url"], cfg["pi_key"])
        if not o_ok:
            print(f"{Y}pi sidecar health check failed; continuing (it may be "
                  f"external or on a different host).{RST}")
        else:
            print(f"{G}OK{RST} pi sidecar: {cfg['pi_url']}")

    print(f"{G}OK{RST} Endpoint: {cfg['endpoint_id']}/{cfg['part_id']}  agent: {cfg['agent_uuid']}")
    if args.system:
        print(f"{G}OK{RST} System prompt: {args.system[:60]}...")
    print()

    sse = None
    if not args.no_sse:
        sse = SSEListener(cfg["gateway_url"], cfg["token"],
                          cfg["endpoint_id"], cfg["part_id"])
        sse.start()
        if not sse.connected:
            print(f"{R}Cannot chat without SSE: tool permissions would be hidden.{RST}")
            return
        print(f"{D}SSE stream connected. Type a message and press Enter to chat.{RST}")
    else:
        print(f"{D}SSE disabled; using HTTP response only. Permission prompts require SSE; ask mode may time out.{RST}")
    print(f"{D}Type 'quit' or 'exit' to leave. Type 'clear' to reset history.{RST}\n")

    conversation_history = []
    turn = 0
    thread_uuid = str(uuid.uuid4())  # Persist across turns for conversation continuity

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
            thread_uuid = str(uuid.uuid4())  # New thread on clear
            print(f"{D}Conversation history cleared.{RST}\n")
            continue

        turn += 1
        task_id = f"chat-{uuid.uuid4().hex[:8]}"
        if sse:
            sse.set_task(task_id)

        print(f"{B}Agent>{RST} ", end="", flush=True)

        try:
            r = interactive_request(lambda: send_message(
                cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
                user_input, task_id,
                agent_uuid=cfg["agent_uuid"],
                system_prompt=args.system,
                conversation_history=conversation_history if conversation_history else None,
                thread_uuid=thread_uuid,
                show_reasoning=False if args.hide_reasoning else None,
            ), sse)
        except requests.RequestException as exc:
            print(f"\n{R}Request failed: {exc}{RST}")
            if sse:
                sse.sections.close()
            continue

        streamed_text = ""
        if sse:
            sse.sections.close()
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