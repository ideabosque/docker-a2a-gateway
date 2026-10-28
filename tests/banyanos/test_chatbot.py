#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive chatbot test for the docker-a2a-gateway stack (Banyanos bridge).

Opens an SSE connection for real-time streaming and lets you chat with the
Banyanos agent interactively. Each message you type flows through the full
pipeline:

    You (stdin)
      -> POST /{endpoint_id}/a2a  (message/send)
      -> SilvaEngine Gateway -> A2ADaemonExecutor -> BanyanosAgentHandler
      -> docker-banyanos-gateway (agent_engine_graphql createSession +
         invokeAgent; banyan_ws agentInvokeStream when streaming)
      -> Token chunks broadcast to the A2A SSE stream -> printed real-time

Conversation continuity is handled by the bridge: it maps this chat's A2A
contextId to a Banyanos session per tenant (a2a_settings), so no client-side
history replay or explicit thread_uuid is needed (unlike the core-engine or
pi bridges). 'clear' deletes the local history; the Banyanos session mapping
follows the new contextId on the next turn.

This script is a standalone examination harness for the A2A gateway image:
it loads ./env from this directory, mints/reuses a gateway JWT, health-checks
the gateway and the Banyanos backend, then runs an interactive REPL against
the native A2A JSON-RPC surface.

Prerequisites:
    - The A2A stack is up:   docker compose --profile postgres up -d
    - docker-banyanos-gateway running and reachable (BANYANOS_GRAPHQL_URL /
      BANYANOS_WS_URL / BANYANOS_TOKEN / BANYANOS_AGENT_ID in .env).
    - A Banyanos agent registered (scripts/seed_agents.py seeds
      a2a-banyanos-agent with agent_type banyanos_agent).

Usage:
    python tests/banyanos/test_chatbot.py
    python tests/banyanos/test_chatbot.py --gateway-url http://127.0.0.1:8775
    python tests/banyanos/test_chatbot.py --system "You are a pirate"
    python tests/banyanos/test_chatbot.py --token <pre_minted_jwt>
    python tests/banyanos/test_chatbot.py --no-sse          # HTTP-only

Only third-party dependency: requests
    pip install requests

