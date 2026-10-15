#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal E2E: say hello from Goose through A2A via the gateway (non-streaming).

    Client -> POST /{endpoint_id}/a2a (message/send)
    -> SilvaEngine Gateway -> A2ADaemonExecutor -> GooseAgentHandler
    -> goose serve -> reply (HTTP response only, no SSE)

Reads ./env from this directory, resolves/mints a gateway JWT, sends a short
prompt, and prints the agent's reply text. Exits non-zero on failure so it can
be used as a smoke test in scripts/CI.

Usage:
    python tests/goose/test_hello.py
    python tests/goose/test_hello.py --prompt "Introduce yourself in one sentence."
    python tests/goose/test_hello.py --token <jwt>

Only third-party dependency: requests
Author: bibow
"""
from __future__ import print_function

import argparse
import sys
import uuid
from pathlib import Path

# tests/goose/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    extract_text, jsonrpc_error, load_env, resolve_config,
    message_send_params, send_a2a, unwrap_response,
)

__author__ = "bibow"

DEFAULT_PROMPT = "Say hello and introduce yourself in one short sentence."


def main():
    parser = argparse.ArgumentParser(
        description="Minimal non-streaming E2E: say hello from Goose via A2A"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--goose-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    env = load_env()
    cfg = resolve_config(
        env,
        bridge="goose",
        gateway_url=args.gateway_url,
        goose_url=args.goose_url,
        token=args.token,
        endpoint_id=args.endpoint_id,
        part_id=args.part_id,
        agent_uuid=args.agent_uuid,
    )

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Goose Hello (non-streaming){RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:   {cfg['gateway_url']}")
    print(f"  goose:  {cfg['goose_url']}")
    print(f"  endpoint:  {cfg['endpoint_id']}/{cfg['part_id']}")
    print(f"  agent:     {cfg['agent_uuid']}")
    print(f"  prompt:    {args.prompt}")
    print()

    # Health checks
    from a2a_test_utils import health_ok
    if not args.no_health:
        if not health_ok("Gateway", cfg["gateway_url"]):
            return 1
        if not health_ok("goose serve", cfg["goose_url"]):
            print(f"  {Y}(continuing — Goose may be external){RST}")

    # message/send
    task_id = f"goose-hello-{uuid.uuid4().hex[:8]}"
    params = message_send_params(
        args.prompt, cfg["agent_uuid"], task_id,
        task_type="goose_hello", stream=False,
    )
    print(f"\n{D}POST /{cfg['endpoint_id']}/a2a  (message/send, task_id={task_id}){RST}")
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id="goose-hello-001",
        timeout=args.timeout,
    )
    print(f"  HTTP {r.status_code}")

    body = unwrap_response(r)
    err = jsonrpc_error(body)
    if err:
        print(f"{R}JSON-RPC error: {err}{RST}")
        return 3

    reply = extract_text(body)
    if not reply:
        print(f"{R}No reply text extracted. Raw:{RST}")
        print(f"  {body}")
        return 4

    # The bridge reports upstream failures (unreachable agent, auth, ...) as
    # reply text rather than a JSON-RPC error.
    if reply.startswith("AI agent error"):
        print(f"\n{R}FAIL{RST} Goose bridge error: {reply}")
        return 5

    print(f"\n{G}PASS{RST} Goose reply: {reply}")
    print(f"{D}({len(reply)} chars){RST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())