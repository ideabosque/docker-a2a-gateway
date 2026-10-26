#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live SSE streaming E2E for the docker-a2a-gateway stack (pi bridge).

Verifies the full streaming pipeline end-to-end:

  01  pi sidecar health (ACP `initialize` probe on POST /acp)
  02  Gateway health
  03  SSE listener connects to GET /{endpoint_id}/a2a_sse
  04  Streaming message/send -> live token chunks broadcast over SSE
  05  Final COMPLETED status received over SSE
  06  Fallback: HTTP response carries the accumulated reply when SSE misses it
  07  Direct pi sidecar SSE streaming verification (POST /sessions and
      POST /sessions/{id}/prompt over the native transport)

Each step prints PASS/FAIL. Exits non-zero if any step failed.

Usage:
    python tests/pi/test_sse_live.py
    python tests/pi/test_sse_live.py --prompt "Stream a haiku about the sea."
    python tests/pi/test_sse_live.py --timeout 200

Only third-party dependency: requests
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
import threading
import time
import uuid
from pathlib import Path

import requests

# tests/pi/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    is_reasoning_artifact, StreamSections,
    extract_text, jsonrpc_error, load_env, resolve_config,
    health_ok, message_send_params, send_a2a, unwrap_response,
    pi_sidecar_ok,
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
        self.sections = StreamSections(response_color="")
        self.chunks_seen = 0
        self.reasoning_text = ""
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
                                # Reasoning (tagged in artifact.metadata) prints in its own
                                # tagged section; the answer is accumulated separately.
                                if isinstance(artifact, dict) and artifact.get("text"):
                                    text = artifact["text"]
                                    if is_reasoning_artifact(artifact):
                                        self.reasoning_text += text
                                        self.sections.reasoning_chunk(text)
                                    elif local_text and (
                                        text == local_text
                                        or text.startswith(local_text)
                                        or local_text.startswith(text)
                                    ):
                                        pass  # accumulated / duplicate chunk
                                    else:
                                        self.chunks_seen += 1
                                        self.sections.response_chunk(text)
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
        self.sections.reset()
        self.chunks_seen = 0
        self.reasoning_text = ""
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
        description="Live SSE streaming E2E for the A2A pi gateway stack"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--pi-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("--timeout", type=int, default=200,
                        help="Max seconds to wait for the stream to complete")
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
    print(f"{C}pi SSE Live Streaming E2E{RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:   {cfg['gateway_url']}")
    print(f"  pi:        {cfg['pi_url']}")
    print(f"  endpoint:  {cfg['endpoint_id']}/{cfg['part_id']}")
    print(f"  agent:     {cfg['agent_uuid']}")
    print(f"  prompt:    {args.prompt}")

    if not args.no_health:
        _section("01 — pi sidecar health")
        o_ok = pi_sidecar_ok(cfg["pi_url"], cfg["pi_key"])
        _record("pi GET /health", o_ok, f"({cfg['pi_url']})")
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
        task_type="pi_sse_live", stream=True,
        show_reasoning=False if args.hide_reasoning else None,
    )
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id=f"sse-{task_id}", timeout=240,
    )
    sse.done.wait(timeout=args.timeout)
    sse.sections.close()
    print()

    streamed_text = sse.full_text
    _record("received streamed tokens", bool(streamed_text),
            f"chunks={sse.chunks_seen} chars={len(streamed_text)}")
    if args.hide_reasoning:
        _record("show_reasoning=false suppresses reasoning artifacts",
                not sse.reasoning_text, f"reasoning_chars={len(sse.reasoning_text)}")

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

    # 07: Direct pi sidecar streaming verification over HTTP+SSE (pi has no
    # network server of its own — the sidecar's POST /sessions +
    # POST /sessions/{id}/prompt event stream is the native transport).
    _section("07 - Direct pi sidecar SSE streaming")
    pi_url = cfg["pi_url"]
    pi_key = cfg["pi_key"]
    pi_text = ""
    session_id = None
    try:
        # POST /sessions -> {sessionId, sessionFile}. Pass the configured
        # default model: the sidecar session boots with pi's own default
        # otherwise (a provider the compat key is not valid for).
        create_body = {"cwd": env.get("PI_CWD", "/workspace")}
        model = env.get("PI_SIDE_MODEL") or env.get("PI_MODEL")
        compat_model = env.get("PI_COMPAT_MODEL") or env.get("OPENAI_COMPAT_MODEL")
        if model or compat_model:
            create_body["model"] = model or f"compat/{compat_model}"
        r = requests.post(
            f"{pi_url}/sessions",
            headers={"Content-Type": "application/json", "X-Secret-Key": pi_key},
            json=create_body,
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError("create session HTTP " + str(r.status_code))
        session_id = (r.json() or {}).get("sessionId")
        if not session_id:
            raise RuntimeError("no sessionId from POST /sessions")

        # POST prompt -> SSE of pi events, terminated by a run_end frame.
        pi_text = ""
        with requests.post(
            f"{pi_url}/sessions/{session_id}/prompt",
            headers={"Content-Type": "application/json", "X-Secret-Key": pi_key},
            json={"message": args.prompt},
            stream=True,
            timeout=(30, 120),
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError("prompt HTTP " + str(resp.status_code))
            run_end = None
            for line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "extension_ui_request" and event.get("method") == "confirm":
                    requests.post(
                        f"{pi_url}/sessions/{session_id}/ui-response",
                        headers={"X-Secret-Key": pi_key},
                        json={"id": event["id"], "confirmed": True}, timeout=15,
                    ).raise_for_status()
                if event.get("type") == "message_update":
                    if ((event.get("assistantMessageEvent") or {}).get("type")) == "text_delta":
                        pi_text += event["assistantMessageEvent"].get("delta") or ""
                if event.get("type") == "run_end":
                    run_end = event
                    break
        pi_ok = bool(pi_text) and run_end is not None
        _record("direct pi sidecar SSE streaming", pi_ok,
                ("stopReason=" + repr((run_end or {}).get("stopReason"))
                 + " chars=" + str(len(pi_text))) if pi_ok else "no streamed text")
    except Exception as e:
        _record("direct pi sidecar SSE streaming", False, str(e))
    finally:
        if session_id:
            try:
                requests.delete(f"{pi_url}/sessions/{session_id}",
                                headers={"X-Secret-Key": pi_key}, timeout=15)
            except requests.RequestException:
                pass

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