Author: bibow
"""
from __future__ import print_function

# Windows consoles default to a legacy ANSI codepage; agent replies can
# include emoji, so force UTF-8 with replacement instead of crashing.
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import sys
import uuid
from pathlib import Path

import requests

# tests/banyanos/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    banyanos_gateway_ok,
    load_env, resolve_config,
    health_ok,
)

__author__ = "bibow"


# ---------------------------------------------------------------------------
# SSE listener (shared harness; permission queue unused by Banyanos — the
# backend's ReAct loop runs server-side without A2A permission prompts)
# ---------------------------------------------------------------------------
from chatbot_stream import SSEListener


# ---------------------------------------------------------------------------
# A2A JSON-RPC message/send
# ---------------------------------------------------------------------------
def send_message(gateway_url, token, endpoint_id, part_id, text, task_id,
                 agent_uuid, system_prompt=None, conversation_history=None,
                 context_id=None, show_reasoning=None):
    """Send a message/send and return the HTTP response.

    ``context_id`` (optional) is the conversation key echoed from the
    previous reply's ``contextId`` — continuing that conversation.

    Banyanos-only contract notes (vs the other bridges):
    - ``conversation_history`` is NOT sent: the Banyanos backend owns the
      conversation history for the mapped session — replaying client-side
      history would double-load it.
    - ``thread_uuid`` is NOT sent: the backend session is resolved by the
      handler from the A2A contextId via the per-tenant mapping; the contextId
      itself is never sent to the backend.
    - ``system_prompt`` is not forwarded (the published Banyanos agent's own
      system prompt is authoritative).
    """
    metadata = {
        "operation": "task_execution",
        "agent_uuid": agent_uuid,
        "stream": True,
        "task_data": {"task_id": task_id, "task_type": "banyanos_chatbot"},
    }
    if show_reasoning is not None:
        metadata["show_reasoning"] = bool(show_reasoning)
    conversation_id = context_id or str(uuid.uuid4())
    # A fresh chat mints a conversation id; later turns echo the previous
    # reply contextId so the bridge maps conversation -> Banyanos session.
    return requests.post(
        f"{gateway_url}/{endpoint_id}/a2a",
        json={
            "jsonrpc": "2.0",
            "method": "message/send",
            "params": {
                "message": {"role": "ROLE_USER", "parts": [{"text": text}],
                            "contextId": conversation_id},
                "metadata": metadata,
            },
            "id": f"banyanos-{task_id}",
        },
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
    elif isinstance(result, str):
        return result
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Interactive chatbot: Banyanos agent through A2A Daemon via Gateway"
    )
    parser.add_argument("--gateway-url", default=None,
                        help="Gateway base URL (default: http://127.0.0.1:$CONTAINER_PORT)")
    parser.add_argument("--banyanos-url", default=None,
                        help="Banyanos gateway base URL (health check only)")
    parser.add_argument("--token", default=None,
                        help="Pre-minted gateway JWT (else ADMIN_STATIC_TOKEN / minted from JWT_SECRET_KEY)")
    parser.add_argument("--endpoint-id", default=None,
                        help="A2A endpoint id (default: 'a2a' or from .env)")
    parser.add_argument("--part-id", default=None,
                        help="Tenant partition id, sent as Part-Id header (default: 'default')")
    parser.add_argument("--agent-uuid", default=None,
                        help="A2A agent uuid (default: A2A_BANYANOS_AGENT_UUID from .env)")
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
        bridge="banyanos",
        gateway_url=args.gateway_url,
        banyanos_gateway_url=args.banyanos_url,
        token=args.token,
        endpoint_id=args.endpoint_id,
        part_id=args.part_id,
        agent_uuid=args.agent_uuid,
    )

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Banyanos Agent Chatbot -- A2A Daemon via Gateway{RST}")
    print(f"{B}{'=' * 70}{RST}\n")

    if not args.no_health:
        if not health_ok("A2A gateway", cfg["gateway_url"]):
            return
        if banyanos_gateway_ok(cfg["banyanos_gateway_url"]):
            print(f"{G}OK{RST} Banyanos gateway: {cfg['banyanos_gateway_url']}")
        else:
            print(f"{Y}Banyanos gateway health check failed; continuing (it may be "
                  f"external or on a different host).{RST}")

    print(f"{G}OK{RST} Endpoint: {cfg['endpoint_id']}/{cfg['part_id']}  agent: {cfg['agent_uuid']}")
    print()

    sse = None
    if not args.no_sse:
        sse = SSEListener(cfg["gateway_url"], cfg["token"],
                          cfg["endpoint_id"], cfg["part_id"])
        sse.start()
        if not sse.connected:
            print(f"{R}Cannot establish SSE connection.{RST}")
            return
        print(f"{D}SSE stream connected. Type a message and press Enter to chat.{RST}")
    else:
        print(f"{D}SSE disabled; using HTTP response only.{RST}")
    print(f"{D}Type 'quit' or 'exit' to leave. Type 'clear' to reset history.{RST}\n")

    conversation_history = []  # local display only; the backend owns history
    conversation_id = None   # echoed reply contextId (A2A conversation key)
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
            print(f"{D}Local history cleared (Banyanos keeps its own session "
                  f"memory — ask it to start fresh if needed).{RST}\n")
            continue

        turn += 1
        task_id = f"banyanos-chat-{uuid.uuid4().hex[:8]}"
        if sse:
            sse.set_task(task_id)

        print(f"{B}Agent>{RST} ", end="", flush=True)

        try:
            r = send_message(
                cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
                user_input, task_id,
                agent_uuid=cfg["agent_uuid"],
                context_id=conversation_id,
                show_reasoning=False if args.hide_reasoning else None,
            )
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
            try:
                conversation_id = ((r.json().get("result") or {}).get("contextId")
                                   or conversation_id)
            except Exception:
                pass
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
    print(f"{D}Bye!{RST}")


if __name__ == "__main__":
    main()