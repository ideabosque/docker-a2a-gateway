#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live SSE streaming E2E for the banyan A2A bridge (a2a-protocol-plugin
<-> docker-banyanos-gateway).

Verifies the full streaming pipeline end-to-end:

  01  Banyanos gateway health (GET /health)
  02  A2A gateway health
  03  SSE listener connects to GET /{endpoint_id}/a2a_sse
  04  Streaming message/send -> live token chunks broadcast over A2A SSE
  05  Final COMPLETED status received over SSE
  06  Fallback: HTTP response carries the accumulated reply when SSE misses it
  07  Direct Banyanos native streaming (banyan_ws channel + agentInvokeStream
      subscription trigger over HTTP; the transport contract the
      BanyanosAgentHandler drives) — skipped without BANYANOS_TOKEN

Each step prints PASS/FAIL. Exits non-zero if any step failed.

Usage:
    python tests/banyanos/test_sse_live.py
    python tests/banyanos/test_sse_live.py --prompt "Stream a haiku about the sea."
    python tests/banyanos/test_sse_live.py --timeout 300

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
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

# tests/banyanos/ -> tests/ (where a2a_test_utils.py lives)
sys_path = str(Path(__file__).resolve().parent.parent)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from a2a_test_utils import (  # noqa: E402  (path bootstrap above)
    B, C, D, G, R, Y, RST,
    BanyanosRawWebSocket, banyanos_gateway_ok, banyanos_login,
    is_reasoning_artifact, StreamSections,
    extract_text, jsonrpc_error, load_env, resolve_config,
    health_ok, message_send_params, send_a2a, unwrap_response,
)

__author__ = "bibow"
__test__ = False  # CLI scenarios, not pytest fixture-based tests.

DEFAULT_PROMPT = "Stream a short poem about the ocean, line by line."


class SSEListener:
    """Background SSE listener capturing streamed tokens + status events.

    Same listener contract as the pi/hermes/openclaw suites: task_artifact
    text chunks accumulate here; status and error events open/close
    the turn's terminal.
    """

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
        self.ready = threading.Event()
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
                self.ready.set()
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
                            if event.get("task_id") != self.current_task_id:
                                continue
                            state = event.get("state", event.get("status", ""))
                            if state in ("completed", "COMPLETED"):
                                self.completed = True
                                self.done.set()
                            if state in ("failed", "FAILED", "canceled", "CANCELED"):
                                self.errored = True
                                self.error_msg = f"terminal state: {state}"
                                self.done.set()
                        if etype == "error" or "error" in str(event).lower()[:20]:
                            err = event.get("error", event.get("message", ""))
                            if err:
                                self.errored = True
                                self.error_msg = str(err)
                                self.done.set()
            except requests.exceptions.ConnectionError:
                self.errored = True
                self.error_msg = "SSE connection failed"
                self.done.set()
            except Exception as e:
                if not self.stop:
                    self.error_msg = str(e)
                    self.errored = True
                    self.done.set()
            finally:
                self.ready.set()

        self.thread = threading.Thread(target=_listen, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=15)

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
    _results.append("pass" if ok else "fail")


def _skip(name, detail=""):
    mark = f"{Y}SKIP{RST}"
    print(f"  {mark} {name}" + (f"  {D}{detail}{RST}" if detail else ""))
    _results.append("skip")


def _section(title):
    print(f"\n{B}{title}{RST}")


def _resolve_banyan_token(cfg):
    if cfg.get("banyanos_token"):
        return cfg["banyanos_token"]
    email, password = cfg.get("banyanos_email"), cfg.get("banyanos_password")
    if email and password:
        return banyanos_login(
            cfg["banyanos_gateway_url"], cfg["banyanos_endpoint_id"],
            email, password, part_id=cfg.get("part_id", ""),
        )
    return ""


SUB_STREAM_DOC = """
subscription AgentInvokeStream($agentId: ID!, $message: String!, $sessionId: ID,
                               $includeReasoning: Boolean) {
  agentInvokeStream(agentId: $agentId, message: $message, sessionId: $sessionId,
                    includeReasoning: $includeReasoning) {
    eventType content toolName error iteration timestamp
  }
}
"""


