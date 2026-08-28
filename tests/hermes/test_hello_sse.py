#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot "say hello from Hermes" through A2A, streamed back over SSE.

    Client -> POST /{endpoint_id}/a2a (message/send, stream=true)
    -> SilvaEngine Gateway -> A2ADaemonExecutor -> HermesAgentHandler
    -> Hermes API Server (POST /v1/runs + GET /v1/runs/{id}/events SSE)
    -> token chunks broadcast to GET /{endpoint_id}/a2a_sse
    -> printed here in real-time

Opens a background SSE listener, sends one prompt, waits for the streamed
reply, and exits. Exits non-zero on failure.

Usage:
    python tests/hermes/test_hello_sse.py
    python tests/hermes/test_hello_sse.py --prompt "Greet me in one sentence."
    python tests/hermes/test_hello_sse.py --timeout 200

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

DEFAULT_PROMPT = "Say hello from Hermes through A2A. Reply in one short sentence."


class SSEListener:
    """Background SSE listener that prints streaming chunks in real-time."""

    def __init__(self, gateway_url, token, endpoint_id, part_id):
        self.gateway_url = gateway_url
        self.token = token
        self.endpoint_id = endpoint_id
        self.part_id = part_id
        self.stop = False
        self.thread = None
        self.current_task_id = None
        self.full_text = ""
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
                                    if local_text and (
                                        text == local_text
                                        or text.startswith(local_text)
                                        or local_text.startswith(text)
                                    ):
                                        pass
                                    else:
                                        print(text, end="", flush=True)
                                        local_text += text
                                        self.full_text = local_text
                        if etype in ("task_status", "status"):
                            state = event.get("state", event.get("status", ""))
                            if state in ("completed", "COMPLETED"):
                                self.done.set()
                        if etype == "error" or "error" in str(event).lower()[:20]:
                            err = event.get("error", event.get("message", ""))
                            if err:
                                print(f"\n{R}Error: {err}{RST}", flush=True)
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
        self.done.clear()
        self._turn += 1
        self._active_turn = self._turn

    def stop_listening(self):
        self.stop = True


def main():
    parser = argparse.ArgumentParser(
        description="One-shot streaming E2E: say hello from Hermes via A2A + SSE"
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
                        help="Max seconds to wait for the SSE stream to complete")
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
    print(f"{C}Hermes Hello (SSE streaming){RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  gateway:   {cfg['gateway_url']}")
    print(f"  hermes:    {cfg['hermes_url']}")
    print(f"  endpoint:  {cfg['endpoint_id']}/{cfg['part_id']}")
    print(f"  agent:     {cfg['agent_uuid']}")
    print(f"  prompt:    {args.prompt}")
    print()

    if not args.no_health:
        if not health_ok("Gateway", cfg["gateway_url"]):
            return 1
        if not health_ok("Hermes API Server", cfg["hermes_url"]):
            print(f"  {Y}(continuing — Hermes may be external){RST}")

    sse = SSEListener(cfg["gateway_url"], cfg["token"],
                      cfg["endpoint_id"], cfg["part_id"])
    sse.start()

    task_id = f"hello-{uuid.uuid4().hex[:8]}"
    sse.set_task(task_id)

    print(f"\n{B}Agent>{RST} ", end="", flush=True)
    params = message_send_params(
        args.prompt, cfg["agent_uuid"], task_id,
        task_type="hermes_hello_sse", stream=True,
    )
    r = send_a2a(
        cfg["gateway_url"], cfg["token"], cfg["endpoint_id"], cfg["part_id"],
        "message/send", params, request_id=f"hello-{task_id}",
        timeout=240,
    )

    sse.done.wait(timeout=args.timeout)
    streamed_text = sse.full_text
    print()

    if not streamed_text:
        body = unwrap_response(r)
        streamed_text = extract_text(body)

    sse.stop_listening()

    if streamed_text:
        if not sse.full_text:
            print(streamed_text)
        print(f"{D}({len(streamed_text)} chars){RST}")
        return 0

    body = unwrap_response(r)
    err = jsonrpc_error(body)
    if err:
        print(f"{R}JSON-RPC error: {err}{RST}")
    else:
        print(f"{R}No response received. HTTP {r.status_code}: {r.text[:200]}{RST}")
    return 2


if __name__ == "__main__":
    sys.exit(main())