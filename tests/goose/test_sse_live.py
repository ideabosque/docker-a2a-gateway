#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live SSE streaming E2E for the docker-a2a-gateway stack (Goose bridge).

Verifies the full streaming pipeline end-to-end:

  01  goose serve health (/v1/models)
  02  Gateway health
  03  SSE listener connects to GET /{endpoint_id}/a2a_sse
  04  Streaming message/send -> live token chunks broadcast over SSE
  05  Final COMPLETED status received over SSE
  06  Fallback: HTTP response carries the accumulated reply when SSE misses it
  07  Direct goose serve ACP streaming verification (initialize /
      session/new / session/prompt over the native transport)

Each step prints PASS/FAIL. Exits non-zero if any step failed.

Usage:
    python tests/goose/test_sse_live.py
    python tests/goose/test_sse_live.py --prompt "Stream a haiku about the sea."
    python tests/goose/test_sse_live.py --timeout 200

Only third-party dependency: requests
Author: bibow
"""
from __future__ import print_function

# Windows consoles default to a legacy ANSI codepage; goose replies can
# include emoji, so force UTF-8 with replacement instead of crashing.
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

# tests/goose/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    extract_text, jsonrpc_error, load_env, resolve_config,
    health_ok, message_send_params, send_a2a, unwrap_response,
    goose_acp_ok,
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
        description="Live SSE streaming E2E for the A2A Goose gateway stack"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--goose-url", default=None)
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
        bridge="goose",
        gateway_url=args.gateway_url,
        goose_url=args.goose_url,
        token=args.token,
        endpoint_id=args.endpoint_id,
        part_id=args.part_id,
        agent_uuid=args.agent_uuid,
    )

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Goose SSE Live Streaming E2E{RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:   {cfg['gateway_url']}")
    print(f"  goose:  {cfg['goose_url']}")
    print(f"  endpoint:  {cfg['endpoint_id']}/{cfg['part_id']}")
    print(f"  agent:     {cfg['agent_uuid']}")
    print(f"  prompt:    {args.prompt}")

    if not args.no_health:
        _section("01 — goose serve health")
        o_ok = goose_acp_ok(cfg["goose_url"], cfg["goose_key"])
        _record("goose /v1/models", o_ok,
                f"({cfg['goose_url']})" if o_ok else f"({cfg['goose_url']})")
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
        task_type="goose_sse_live", stream=True,
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

    # 07: Direct goose serve streaming verification over ACP (goose has no
    # OpenAI-compatible chat surface — the ACP initialize POST + SSE streams
    # are the native transport, so probe those directly).
    _section("07 - Direct goose serve ACP streaming")
    goose_url = cfg["goose_url"]
    goose_key = cfg["goose_key"]
    goose_text = ""
    try:
        acp_headers = {"Content-Type": "application/json", "X-Secret-Key": goose_key}

        # initialize (result inline, 200 + canonical Acp-Connection-Id)
        r = requests.post(
            f"{goose_url}/acp",
            headers={**acp_headers, "Acp-Connection-Id": "sse-live-direct"},
            json={"jsonrpc": "2.0", "method": "initialize",
                  "params": {"protocolVersion": 1,
                             "clientCapabilities": {"fs": {"readTextFile": False,
                                                           "writeTextFile": False}}},
                  "id": 1},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError("initialize HTTP " + str(r.status_code))
        conn_id = r.headers.get("acp-connection-id") or "sse-live-direct"
        acp_headers["Acp-Connection-Id"] = conn_id

        # connection-scoped SSE stream (carries the session/new result)
        collected = []
        stop = threading.Event()

        def _direct_reader():
            with requests.get(
                f"{goose_url}/acp", headers={
                    **acp_headers, "Accept": "text/event-stream"},
                stream=True, timeout=60,
            ) as resp:
                for line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                    if stop.is_set():
                        break
                    if line.startswith("data: "):
                        collected.append(line[6:].strip())

        t = threading.Thread(target=_direct_reader, daemon=True)
        t.start()
        time.sleep(0.5)

        def _wait_msg(want_id, timeout_s=60):
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                for raw in list(collected):
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("id") == want_id and ("result" in msg or "error" in msg):
                        return msg
                time.sleep(0.2)
            return None

        # session/new -> 202, result rides the connection stream
        requests.post(
            f"{goose_url}/acp", headers=acp_headers,
            json={"jsonrpc": "2.0", "method": "session/new",
                  "params": {"cwd": os.getenv("GOOSE_CWD", "/workspace"), "mcpServers": []}, "id": 2},
            timeout=15,
        )
        new_msg = _wait_msg(2, timeout_s=45)
        session_id = (new_msg.get("result") or {}).get("sessionId") if new_msg else None
        if not session_id:
            raise RuntimeError("no sessionId from session/new")

        # session-scoped stream + prompt -> streamed updates + final result
        session_headers = {**acp_headers, "Acp-Session-Id": session_id}
        collected.clear()
        stop2 = threading.Event()

        def _direct_reader2():
            with requests.get(
                f"{goose_url}/acp", headers={
                    **session_headers, "Accept": "text/event-stream"},
                stream=True, timeout=60,
            ) as resp:
                for line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                    if stop2.is_set():
                        break
                    if line.startswith("data: "):
                        collected.append(line[6:].strip())

        t2 = threading.Thread(target=_direct_reader2, daemon=True)
        t2.start()
        time.sleep(0.5)
        requests.post(
            f"{goose_url}/acp", headers=acp_headers,
            json={"jsonrpc": "2.0", "method": "session/prompt",
                  "params": {"sessionId": session_id,
                             "prompt": [{"type": "text",
                                         "text": args.prompt}]},
                  "id": 3},
            timeout=15,
        )
        result = _wait_msg(3, timeout_s=90)
        stop2.set()
        stop.set()
        for raw in collected:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            upd = ((msg.get("params") or {}).get("update") or {})
            if msg.get("method") == "session/update" and (
                upd.get("sessionUpdate") == "agent_message_chunk"
            ):
                goose_text += ((upd.get("content") or {}).get("text")) or ""
        acp_ok = bool(goose_text) and result is not None
        _record("direct goose ACP streaming", acp_ok,
                ("stopReason=" + repr((result.get("result") or {}).get("stopReason"))
                 + " chars=" + str(len(goose_text))) if acp_ok else "no streamed text")
    except Exception as e:
        _record("direct goose ACP streaming", False, str(e))

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
