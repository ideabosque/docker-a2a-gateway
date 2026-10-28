#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live E2E suite for the banyan bridge (a2a-protocol-plugin
<-> docker-banyanos-gateway).

Runs a sequence of checks against the running A2A gateway + Banyanos gateway:

  01  Banyanos gateway health (GET /health on docker-banyanos-gateway)
  02  A2A gateway health
  03  A2A Agent Card discovery (public, no auth)
  04  A2A core GraphQL ping (auth)
  05  Banyanos GraphQL connectivity (agent_engine_graphql, Banyanos JWT) — direct
  06  banyan_ws handshake (connection_ack frame) — direct
  07  Non-streaming message/send  -> reply text + task state
  08  tasks/get on the created task
  09  Session continuation: second turn in the same conversation
  10  Failure path: unknown agent uuid -> JSON-RPC error (not a crash)

Each step prints PASS/FAIL with a detail line. Exits non-zero if any step
failed, so it works as a CI gate.

Banyanos backend credential: BANYANOS_TOKEN (pre-minted Banyanos JWT) or
BANYANOS_EMAIL + BANYANOS_PASSWORD (Login mutation; MFA deployments must supply
a token). Backend probes (05/06) are skipped (recorded as SKIP) without one.

Usage:
    python tests/banyanos/test_gateway_live.py
    python tests/banyanos/test_gateway_live.py --skip-continuation
    python tests/banyanos/test_gateway_live.py --prompt "Count to 3."

