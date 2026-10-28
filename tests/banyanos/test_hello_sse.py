#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot streaming E2E: say hello from the Banyanos agent via A2A + SSE.

    Client -> POST /{endpoint_id}/a2a (message/send, stream=true)
    -> SilvaEngine Gateway -> A2ADaemonExecutor -> BanyanosAgentHandler
    -> docker-banyanos-gateway (banyan_ws channel: agentInvokeStream
       subscription) -> token chunks broadcast to the A2A SSE stream

Reads ./env from this directory, resolves/mints a gateway JWT, health-checks
the gateway and (optionally) the Banyanos backend, sends a short prompt, and
prints the live-streamed reply. Exits non-zero on failure so it works as an
smoke test in scripts/CI.

Usage:
    python tests/banyanos/test_hello_sse.py
    python tests/banyanos/test_hello_sse.py --prompt "Stream a haiku about the sea."
    python tests/banyanos/test_hello_sse.py --timeout 300

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
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

# tests/banyanos/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (
    B, C, D, G, R, Y, RST,
    banyanos_gateway_ok, extract_text, jsonrpc_error, load_env, resolve_config,
    health_ok, is_reasoning_artifact, message_send_params, send_a2a,
    unwrap_response, StreamSections,
)

__author__ = "bibow"

DEFAULT_PROMPT = "Say hello and introduce yourself in one short sentence."


class SSEListener:
    """Background SSE listener capturing streamed tokens + COMPLETED status."""

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
        self.done = threading.Event()
        self.completed = False
        self.errored = False
        self.error_msg = ""
        self.chunks_seen = 0
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
                if r.status_code != 200:
                    print(f"{R}SSE connection failed: HTTP {r.status_code}{RST}")
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
                                self.errored = True
                                self.done.set()
                        if etype == "error" or "error" in str(event).lower()[:20]:
                            err = event.get("error", event.get("message", ""))
                            if err:
                                print(f"\n{R}Error: {err}{RST}", flush=True)
                                self.errored = True
                                self.error_msg = str(err)
                                self.done.set()
            except requests.exceptions.ConnectionError:
                pass
            except Exception as e:
                if not self.stop:
                    print(f"{R}SSE error: {e}{RST}")

        self.thread = threading.Thread(target=_listen, daemon=True)
        self.thread.start()
        time.sleep(1)

    def set_task(self, task_id):
        self.current_task_id = task_id
        self.full_text = ""
        self.sections.reset()
        self.done.clear()
        self.completed = False
        self.errored = False
        self.error_msg = ""
        self.chunks_seen = 0
        self._turn += 1
        self._active_turn = self._turn

    def stop_listening(self):
        self.stop = True


def main():
    parser = argparse.ArgumentParser(
        description="One-shot streaming E2E: say hello from the Banyanos agent via A2A + SSE"
    )
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--banyanos-url", default=None,
                        help="Banyanos gateway base URL (health check only)")
    parser.add_argument("--token", default=None, help="gateway JWT")
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("--show-reasoning", action=argparse.BooleanOptionalAction,
                        default=None, help="Override the server reasoning default")
    parser.add_argument("--skip-banyanos-probe", action="store_true")
    parser.add_argument("--timeout", type=int, default=240,
                        help="Max seconds to wait for the SSE stream to complete")
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
    print(f"{C}Banyanos Hello (SSE streaming){RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:          {cfg['gateway_url']}")
    print(f"  banyanos gateway: {cfg['banyanos_gateway_url']} "
          f"(endpoint={cfg['banyanos_endpoint_id']}, "
          f"agent_id={cfg['banyanos_agent_id'] or '?'})")
    print(f"  endpoint:         {cfg['endpoint_id']}/{cfg['part_id']}")
    print(f"  agent:            {cfg['agent_uuid']}")
    print(f"  prompt:           {args.prompt}")
    print()

    if not args.no_health:
        if not health_ok("A2A gateway", cfg["gateway_url"]):
            return 1
        if not args.skip_banyanos_probe:
            if not banyanos_gateway_ok(cfg["banyanos_gateway_url"]):
                print(f"  {Y}(continuing — Banyanos gateway may be external){RST}")

    sse = SSEListener(cfg["gateway_url"], cfg["token"],
                      cfg["endpoint_id"], cfg["part_id"])
    sse.start()

    task_id = f"banyanos-hello-{uuid.uuid4().hex[:8]}"
    sse.set_task(task_id)

    print(f"\n{B}Agent>{RST} ", end="", flush=True)
    params = message_send_params(
        args.prompt, cfg["agent_uuid"], task_id,
        task_type="banyanos_hello_sse", stream=True,
        show_reasoning=args.show_reasoning,
    )
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id=f"banyanos-{task_id}",
        timeout=240,
    )

    sse.done.wait(timeout=args.timeout)
    sse.sections.close()
    streamed_text = sse.full_text
    print()

    ok = True
    if not streamed_text:
        body = unwrap_response(r)
        err = jsonrpc_error(body)
        if err:
            print(f"{R}JSON-RPC error: {err}{RST}")
            return 3
        streamed_text = extract_text(body)
        if streamed_text:
            print(f"  {Y}SSE carried no tokens; reply came via HTTP response{RST}")
    if not streamed_text:
        print(f"{R}No reply text received on SSE or HTTP{RST}")
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
        return 4
    if sse.errored:
        print(f"{R}Stream ended with an error: {sse.error_msg}{RST}")
        ok = False

    exit_code = 0 if ok and (sse.completed or sse.chunks_seen) else 5
    print(f"\n{G}PASS{RST} Banyanos reply: {streamed_text}")
    print(f"{D}({len(streamed_text)} chars, chunks={sse.chunks_seen}, "
          f"completed={sse.completed}){RST}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
