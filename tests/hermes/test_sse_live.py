#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live SSE streaming E2E for the docker-a2a-gateway stack (Hermes bridge).

Verifies the full streaming pipeline end-to-end:

  01  Hermes API server health
  02  Gateway health
  03  SSE listener connects to GET /{endpoint_id}/a2a_sse
  04  Streaming message/send -> live token chunks broadcast over SSE
  05  Final COMPLETED status received over SSE
  06  Fallback: HTTP response carries the accumulated reply when SSE misses it

Each step prints PASS/FAIL. Exits non-zero if any step failed.

Usage:
    python tests/hermes/test_sse_live.py
    python tests/hermes/test_sse_live.py --prompt "Stream a haiku about the sea."
    python tests/hermes/test_sse_live.py --timeout 200

Only third-party dependency: requests
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

# tests/hermes/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    extract_text, jsonrpc_error, load_env, resolve_config,
    health_ok, message_send_params, send_a2a, unwrap_response,
)

__author__ = "bibow"

DEFAULT_PROMPT = "Stream a short poem about the ocean, line by line."


class SSEListener:
    """Background SSE listener capturing streamed tokens + status events."""

    def __init__(self, gateway_url, token, endpoint_id, part_id):
        self.gateway_url = gateway_url
        self.token = token
        self.endpoint_id = endpoint_id
        self.part_id = part_id
        self.stop = False
        self.thread = None
        self.current_task_id = None
        self.full_text = ""
        self.chunks_seen = 0
        self.completed = False
        self.errored = False
        self.error_msg = ""
        self.connected = False
        self.connect_status = None
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
                self.connect_status = r.status_code
                self.connected = r.status_code == 200
                if not self.connected:
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
                                    if local_text and (
                                        text == local_text
                                        or text.startswith(local_text)
                                        or local_text.startswith(text)
                                    ):
                                        pass
                                    else:
                                        self.chunks_seen += 1
                                        local_text += text
                                        self.full_text = local_text
                        if etype in ("task_status", "status"):
                            state = event.get("state", event.get("status", ""))
                            if state in ("completed", "COMPLETED"):
                                self.completed = True
                                self.done.set()
                            if state in ("failed", "FAILED", "canceled", "CANCELED"):
                                self.done.set()
                        if etype == "error" or "error" in str(event).lower()[:20]:
                            err = event.get("error", event.get("message", ""))
                            if err:
                                self.errored = True
                                self.error_msg = str(err)
                                self.done.set()
            except requests.exceptions.ConnectionError:
                pass
            except Exception as e:
                if not self.stop:
                    self.error_msg = str(e)
                    self.errored = True
                    self.done.set()

        self.thread = threading.Thread(target=_listen, daemon=True)
        self.thread.start()
        time.sleep(1)

    def set_task(self, task_id):
        self.current_task_id = task_id
        self.full_text = ""
        self.chunks_seen = 0
        self.completed = False
        self.errored = False
        self.error_msg = ""
        self.done.clear()
        self._turn += 1
        self._active_turn = self._turn

    def stop_listening(self):
        self.stop = True


_results: list = []


def _record(name, ok, detail=""):
    mark = f"{G}PASS{RST}" if ok else f"{R}FAIL{RST}"
    print(f"  {mark} {name}" + (f"  {D}{detail}{RST}" if detail else ""))
    _results.append(ok)


def _section(title):
    print(f"\n{B}{title}{RST}")


def main():
    parser = argparse.ArgumentParser(
        description="Live SSE streaming E2E for the A2A Hermes gateway stack"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--hermes-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("--timeout", type=int, default=200,
                        help="Max seconds to wait for the stream to complete")
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

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Hermes SSE Live Streaming E2E{RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:   {cfg['gateway_url']}")
    print(f"  hermes:    {cfg['hermes_url']}")
    print(f"  endpoint:  {cfg['endpoint_id']}/{cfg['part_id']}")
    print(f"  agent:     {cfg['agent_uuid']}")
    print(f"  prompt:    {args.prompt}")

    if not args.no_health:
        _section("01 — Hermes API server health")
        h_ok = health_ok("Hermes API Server", cfg["hermes_url"])
        _record("hermes /health", h_ok)
        _section("02 — Gateway health")
        g_ok = health_ok("Gateway", cfg["gateway_url"])
        _record("gateway /health", g_ok)
        if not g_ok:
            print(f"\n{R}Gateway down — aborting.{RST}")
            return 1

    # Start SSE listener
    sse = SSEListener(cfg["gateway_url"], cfg["token"],
                      cfg["endpoint_id"], cfg["part_id"])
    sse.start()

    _section("03 — SSE listener connect")
    _record("GET /a2a_sse connected", sse.connected,
            f"http={sse.connect_status}")

    _section("04 — Streaming message/send -> live token chunks")
    task_id = f"sse-{uuid.uuid4().hex[:8]}"
    sse.set_task(task_id)
    print(f"{B}Agent>{RST} ", end="", flush=True)
    params = message_send_params(
        args.prompt, cfg["agent_uuid"], task_id,
        task_type="hermes_sse_live", stream=True,
    )
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id=f"sse-{task_id}", timeout=240,
    )
    sse.done.wait(timeout=args.timeout)
    print()

    streamed_text = sse.full_text
    _record("received streamed tokens", bool(streamed_text),
            f"chunks={sse.chunks_seen} chars={len(streamed_text)}")

    _section("05 — COMPLETED status over SSE")
    if sse.errored:
        _record("stream completed", False, f"error={sse.error_msg}")
    else:
        _record("COMPLETED status received", sse.completed,
                "completed=True" if sse.completed else "no terminal status (timeout?)")

    _section("06 — HTTP fallback reply")
    body = unwrap_response(r)
    err = jsonrpc_error(body)
    http_reply = extract_text(body)
    if err:
        _record("HTTP response", False, f"error={err}")
    else:
        _record("HTTP response carries reply", bool(http_reply),
                f"chars={len(http_reply)}" if http_reply else "empty (relied on SSE)")

    sse.stop_listening()

    # Final
    print(f"\n{B}{'=' * 70}{RST}")
    if streamed_text:
        print(f"{G}Streamed reply:{RST}\n{streamed_text}")
        print(f"{D}({len(streamed_text)} chars){RST}")
    elif http_reply:
        print(f"{Y}HTTP reply (SSE missed):{RST}\n{http_reply}")

    passed = sum(1 for x in _results if x)
    total = len(_results)
    mark = G if passed == total else R
    print(f"\n{mark}RESULT: {passed}/{total} passed{RST}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())