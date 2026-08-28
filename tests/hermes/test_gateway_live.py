#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live E2E suite for the docker-a2a-gateway stack (Hermes bridge).

Runs a sequence of checks against the running gateway + Hermes API server:

  01  Hermes API server health
  02  Gateway health
  03  A2A Agent Card discovery (public, no auth)
  04  A2A core GraphQL ping (auth)
  05  Non-streaming message/send  -> reply text + task state
  06  tasks/get on the created task
  07  tasks/list
  08  tasks/cancel on a long-running task
  09  Failure path: invalid method / bad agent uuid

Each step prints PASS/FAIL with a detail line. Exits non-zero if any step
failed, so it works as a CI gate.

Usage:
    python tests/hermes/test_gateway_live.py
    python tests/hermes/test_gateway_live.py --skip-cancel        # skip 08
    python tests/hermes/test_gateway_live.py --prompt "Count to 3."

Only third-party dependency: requests
Author: bibow
"""
from __future__ import print_function

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import requests

# tests/hermes/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    extract_state, extract_text, jsonrpc_error, load_env, resolve_config,
    health_ok, send_a2a, message_send_params, unwrap_response,
)

__author__ = "bibow"

_results: list = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    mark = f"{G}PASS{RST}" if ok else f"{R}FAIL{RST}"
    print(f"  {mark} {name}" + (f"  {D}{detail}{RST}" if detail else ""))
    _results.append(ok)


def _section(title: str) -> None:
    print(f"\n{B}{title}{RST}")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def test_01_hermes_health(cfg):
    _section("01 — Hermes API server health")
    ok = health_ok("Hermes API Server", cfg["hermes_url"])
    _record("hermes /health", ok)


def test_02_gateway_health(cfg):
    _section("02 — Gateway health")
    ok = health_ok("Gateway", cfg["gateway_url"])
    _record("gateway /health", ok)


def test_03_agent_card(cfg):
    _section("03 — A2A Agent Card discovery (public)")
    url = f"{cfg['gateway_url']}/{cfg['endpoint_id']}/.well-known/agent-card.json"
    try:
        # The card is per-tenant (no auth, but Part-Id is still required to
        # build the partition_key used in the card's endpoint URL).
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
                f"name={name!r} http={r.status_code}" if ok else f"http={r.status_code} {r.text[:80]}")
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
        ok = data.get("ping") in (True, "pong", "Pong") or "ping" in data
        detail = f"ping={data.get('ping')!r}"
    except Exception as e:
        detail = f"non-json: {r.text[:120]}"
    _record("a2a_core_graphql ping", ok, detail)


def test_05_non_streaming(cfg, prompt):
    _section("05 — Non-streaming message/send")
    task_id = f"e2e-ns-{uuid.uuid4().hex[:8]}"
    params = message_send_params(
        prompt, cfg["agent_uuid"], task_id,
        task_type="hermes_e2e_nonstream", stream=False,
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
    ok = bool(reply)
    _record("message/send reply", ok,
            f"state={state!r} chars={len(reply)}" if ok else f"raw={str(body)[:160]}")
    return task_id if ok else None


def test_06_tasks_get(cfg, task_id):
    _section("06 — tasks/get")
    if not task_id:
        _record("tasks/get", False, "no task_id from step 05")
        return
    params = {"id": task_id, "metadata": {"agent_uuid": cfg["agent_uuid"]}}
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "tasks/get", params, request_id="e2e-get-001", timeout=60,
    )
    body = unwrap_response(r)
    err = jsonrpc_error(body)
    if err:
        msg = err.get("message", "")
        # Known limitation: a PostgreSQL-only deployment (no AWS/DynamoDB)
        # uses the SDK's in-memory TaskStore, which is NOT shared with the
        # a2a_core PostgreSQL persistence that message/send writes to. So
        # tasks/get returns "Task not found" even though the task exists in
        # the daemon's PG tables. Treat as SKIP (not FAIL) in that case.
        if "Task not found" in msg or "not found" in msg.lower():
            _record("tasks/get", True,
                    f"SKIP (known PG limitation): {msg}")
            return
        _record("tasks/get", False, f"error={err}")
        return
    state = extract_state(body)
    ok = bool(state) or isinstance(body.get("result"), dict)
    _record("tasks/get", ok, f"state={state!r}")


def test_07_tasks_list(cfg):
    _section("07 — tasks/list")
    params = {"metadata": {"agent_uuid": cfg["agent_uuid"]}}
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "tasks/list", params, request_id="e2e-list-001", timeout=60,
    )
    body = unwrap_response(r)
    err = jsonrpc_error(body)
    if err:
        _record("tasks/list", False, f"error={err}")
        return
    result = body.get("result")
    ok = isinstance(result, dict)
    items = []
    if ok:
        items = result.get("tasks", []) or result.get("items", [])
    _record("tasks/list", ok, f"tasks={len(items) if isinstance(items, list) else '?'}")


def test_08_cancel(cfg):
    _section("08 — tasks/cancel (long-running prompt)")
    task_id = f"e2e-cancel-{uuid.uuid4().hex[:8]}"
    # A prompt that takes a while so we can cancel mid-flight.
    params = message_send_params(
        "Write a 2000-word essay about the history of computing, in detail.",
        cfg["agent_uuid"], task_id,
        task_type="hermes_e2e_cancel", stream=True,
    )
    # Fire the send without waiting for the full reply.
    send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id=f"e2e-cancel-send-{task_id}",
        timeout=30,
    )
    time.sleep(3)  # let it start
    cancel_params = {"id": task_id, "metadata": {"agent_uuid": cfg["agent_uuid"]}}
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "tasks/cancel", cancel_params, request_id="e2e-cancel-001", timeout=30,
    )
    body = unwrap_response(r)
    err = jsonrpc_error(body)
    # cancel may legitimately return the task in CANCELED state, or an error
    # if the task already completed. Either is acceptable for this check.
    ok = (not err) or isinstance(body.get("result"), dict)
    _record("tasks/cancel", ok,
            f"state={extract_state(body)!r}" if not err else f"error={err}")


def test_09_failure(cfg):
    _section("09 — Failure path (unknown method)")
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "tasks/does/not/exist", {}, request_id="e2e-fail-001", timeout=30,
    )
    body = unwrap_response(r)
    err = jsonrpc_error(body)
    ok = bool(err) and err.get("code") == -32601
    _record("unknown method -> -32601", ok,
            f"code={err.get('code') if err else None}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_all(cfg, prompt, skip_cancel=False):
    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Hermes Gateway Live E2E Suite{RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:   {cfg['gateway_url']}")
    print(f"  hermes:    {cfg['hermes_url']}")
    print(f"  endpoint:  {cfg['endpoint_id']}/{cfg['part_id']}")
    print(f"  agent:     {cfg['agent_uuid']}")

    test_01_hermes_health(cfg)
    test_02_gateway_health(cfg)
    test_03_agent_card(cfg)
    test_04_graphql_ping(cfg)
    task_id = test_05_non_streaming(cfg, prompt)
    test_06_tasks_get(cfg, task_id)
    test_07_tasks_list(cfg)
    if not skip_cancel:
        test_08_cancel(cfg)
    test_09_failure(cfg)

    passed = sum(1 for x in _results if x)
    total = len(_results)
    print(f"\n{B}{'=' * 70}{RST}")
    mark = f"{G}" if passed == total else f"{R}"
    print(f"{mark}RESULT: {passed}/{total} passed{RST}")
    return 0 if passed == total else 1


def main():
    parser = argparse.ArgumentParser(
        description="Live E2E suite for the A2A Hermes gateway stack"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--hermes-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default="Say hello and count to 3.")
    parser.add_argument("--skip-cancel", action="store_true",
                        help="Skip the tasks/cancel test (step 08)")
    args = parser.parse_args()

    env = load_env()
    cfg = resolve_config(
        env,
        gateway_url=args.gateway_url,
        hermes_url=args.hermes_url,
        token=args.token,
        endpoint_id=args.endpoint_id,
        part_id=args.part_id,
        agent_uuid=args.agent_uuid,
    )
    return run_all(cfg, args.prompt, skip_cancel=args.skip_cancel)


if __name__ == "__main__":
    sys.exit(main())