Only third-party dependency: requests
Author: bibow
"""
from __future__ import print_function

# Windows consoles: force UTF-8 so agent replies with non-ASCII don't crash.
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
sys_path = str(Path(__file__).resolve().parent.parent)
if sys_path not in _sys.path:
    _sys.path.insert(0, sys_path)

from a2a_test_utils import (  # noqa: E402  (path bootstrap above)
    B, C, D, G, R, Y, RST,
    banyanos_gateway_ok, banyanos_graphql_ok, banyanos_login, banyanos_ws_ok,
    extract_state, extract_text, health_ok, jsonrpc_error, load_env,
    message_send_params, resolve_config, send_a2a, unwrap_response,
)

__author__ = "bibow"

_results: list = []

# These are CLI scenarios, not pytest tests requiring a cfg fixture.
__test__ = False


def _record(name: str, ok: bool, detail: str = "") -> None:
    mark = f"{G}PASS{RST}" if ok else f"{R}FAIL{RST}"
    print(f"  {mark} {name}" + (f"  {D}{detail}{RST}" if detail else ""))
    _results.append("pass" if ok else "fail")


def _skip(name: str, detail: str = "") -> None:
    mark = f"{Y}SKIP{RST}"
    print(f"  {mark} {name}" + (f"  {D}{detail}{RST}" if detail else ""))
    _results.append("skip")


def _section(title: str) -> None:
    print(f"\n{B}{title}{RST}")


def _resolve_banyan_token(cfg: dict) -> str:
    """BANYANOS_TOKEN first; else the user_engine Login mutation."""
    if cfg.get("banyanos_token"):
        return cfg["banyanos_token"]
    email = cfg.get("banyanos_email")
    password = cfg.get("banyanos_password")
    if email and password:
        return banyanos_login(
            cfg["banyanos_gateway_url"], cfg["banyanos_endpoint_id"],
            email, password, part_id=cfg.get("part_id", ""),
        )
    return ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def test_01_banyan_health(cfg):
    _section("01 — Banyanos gateway health (docker-banyanos-gateway)")
    ok = banyanos_gateway_ok(cfg["banyanos_gateway_url"])
    _record("banyan GET /health", ok,
            f"({cfg['banyanos_gateway_url']})" if ok
            else f"({cfg['banyanos_gateway_url']})")


def test_02_gateway_health(cfg):
    _section("02 — A2A gateway health")
    ok = health_ok("A2A gateway", cfg["gateway_url"])
    _record("a2a gateway /health", ok)


def test_03_agent_card(cfg):
    _section("03 — A2A Agent Card discovery (public)")
    url = f"{cfg['gateway_url']}/{cfg['endpoint_id']}/.well-known/agent-card.json"
    try:
        r = requests.get(url, headers={"Part-Id": cfg["part_id"]}, timeout=15)
        ok = r.status_code == 200
        card = {}
        if ok:
            try:
                card = r.json()
            except Exception:
                ok = False
        name = card.get("name", "") if isinstance(card, dict) else ""
        _record("agent-card.json", ok,
                f"name={name!r} http={r.status_code}" if ok
                else f"http={r.status_code} {r.text[:80]}")
    except Exception as e:
        _record("agent-card.json", False, str(e))


def test_04_graphql_ping(cfg):
    _section("04 — A2A core GraphQL ping (auth)")
    query = {"query": "{ ping }"}
    r = requests.post(
        f"{cfg['gateway_url']}/{cfg['endpoint_id']}/a2a_core_graphql",
        json=query,
        headers={
            "Authorization": f"Bearer {cfg['token']}",
            "Content-Type": "application/json",
            "Part-Id": cfg["part_id"],
        },
        timeout=30,
    )
    ok = False
    detail = f"http={r.status_code}"
    try:
        body = r.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        ok = (r.status_code == 200 and not body.get("errors")
              and isinstance(data, dict)
              and bool(data.get("ping")))
        detail = f"ping={data.get('ping')!r}"
    except Exception:
        detail = f"non-json: {r.text[:120]}"
    _record("a2a_core_graphql ping", ok, detail)


def test_05_banyan_graphql(cfg, banyanos_token):
    _section("05 — Banyanos GraphQL connectivity (agent_engine_graphql, direct)")
    if not banyanos_token:
        _skip("banyan GraphQL probe", "no BANYANOS_TOKEN / login credentials")
        return
    ok = banyanos_graphql_ok(
        cfg["banyanos_gateway_url"], cfg["banyanos_endpoint_id"],
        banyanos_token, part_id=cfg["part_id"],
    )
    _record("agent_engine_graphql schema probe", ok,
            f"({cfg['banyanos_gateway_url']}/{cfg['banyanos_endpoint_id']}/"
            f"agent_engine_graphql)")


def check_auth_required(cfg):
    r = requests.post(
        f"{cfg['gateway_url']}/{cfg['endpoint_id']}/a2a_core_graphql",
        json={"query": "{ ping }"}, headers={"Part-Id": cfg["part_id"]},
        timeout=15,
    )
    _record("protected GraphQL rejects missing credentials",
            r.status_code in (401, 403), f"http={r.status_code}")


def test_06_banyan_ws(cfg, banyanos_token):
    _section("06 — banyan_ws handshake (direct)")
    if not banyanos_token:
        _skip("banyan_ws handshake", "no BANYANOS_TOKEN / login credentials")
        return
    ws_base = cfg.get("banyanos_ws_url") or cfg["banyanos_gateway_url"]
    ok = banyanos_ws_ok(
        ws_base, cfg["banyanos_endpoint_id"], banyanos_token,
        part_id=cfg["part_id"],
    )
    _record("banyan_ws connection_ack", ok, f"({ws_base})")


def test_07_non_streaming(cfg, prompt):
    _section("07 — Non-streaming message/send")
    task_id = f"e2e-ns-{uuid.uuid4().hex[:8]}"
    params = message_send_params(
        prompt, cfg["agent_uuid"], task_id,
        task_type="banyanos_e2e_nonstream", stream=False,
    )
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id="e2e-ns-001", timeout=300,
    )
    body = unwrap_response(r)
    err = jsonrpc_error(body)
    if err:
        _record("message/send", False, f"error={err}")
        return None
    reply = extract_text(body)
    state = extract_state(body)
    reply_prefixes = ("ai agent error", "banyan auth error", "banyan protocol error",
                      "banyan transport error", "banyan execution error")
    ok = (r.status_code == 200 and bool(reply)
          and (not state or state.lower() in ("completed", "task_state_completed"))
          and not reply.lower().startswith(reply_prefixes))
    _record("message/send reply", ok,
            f"state={state!r} reply={reply[:80]!r}" if ok
            else f"raw={str(body)[:160]}")
    result = body.get("result") or {}
    return {"task_id": result.get("id") or result.get("taskId") or task_id,
            "reply": reply} if ok else None


def test_08_tasks_get(cfg, task_id):
    _section("08 — tasks/get")
    if not task_id:
        _record("tasks/get", False, "no task_id from step 07")
        return
    params = {"id": task_id, "metadata": {"agent_uuid": cfg["agent_uuid"]}}
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "tasks/get", params, request_id="e2e-get-001", timeout=60,
    )
    body = unwrap_response(r)
    err = jsonrpc_error(body)
    if err:
        _record("tasks/get", False, f"error={err}")
        return
    state = extract_state(body)
    result = body.get("result") or {}
    ok = (r.status_code == 200 and result.get("id") == task_id
          and state.lower() in ("completed", "task_state_completed"))
    _record("tasks/get", ok, f"state={state!r}")


def test_09_continuation(cfg, first_reply):
    _section("09 — Session continuation (same conversation, turn 2)")
    if not first_reply:
        _record("continuation", False, "no turn-1 reply")
        return
    followup = ("What exact phrase did I ask you to repeat in this conversation? "
                "Answer with only that phrase.")
    # Turn 1: seed the conversation with a memorable marker.
    seed = message_send_params(
        "Remember this token for the rest of our conversation: "
        "BANYAN-CONTINUATION-TOKEN-42. Reply with the token only.",
        cfg["agent_uuid"], f"e2e-cont1-{uuid.uuid4().hex[:8]}",
        task_type="banyanos_e2e_continuation", stream=False,
    )
    r1 = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", seed, request_id="e2e-cont-seed-001", timeout=300,
    )
    body1 = unwrap_response(r1)
    if jsonrpc_error(body1) or not extract_text(body1):
        _record("continuation seed turn", False, str(body1)[:160])
        return
    # Turn 2: a generic question in the SAME conversation — the client echoes
    # turn 1's returned contextId (the A2A conversation key the bridge maps
    # to the Banyanos session per tenant), and the backend must recall the
    # turn-1 marker through that session.
    turn1_context = (body1.get("result") or {}).get("contextId")
    if not turn1_context:
        _record("continuation contextId", False, "seed response has no contextId")
        return
    params2 = message_send_params(
        followup, cfg["agent_uuid"], f"e2e-cont2-{uuid.uuid4().hex[:8]}",
        task_type="banyanos_e2e_continuation", stream=False,
    )
    if turn1_context:
        params2["message"]["contextId"] = turn1_context
    r2 = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params2, request_id="e2e-cont-002", timeout=300,
    )
    body2 = unwrap_response(r2)
    if jsonrpc_error(body2):
        _record("continuation turn 2", False, f"error={jsonrpc_error(body2)}")
        return
    reply2 = extract_text(body2)
    ok = bool(reply2) and "BANYAN-CONTINUATION-TOKEN-42" in reply2
    _record("session history recalled across turns", ok,
            f"reply={reply2[:120]!r}" if reply2 else "empty")


def test_10_failure_path(cfg):
    _section("10 — Failure path (unknown agent uuid)")
    params = message_send_params(
        "This should never reach any backend.", "a2a-no-such-agent-uuid",
        f"e2e-fail-{uuid.uuid4().hex[:8]}", stream=False,
    )
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id="e2e-fail-001", timeout=120,
    )
    body = unwrap_response(r)
    err = jsonrpc_error(body)
    if err and err.get("code") in (-32001, -32004, -32602):
        _record("unknown agent -> JSON-RPC error", True,
                f"code={err.get('code')} msg={err.get('message', '')[:80]}")
        return
    # Some executor paths report failures as reply text instead; accept a
    # reply that clearly says the agent was not found.
    reply = extract_text(body)
    ok = "not found" in reply.lower() or "no agent" in reply.lower()
    _record("unknown agent -> JSON-RPC error", ok,
            f"reply={reply[:120]!r}" if reply else f"raw={str(body)[:160]}")


def main():
    _results.clear()
    parser = argparse.ArgumentParser(
        description="Live E2E suite for the banyan A2A bridge"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--banyanos-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--banyanos-token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default="Count from 1 to 3, then answer the question: 2+2=?")
    parser.add_argument("--skip-continuation", action="store_true")
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
    banyanos_token = _resolve_banyan_token(cfg)

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Banyanos bridge — live E2E "
          f"(a2a-protocol-plugin -> docker-banyanos-gateway){RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  a2a gateway:     {cfg['gateway_url']} "
          f"({cfg['endpoint_id']}/{cfg['part_id']})")
    print(f"  banyan gateway:  {cfg['banyanos_gateway_url']} "
          f"(endpoint_id={cfg['banyanos_endpoint_id']})")
    print(f"  a2a agent:       {cfg['agent_uuid']} "
          f"(banyan agent_id={cfg['banyanos_agent_id'] or '?'})")
    print(f"  banyan cred:     {'yes' if banyanos_token else 'NO (backend probes skipped)'}")

    def run(check, *values):
        try:
            return check(*values)
        except Exception as exc:
            _record(check.__name__, False, type(exc).__name__)
            return None

    for check in (test_01_banyan_health, test_02_gateway_health,
                  test_03_agent_card, test_04_graphql_ping, check_auth_required):
        run(check, cfg)
    run(test_05_banyan_graphql, cfg, banyanos_token)
    run(test_06_banyan_ws, cfg, banyanos_token)
    turn1 = run(test_07_non_streaming, cfg, args.prompt)
    run(test_08_tasks_get, cfg, turn1["task_id"] if turn1 else None)
    if not args.skip_continuation:
        run(test_09_continuation, cfg, turn1["reply"] if turn1 else None)
    else:
        _skip("continuation", "--skip-continuation")
    run(test_10_failure_path, cfg)

    passed = _results.count("pass")
    failed = _results.count("fail")
    skipped = _results.count("skip")
    total = len(_results)
    print(f"\n{B}{passed} passed, {failed} failed, {skipped} skipped ({total} checks){RST}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
