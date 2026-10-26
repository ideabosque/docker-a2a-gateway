#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 spike (docs/PI_INTEGRATION_PLAN.md) — drive the pi sidecar directly and
capture the real RPC/event behaviour into a transcript JSON.

Verifies the plan's "(verify)" items against the PINNED pi version in the
bundled sidecar container:

  1.  GET /health                       — pi version + session count
  2.  POST /sessions                    — sessionId/sessionFile minted
        (models.json compat endpoint; --session-dir /data/sessions override)
  3.  prompt (plain)                    — disposition started, text_delta
        events, run_end usage
  4.  thinking (if the model reasons)   — thinking_delta captured
  5.  prompt (tool call)                — tool_execution_start/end events
  6.  prompt-while-streaming            — 409 without streamingBehavior;
        JSON ack with streamingBehavior=followUp; follow-up text arrives
  7.  abort mid-run                     — agent_settled aborted:true /
        run_end aborted:true
  8.  session resume                    — DELETE the live process, re-prompt:
        getOrResume re-opens the session file (switch_session) and the
        conversation keeps its history (recall test)

Usage:
    python tests/pi/test_spike_protocol.py                 # against .env sidecar
    python tests/pi/test_spike_protocol.py --out transcript.json

Only third-party dependency: requests
Author: bibow
"""
from __future__ import print_function

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import B, C, D, G, R, Y, RST, load_env

__author__ = "bibow"

_results: list = []
_transcript: dict = {}


def _ok(name: str, ok: bool, detail: str = "") -> bool:
    mark = f"{G}PASS{RST}" if ok else f"{R}FAIL{RST}"
    print(f"  {mark} {name}" + (f"  {D}{detail}{RST}" if detail else ""))
    _results.append(ok)
    return ok


def _section(title: str) -> None:
    print(f"\n{B}{title}{RST}")


class Sidecar:
    """Minimal synchronous pi-sidecar client for the spike."""

    def __init__(self, base_url: str, key: str):
        self.base = base_url.rstrip("/")
        self.key = key
        self.s = requests.Session()
        self.s.headers.update({
            "Content-Type": "application/json",
            "X-Secret-Key": key,
        })

    def health(self):
        return self.s.get(f"{self.base}/health", timeout=10)

    def create(self, cwd="/workspace", model=None, thinking=None):
        body = {"cwd": cwd}
        if model:
            body["model"] = model
        if thinking:
            body["thinking"] = thinking
        return self.s.post(f"{self.base}/sessions", json=body, timeout=60)

    def get_session(self, sid):
        return self.s.get(f"{self.base}/sessions/{sid}", timeout=30)

    def delete_session(self, sid):
        return self.s.delete(f"{self.base}/sessions/{sid}", timeout=30)

    def abort(self, sid):
        return self.s.post(f"{self.base}/sessions/{sid}/abort", json={}, timeout=30)

    def ui_response(self, sid, payload):
        return self.s.post(f"{self.base}/sessions/{sid}/ui-response",
                           json=payload, timeout=30)

    def prompt_sse(self, sid, message, streaming_behavior=None, timeout=180):
        """POST prompt; yields (event, event_type) parsed from the SSE frames."""
        body = {"message": message}
        if streaming_behavior:
            body["streamingBehavior"] = streaming_behavior
        with self.s.post(
            f"{self.base}/sessions/{sid}/prompt", json=body,
            headers={"Accept": "text/event-stream"},
            stream=True, timeout=(30, timeout),
        ) as resp:
            yield resp.status_code, resp.headers.get("content-type", "")
            for line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                yield event, event.get("type", "")


def collect_run(spike: Sidecar, sid: str, message: str,
                streaming_behavior=None, max_seconds=180.0):
    """Run one prompt window; return (status, events, run_end, text, thinking)."""
    events: list = []
    run_end: dict = {}
    text_parts: list = []
    thinking_parts: list = []
    status, ctype, closed = None, "", False
    deadline = time.monotonic() + max_seconds
    for item in spike.prompt_sse(sid, message, streaming_behavior, timeout=max_seconds + 30):
        if isinstance(item, tuple) and isinstance(item[0], int):
            status, ctype = item
            print(f"    {D}prompt -> HTTP {status} ({ctype.strip() or 'no content-type'}){RST}")
            continue
        event, etype = item
        events.append({"type": etype, **event})
        # The bundled tool gate emits blocking dialogs even for this direct
        # transport probe. Answer this test's dialogs so the tool-call and
        # cancellation scenarios can finish with the gate enabled.
        if etype == "extension_ui_request":
            method = event.get("method")
            if method in ("confirm", "select", "input", "editor"):
                answer = {"confirmed": True} if method == "confirm" else {"cancelled": True}
                response = spike.ui_response(sid, {"id": event["id"], **answer})
                if response.status_code != 200:
                    raise RuntimeError(f"dialog response HTTP {response.status_code}")
        if etype == "message_update":
            sub = event.get("assistantMessageEvent") or {}
            if sub.get("type") == "text_delta":
                text_parts.append(sub.get("delta") or "")
            elif sub.get("type") == "thinking_delta":
                thinking_parts.append(sub.get("delta") or "")
        if etype == "run_end":
            run_end = event
            closed = True
            break
        if time.monotonic() > deadline:
            break
    return {
        "status": status, "content_type": ctype, "closed_with_run_end": closed,
        "events": events, "run_end": run_end,
        "text": "".join(text_parts), "thinking": "".join(thinking_parts),
    }


def summarize_events(events: list) -> list:
    """Compact per-type event counts + assistantMessageEvent subtypes."""
    counts: dict = {}
    subs: dict = {}
    for e in events:
        etype = e.get("type", "")
        counts[etype] = counts.get(etype, 0) + 1
        if etype == "message_update":
            sub = (e.get("assistantMessageEvent") or {}).get("type", "")
            if sub:
                subs[sub] = subs.get(sub, 0) + 1
    return {"counts": counts, "assistant_message_events": subs}


def main():
    parser = argparse.ArgumentParser(
        description="P1 spike: real-pi behaviour capture through the sidecar")
    parser.add_argument("--pi-url", default=None,
                        help="Sidecar base URL (default from .env PI_API_URL host-resolved)")
    parser.add_argument("--model", default=None,
                        help="provider/modelId (default: compat/<PI_COMPAT_MODEL>)")
    parser.add_argument("--out", default=None,
                        help="Write the transcript JSON here instead of printing only")
    args = parser.parse_args()

    env = load_env()
    # host-reachable sidecar URL: PI_API_URL is in-container (http://pi:3300).
    pi_port = env.get("PI_PORT", "3300")
    base = args.pi_url or f"http://127.0.0.1:{pi_port}"
    key = env.get("PI_SECRET_KEY", "")
    model = args.model or f"compat/{env.get('PI_COMPAT_MODEL', '') or 'local-model'}"

    spike = Sidecar(base, key)

    print(f"{B}{'=' * 70}{RST}")
    print(f"{C}pi sidecar P1 spike{RST}")
    print(f"{B}{'=' * 70}{RST}")
    print(f"  sidecar: {base}")
    print(f"  model:   {model}")
    transcript_sections: dict = {}

    # ── 1. health ────────────────────────────────────────────────────────────
    _section("01 — GET /health")
    r = spike.health()
    probe = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:200]}
    _ok("health 200 + status ok + pi version",
        probe["status"] == 200 and probe["body"].get("status") == "ok",
        f"pi={probe['body'].get('pi')}")
    transcript_sections["01_health"] = probe

    # ── 2. session create ────────────────────────────────────────────────────
    _section("02 — POST /sessions (model compat, cwd /workspace)")
    r = spike.create(cwd="/workspace", model=model)
    create = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:200]}
    sid = (create["body"] or {}).get("sessionId", "")
    file_hint = (create["body"] or {}).get("sessionFile", "")
    _ok("session created with sessionId + sessionFile",
        bool(sid) and bool(file_hint),
        f"sessionFile={file_hint}")
    transcript_sections["02_create"] = create

    # (verify) session files land in the overridden --session-dir
    session_dir_via_stats = {}
    try:
        g = spike.get_session(sid)
        body = g.json() if g.status_code == 200 else {}
        state = body.get("state", {}) or {}
        session_dir_via_stats = {
            "get_status": g.status_code,
            "state_sessionFile": state.get("sessionFile"),
            "state_model": (state.get("model") or {}).get("id"),
            "state_streaming": state.get("isStreaming"),
        }
        _ok("GET /sessions/{id} → state + stats", g.status_code == 200,
            f"sessionFile={session_dir_via_stats['state_sessionFile']} "
            f"model={session_dir_via_stats['state_model']}")
    except Exception as e:
        _ok("GET /sessions/{id} → state + stats", False, str(e))
    transcript_sections["02_get_session"] = session_dir_via_stats

    # ── 3. plain prompt ──────────────────────────────────────────────────────
    _section("03 — prompt (plain answer)")
    run = collect_run(spike, sid, f"Say exactly: PI-SPIKE-{uuid.uuid4().hex[:4].upper()}")
    run["_summary"] = summarize_events(run["events"])
    _ok("HTTP 200 SSE + run_end frame", run["status"] == 200 and run["closed_with_run_end"],
        f"types={run['_summary']['counts']}")
    _ok("text_delta answer arrived", bool(run["text"]), f"chars={len(run['text'])}")
    transcript_sections["03_plain"] = {
        k: run[k] for k in ("status", "content_type", "closed_with_run_end", "text")
    } | {"summary": run["_summary"], "run_end": run["run_end"]}
    print(f"    {D}answer: {run['text'][:120]!r}{RST}")

    # ── 4. thinking (model-dependent) ────────────────────────────────────────
    _section("04 — prompt (thinking — model-dependent)")
    run2 = collect_run(spike, sid, "Think step by step about what 17 * 23 is, then answer.")
    run2["_summary"] = summarize_events(run2["events"])
    thinking_delta = run2["thinking"]
    text_out = run2["text"]
    has_thinking_events = run2["_summary"]["assistant_message_events"].get("thinking_delta", 0)
    _ok("prompt ran to run_end", run2["closed_with_run_end"],
        f"thinking_deltas={has_thinking_events}, chars={len(text_out)}")
    if has_thinking_events:
        _ok("thinking_delta captured", bool(thinking_delta), f"{len(thinking_delta)} chars")
    else:
        print(f"    {Y}(model emitted no thinking_delta — reasoning coverage\n"
              f"     rides pi models with thinking enabled for this bridge){RST}")
    transcript_sections["04_thinking"] = {
        "summary": run2["_summary"], "run_end": run2["run_end"],
    }

    # ── 5. tool call ─────────────────────────────────────────────────────────
    _section("05 — prompt (tool call: bash echo)")
    token = uuid.uuid4().hex[:6]
    run3 = collect_run(spike, sid,
                       f"Run the bash tool with the command: echo spike-{token}. "
                       "Then show the output.")
    run3["_summary"] = summarize_events(run3["events"])
    starts = [e for e in run3["events"] if e.get("type") == "tool_execution_start"]
    ends = [e for e in run3["events"] if e.get("type") == "tool_execution_end"]
    _ok("tool_execution_start/end captured", bool(starts) and bool(ends),
        f"tools={len(starts)} (names={sorted({s.get('toolName') for s in starts})})")
    tool_out = "".join(
        (block.get("text") or "")
        for block in ((ends[0].get("result") or {}).get("content") or [])
    ) if ends else ""
    _ok("tool result content captured", f"spike-{token}" in tool_out or f"spike-{token}" in run3["text"],
        f"output={tool_out[:80]!r}")
    transcript_sections["05_tool"] = {
        "summary": run3["_summary"],
        "tool_execution_start_shape": starts[0] if starts else None,
        "tool_execution_end_shape": {k: ends[0].get(k)
                                     for k in ("toolCallId", "toolName", "isError", "durationMs")}
        if ends else None,
    }

    # ── 6. prompt-while-streaming ────────────────────────────────────────────
    _section("06 — prompt while streaming (409 + followUp ack)")
    long_prompt = ("Write a very detailed 600-word outline about the history of "
                   "printing, section by section. Do not stop early.")
    busy_status: dict = {}
    busy_result: dict = {}

    def _busy_sender():
        try:
            busy_result.update(collect_run(spike, sid, long_prompt, max_seconds=150))
        except Exception as e:
            busy_result["error"] = str(e)

    t = threading.Thread(target=_busy_sender, daemon=True)
    t.start()
    # Wait until the first prompt has started streaming (poll get_state).
    became_busy = False
    for _ in range(50):
        try:
            g = spike.get_session(sid)
            if (g.json() or {}).get("streaming"):
                became_busy = True
                break
        except Exception:
            pass
        time.sleep(0.3)
    if became_busy:
        r2 = spike.s.post(
            f"{spike.base}/sessions/{sid}/prompt",
            json={"message": "Just say 'no.'"},
            headers={"Content-Type": "application/json", "X-Secret-Key": spike.key},
            timeout=30,
        )
        busy_status = {"no_behavior_status": r2.status_code,
                       "no_behavior_body": r2.json() if r2.status_code != 200 else {}}
        _ok("second prompt without streamingBehavior → 409", r2.status_code == 409,
            f"HTTP {r2.status_code}")
        r3 = spike.s.post(
            f"{spike.base}/sessions/{sid}/prompt",
            json={"message": "Then also say 'queued.'",
                  "streamingBehavior": "followUp"},
            headers={"Content-Type": "application/json", "X-Secret-Key": spike.key},
            timeout=30,
        )
        ok_ack = r3.status_code == 200 and "disposition" in (r3.json() or {})
        busy_status["followup_status"] = r3.status_code
        busy_status["followup_body"] = r3.json() if r3.status_code == 200 else r3.text[:200]
        _ok("followUp during streaming → JSON ack", ok_ack,
            f"HTTP {r3.status_code} {busy_status['followup_body']}")
    else:
        _ok("second prompt without streamingBehavior → 409", False,
            "first prompt never became busy in time")
    spike.abort(sid)  # end the long run for the next step
    t.join(timeout=150)
    transcript_sections["06_busy"] = busy_status

    # ── 7. abort mid-run ─────────────────────────────────────────────────────
    _section("07 — abort mid-run")
    abort_result: dict = {}

    def _abort_run():
        try:
            abort_result.update(collect_run(
                spike, sid,
                "Write a 5000-word essay on the history of computing. Detail is required.",
                max_seconds=120))
            # The run_end that closes the stream after our abort below.
            ra = spike.abort(sid)
            abort_result["abort_http"] = ra.status_code
            abort_result["abort_body"] = (ra.json() or {}) if ra.status_code == 200 else ra.text[:200]
        except Exception as e:
            abort_result["error"] = str(e)

    t = threading.Thread(target=_abort_run, daemon=True)
    t.start()
    time.sleep(6)  # let the essay generation start
    run_end_aborted = None

    def _watcher():
        nonlocal run_end_aborted
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if abort_result.get("run_end"):
                run_end_aborted = abort_result["run_end"]
                return
            time.sleep(0.3)

    w = threading.Thread(target=_watcher, daemon=True)
    w.start()
    try:
        time.sleep(4)  # watcher + run window
        if not abort_result.get("run_end"):
            ra2 = spike.abort(sid)
            abort_result["abort_http"] = ra2.status_code
    finally:
        t.join(timeout=130)
    aborted = bool(run_end_aborted and run_end_aborted.get("aborted")) or \
        (abort_result.get("run_end") or {}).get("aborted") is True
    _ok("abort → run settles aborted:true", aborted,
        f"run_end.aborted={(run_end_aborted or {}).get('aborted') if run_end_aborted else None}")
    transcript_sections["07_abort"] = {
        "abort": "aborted:true observed" if aborted
        else f"unexpected: {json.dumps(abort_result)[:300]}",
    }

    # ── 8. session resume (process closed, file re-opened) ──────────────────
    _section("08 — session resume (DELETE process → re-prompt)")
    rd = spike.delete_session(sid)
    resume = {"delete_status": rd.status_code}
    time.sleep(1)
    # Probe the (now-closed) session: getOrResume should bring it back.
    r4 = spike.get_session(sid)
    resume["probe_status_after_close"] = r4.status_code
    resume_file = (r4.json() or {}).get("state", {}).get("sessionFile") if r4.status_code == 200 else None
    resume["sessionFile"] = resume_file
    _ok("closed session re-opened through GET probe",
        r4.status_code == 200 and bool(resume_file), f"sessionFile={resume_file}")

    run4 = collect_run(spike, sid,
                       "Without any preamble: what exact answer did I ask you to "
                       "say in my very first message?")
    resume["resume_text"] = run4["text"][:300]
    resumed_ok = "PI-SPIKE-" in run4["text"] and len(run4["text"]) < 40
    _ok("resumed session kept history (echoed the first-turn answer)", resumed_ok,
        f"answer={run4['text'][:80]!r}")
    # and the process is live again afterwards
    g = spike.get_session(sid)
    resume["final_state"] = {"streaming": (g.json() or {}).get("streaming")}
    transcript_sections["08_resume"] = resume

    # ── summary ──────────────────────────────────────────────────────────────
    passed = sum(1 for x in _results if x)
    total = len(_results)
    print(f"\n{B}{'=' * 70}{RST}")
    mark = f"{G}" if passed == total else f"{R}"
    print(f"{mark}RESULT: {passed}/{total} passed{RST}")

    if args.out:
        out = {
            "captured": "tests/pi/test_spike_protocol.py",
            "sidecar": base,
            "model": model,
            "results": {"passed": passed, "total": total},
            "sections": transcript_sections,
        }
        Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\n{D}transcript written to {args.out}{RST}")
        # cleanup: leave the volume tidy (best effort)
        try:
            spike.delete_session(sid)
        except Exception:
            pass

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
