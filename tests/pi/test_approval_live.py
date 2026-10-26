#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live E2E: interactive tool approval for the pi bridge (test_approval_live.py).

Prerequisites (bundled sidecar + the tool-approval extension):
    - `pi` compose profile UP; the sidecar's PI_SIDE_APPROVE_TOOLS gates
      bash/edit/write (extension installed into the agent dir by the
      entrypoint)
    - the a2a-pi-agent registered with metadata pi_permission_mode=ask

Flow per scenario (approve / deny):
    1. Stream a prompt that runs the bash tool (echo pi-approval-<token>).
    2. The dialog arrives as an extension_ui_request → the bridge emits
       an `approval` chunk → the executor parks the task INPUT_REQUIRED →
       a `task_permission` event arrives over the gateway SSE stream.
    3. Answer via message/send metadata.operation="approval_response"
       (approved=true|false).
    4. pi proceeds (runs the tool) or the tool is blocked; the task settles
       COMPLETED either way — only the tool's output differs.

Assertions: approved scenario sees the token in the reply; denied scenario
completes WITHOUT running it. Exit non-zero on failure.

Usage:
    python tests/pi/test_approval_live.py
    python tests/pi/test_approval_live.py --tool bash

Only third-party dependency: requests
Author: bibow
"""
from __future__ import print_function

import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import sys
import time
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    load_env, resolve_config, health_ok,
    message_send_params, send_a2a, unwrap_response, extract_text,
)
from chatbot_stream import SSEListener, answer_permission

__author__ = "bibow"

_results: list = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    mark = f"{G}PASS{RST}" if ok else f"{R}FAIL{RST}"
    print(f"  {mark} {name}" + (f"  {D}{detail}{RST}" if detail else ""))
    _results.append(ok)


def _section(title: str) -> None:
    print(f"\n{B}{title}{RST}")


def run_scenario(cfg, approved: bool, label: str) -> None:
    """One approval scenario: prompt → permission → answer → settle."""
    token = uuid.uuid4().hex[:6]
    command = f"echo pi-approval-{token}"
    prompt = (
        f"Use the bash tool to run exactly: {command}\n"
        "Then reply with the command's exact output and nothing else."
    )
    task_id = f"pi-approval-{uuid.uuid4().hex[:8]}"
    thread_uuid = f"approval-{uuid.uuid4().hex[:8]}"

    _section(label)
    sse = SSEListener(cfg["gateway_url"], cfg["token"],
                      cfg["endpoint_id"], cfg["part_id"])
    sse.start()
    sse.set_task(task_id)

    params = message_send_params(
        prompt, cfg["agent_uuid"], task_id,
        task_type="pi_approval_live", stream=True,
        thread_uuid=thread_uuid,
    )

    send_result = {}

    def _send():
        try:
            send_result["response"] = send_a2a(
                cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                cfg["part_id"], "message/send", params,
                request_id=f"approval-{task_id}", timeout=200,
            )
        except Exception as e:
            send_result["error"] = str(e)

    sender = _sys.modules.setdefault("threading", sys.modules["threading"])
    import threading
    t = threading.Thread(target=_send, daemon=True)
    t.start()

    # Wait for the permission prompt (the tool gate fires within ~seconds).
    permission = None
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            permission = sse.permissions.get(timeout=2)
            break
        except Exception:
            if send_result.get("error"):
                break
            # a COMPLETED before any prompt would mean the tool ran ungated
            if sse.done.is_set() and not sse.permissions.qsize():
                break
    if permission is None:
        _record(f"{label.split('—')[0].strip()}: permission prompt received", False,
                "run completed without asking (gate not active?)")
        sse.stop_listening()
        return

    details_blob = str(permission.get("details") or "")
    detail = (permission.get("message") or permission.get("title")
              or details_blob)
    _record("permission prompt received", True,
            f"task={permission.get('task_id')} detail={str(detail)[:80]!r}")
    _record("prompt carries the tool input", command in str(detail),
            f"looking for {command!r} in {str(detail)[:120]!r}")

    answer_permission(sse, permission, approved)
    _record("approval_response sent", True, f"approved={approved}")

    sse.done.wait(timeout=180)
    sse.sections.close()
    t.join(timeout=200)

    body = send_result.get("response")
    final_text = ""
    if body is not None:
        final_text = extract_text(unwrap_response(body))
    streamed = sse.full_text
    completed = sse.done.is_set() and not send_result.get("error")

    if approved:
        _record("approved: tool ran and result in reply",
                completed and (command in (final_text + streamed)
                               or token in (final_text + streamed)),
                f"token={token} chars={len(final_text or streamed)}")
    else:
        _record("denied: task completed WITHOUT the tool output",
                completed and token not in (final_text or "") + (streamed or ""),
                f"completed={completed} (block reason reached the model)")
    sse.stop_listening()


def main():
    parser = argparse.ArgumentParser(
        description="Live E2E: interactive tool approval for the pi bridge")
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--pi-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--no-health", action="store_true")
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
    print(f"{C}pi Tool-Approval Live E2E (extension gate + ask mode){RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:   {cfg['gateway_url']}")
    print(f"  pi:      {cfg['pi_url']}")
    print(f"  agent:     {cfg['agent_uuid']} (metadata pi_permission_mode=ask?)")

    if not args.no_health:
        from a2a_test_utils import pi_sidecar_ok
        if not health_ok("Gateway", cfg["gateway_url"]):
            return 1
        if not pi_sidecar_ok(cfg["pi_url"], cfg["pi_key"]):
            print(f"  {Y}(continuing — pi sidecar may be external){RST}")

    run_scenario(cfg, approved=True, label="Scenario A — APPROVE")
    run_scenario(cfg, approved=False, label="Scenario B — DENY")

    passed = sum(1 for x in _results if x)
    total = len(_results)
    print(f"\n{B}{'=' * 70}{RST}")
    mark = f"{G}" if passed == total else f"{R}"
    print(f"{mark}RESULT: {passed}/{total} passed{RST}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())