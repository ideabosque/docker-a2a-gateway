#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal E2E: say hello from the Banyanos agent through A2A (non-streaming).

    Client -> POST /{endpoint_id}/a2a (message/send)
    -> SilvaEngine Gateway -> A2ADaemonExecutor -> BanyanosAgentHandler
    -> docker-banyanos-gateway (agent_engine_graphql: createSession + SendMessage)
    -> reply (HTTP response only, no SSE)

Reads ./env from this directory, resolves/mints a gateway JWT, resolves the
Banyanos backend credential (BANYANOS_TOKEN, or BANYANOS_EMAIL/BANYANOS_PASSWORD
login), sends a short prompt, and prints the agent's reply text. Exits
non-zero on failure so it can be used as a smoke test in scripts/CI.

Usage:
    python tests/banyanos/test_hello.py
    python tests/banyanos/test_hello.py --prompt "Introduce yourself in one sentence."
    python tests/banyanos/test_hello.py --skip-banyanos-probes   # A2A call only

Only third-party dependency: requests
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


# tests/banyanos/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    banyanos_gateway_ok, banyanos_graphql_ok, banyanos_login, banyanos_ws_ok,
    extract_text, jsonrpc_error, load_env, resolve_config,
    health_ok, message_send_params, send_a2a, unwrap_response,
)

__author__ = "bibow"

DEFAULT_PROMPT = "Say hello and introduce yourself in one short sentence."


def resolve_banyanos_token(cfg: dict) -> str:
    """BANYANOS_TOKEN first; else the Login mutation (MFA deployments must
    supply a pre-minted token)."""
    if cfg["banyanos_token"]:
        return cfg["banyanos_token"]
    email = cfg.get("banyanos_email")
    password = cfg.get("banyanos_password")
    if email and password:
        return banyanos_login(
            cfg["banyanos_gateway_url"], cfg["banyanos_endpoint_id"],
            email, password, part_id=cfg.get("part_id", ""),
        )
    print(f"{Y}  ! No BANYANOS_TOKEN / BANYANOS_EMAIL+BANYANOS_PASSWORD — "
          f"backend probes will be skipped{RST}")
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Minimal non-streaming E2E: say hello from the Banyanos agent via A2A"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--banyanos-url", default=None)
    parser.add_argument("--token", default=None, help="gateway JWT")
    parser.add_argument("--banyanos-token", default=None, help="Banyanos JWT")
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("--skip-banyanos-probes", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    env = load_env()
    cfg = resolve_config(
        env,
        bridge="banyanos",
        gateway_url=args.gateway_url,
        banyanos_gateway_url=args.banyanos_url,
        token=args.token,
        banyanos_token=args.banyanos_token,
        endpoint_id=args.endpoint_id,
        part_id=args.part_id,
        agent_uuid=args.agent_uuid,
    )
    banyanos_token = resolve_banyanos_token(cfg)

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Banyanos Hello (non-streaming) — docker-banyanos-gateway "
          f"backend{RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:        {cfg['gateway_url']}")
    print(f"  banyanos gw:    {cfg['banyanos_gateway_url']} "
          f"(endpoint_id={cfg['banyanos_endpoint_id']})")
    print(f"  endpoint:       {cfg['endpoint_id']}/{cfg['part_id']}")
    print(f"  agent:          {cfg['agent_uuid']} "
          f"(banyanos agent_id={cfg['banyanos_agent_id'] or '?'})")
    print(f"  banyanos cred:  {'yes' if banyanos_token else 'NO'}")
    print(f"  prompt:         {args.prompt}")
    print()

    # Health checks
    if not args.no_health:
        if not health_ok("Gateway", cfg["gateway_url"]):
            return 1
        if not args.skip_banyanos_probes:
            if not banyanos_gateway_ok(cfg["banyanos_gateway_url"]):
                print(f"{Y}(continuing — banyanos gateway may be external){RST}")
            if banyanos_token:
                banyanos_graphql_ok(
                    cfg["banyanos_gateway_url"], cfg["banyanos_endpoint_id"],
                    banyanos_token, part_id=cfg["part_id"],
                )
                ws_base = cfg.get("banyanos_ws_url") or cfg["banyanos_gateway_url"]
                banyanos_ws_ok(
                    ws_base, cfg["banyanos_endpoint_id"], banyanos_token,
                    part_id=cfg["part_id"],
                )

    # message/send
    task_id = f"banyanos-hello-{uuid.uuid4().hex[:8]}"
    params = message_send_params(
        args.prompt, cfg["agent_uuid"], task_id,
        task_type="banyanos_hello", stream=False,
    )
    print(f"\n{D}POST /{cfg['endpoint_id']}/a2a  (message/send, task_id={task_id}){RST}")
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id="banyanos-hello-001",
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
    lowered = reply.lower()
    if lowered.startswith(("ai agent error", "banyanos auth error", "banyanos protocol error",
                           "banyanos transport error", "banyanos execution error")):
        print(f"\n{R}FAIL{RST} banyanos bridge error: {reply}")
        return 5

    print(f"\n{G}PASS{RST} banyanos reply: {reply}")
    print(f"{D}({len(reply)} chars){RST}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)