def test_07_direct_banyan_stream(cfg, banyanos_token, prompt):
    """Direct backend-native streaming: banyan_ws channel + HTTP-triggered
    agentInvokeStream subscription — the exact contract BanyanosAgentHandler
    drives (handshake -> trigger with connection_id -> consume frames)."""
    _section("07 — Direct Banyanos native streaming (banyan_ws + trigger)")
    if not banyanos_token:
        _skip("banyan_ws native stream", "no BANYANOS_TOKEN / login credentials")
        return
    agent_id = cfg.get("banyanos_agent_id")
    if not agent_id:
        _skip("banyan_ws native stream", "no BANYANOS_AGENT_ID configured")
        return
    ws_base = cfg.get("banyanos_ws_url") or cfg["banyanos_gateway_url"]
    conn = BanyanosRawWebSocket(
        ws_base, cfg["banyanos_endpoint_id"], banyanos_token,
        part_id=cfg.get("part_id", ""), timeout=20.0,
    )
    try:
        connection_id = conn.connect()
        _record("banyan_ws handshake", True, f"connection_id={connection_id[:12]}...")

        # Trigger the subscription over HTTP with the connection id.
        trigger_url = (
            f"{cfg['banyanos_gateway_url'].rstrip('/')}/"
            f"{cfg['banyanos_endpoint_id']}/agent_engine_graphql"
        )
        r = requests.post(
            trigger_url,
            json={
                "query": SUB_STREAM_DOC,
                "variables": {"agentId": agent_id, "message": prompt,
                              "includeReasoning": cfg.get("show_reasoning", True)},
                "connection_id": connection_id,
            },
            headers={
                "Authorization": f"Bearer {banyanos_token}",
                "Content-Type": "application/json",
                "Part-Id": cfg["part_id"],
            },
            timeout=60,
        )
        if r.status_code != 200:
            _record("agentInvokeStream trigger", False,
                    f"http={r.status_code} {r.text[:120]}")
            return
        body = r.json()
        if not isinstance(body, dict) or body.get("errors"):
            _record("agentInvokeStream trigger accepted", False,
                    "invalid response or GraphQL errors")
            return
        _record("agentInvokeStream trigger accepted", True)

        # Consume frames until a terminal event (or deadline).
        events = []
        terminal = None
        deadline = time.time() + 180
        while time.time() < deadline:
            frame = conn.recv_json(timeout=20.0)
            if frame is None:
                break
            payload = (frame.get("data") or {}).get("agentInvokeStream") \
                if isinstance(frame, dict) else None
            if not isinstance(payload, dict):
                continue
            events.append(payload)
            if payload.get("eventType") in ("completed", "error"):
                terminal = payload
                break
        types = [e.get("eventType") for e in events]
        if not cfg.get("show_reasoning", True):
            _record("native reasoning disabled", "reasoning" not in types)
        _record("stream events received", bool(events),
                f"count={len(events)} types={types[:8]}{'...' if len(types) > 8 else ''}")
        _record("terminal completed event received",
                terminal is not None and terminal.get("eventType") == "completed",
                f"eventType={terminal and terminal.get('eventType')} "
                f"error={terminal and terminal.get('error')}")

        # Terminal completed content: full answer (authoritative)
        if terminal and terminal.get("eventType") == "completed":
            content = terminal.get("content") or ""
            deltas = "".join(
                e.get("content") or "" for e in events if e.get("eventType") == "chunk"
            )
            ok = bool(content) or bool(deltas)
            _record("completed content present", ok,
                    f"content_chars={len(content)} delta_chars={len(deltas)}")
    except Exception as e:
        _record("banyan_ws native stream", False, str(e))
    finally:
        conn.close()


def main():
    _results.clear()
    parser = argparse.ArgumentParser(
        description="Live SSE streaming E2E for the banyan A2A bridge"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--banyanos-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--banyanos-token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("--show-reasoning", action=argparse.BooleanOptionalAction,
                        default=None, help="Override the server reasoning default")
    parser.add_argument("--require-reasoning", action="store_true",
                        help="Fail unless the configured backend emits reasoning")
    parser.add_argument("--timeout", type=int, default=240,
                        help="Max seconds to wait for the stream to complete")
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
    raw_reasoning = env.get("BANYANOS_SHOW_REASONING", env.get("A2A_SHOW_REASONING", "true"))
    cfg["show_reasoning"] = (args.show_reasoning if args.show_reasoning is not None
                             else str(raw_reasoning).lower() in ("true", "1", "yes", "on"))
    banyanos_token = cfg.get("banyanos_token")
    if not banyanos_token and cfg.get("banyanos_email") and cfg.get("banyanos_password"):
        banyanos_token = banyanos_login(
            cfg["banyanos_gateway_url"], cfg["banyanos_endpoint_id"],
            cfg["banyanos_email"], cfg["banyanos_password"],
            part_id=cfg.get("part_id", ""),
        )

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}Banyanos SSE Live Streaming E2E{RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  a2a gateway:    {cfg['gateway_url']} "
          f"({cfg['endpoint_id']}/{cfg['part_id']})")
    print(f"  banyan gateway: {cfg['banyanos_gateway_url']} "
          f"(endpoint_id={cfg['banyanos_endpoint_id']})")
    print(f"  agent:          {cfg['agent_uuid']} "
          f"(banyan agent_id={cfg['banyanos_agent_id'] or '?'})")
    print(f"  prompt:         {args.prompt}")

    if not args.no_health:
        _section("01 — Banyanos gateway health")
        b_ok = banyanos_gateway_ok(cfg["banyanos_gateway_url"])
        _record("banyan GET /health", b_ok, f"({cfg['banyanos_gateway_url']})")
        _section("02 — A2A gateway health")
        g_ok = health_ok("A2A gateway", cfg["gateway_url"])
        _record("a2a gateway /health", g_ok)
        if not g_ok:
            print(f"\n{R}A2A gateway down — aborting.{RST}")
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
        task_type="banyanos_sse_live", stream=True,
        show_reasoning=args.show_reasoning,
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
    if args.require_reasoning:
        _record("received tagged reasoning", bool(sse.reasoning_text),
                f"chars={len(sse.reasoning_text)}")
    if not cfg["show_reasoning"]:
        _record("reasoning disabled", not sse.reasoning_text,
                f"chars={len(sse.reasoning_text)}")

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
    if not cfg["show_reasoning"]:
        result_metadata = (body.get("result") or {}).get("metadata") or {}
        _record("HTTP reasoning disabled", not result_metadata.get("reasoning"))
    if err:
        _record("HTTP response", False, f"error={err}")
    else:
        _record("HTTP response carries reply", bool(http_reply),
                f"chars={len(http_reply)}" if http_reply else "empty (relied on SSE)")

    sse.stop_listening()

    test_07_direct_banyan_stream(cfg, banyanos_token, args.prompt)

    passed = _results.count("pass")
    failed = _results.count("fail")
    skipped = _results.count("skip")
    total = len(_results)
    print(f"\n{B}{passed} passed, {failed} failed, {skipped} skipped ({total} checks){RST}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
