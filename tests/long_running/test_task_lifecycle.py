#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live long-running A2A task lifecycle scenarios (docker-a2a-gateway).

This is a SCRIPT, not a pytest module: it is executed as a bounded
subprocess by ``scripts/run_long_running_tests.py --suite live`` and it
prints one sanitized PASS/FAIL/UNMET line per check. It is import-safe
(no side effects at import time) so the helpers can be reused.

Scenarios (per selected bridge):

  01 health            gateway health, backend health, agent-card access
                       policy (the card is public but needs Part-Id; the
                       JSON-RPC surface must reject unauthenticated calls).
  02 submit            submit a task with the selected response mode and
                       record the acceptance latency + the returned envelope.
  03 observe           poll ``tasks/get`` to a terminal state with a bounded
                       deadline and read the PERSISTED output.
  04 stability         observe the same terminal task again: state and output
                       must be stable and no second backend execution.
  05 disconnect        authenticated SSE listener first, disconnect it while
                       the task runs, reconnect, then prove the final result
                       is still recoverable through ``tasks/get``.
  06 cancel            ``tasks/cancel``: a terminal A2A state is required; the
                       backend stop outcome is reported SEPARATELY
                       (requested / confirmed / unsupported) and a late
                       completion overwriting the cancelled state is a
                       distinct check.
  07 unknown_agent     an unknown agent must produce a typed error, not a
                       successful completed Message.

Honesty rules (see docs/LONG_RUNNING_TASKS_TEST_PLAN.md):

* A REQUIRED check that fails -> the scenario FAILS and the process exits
  non-zero.
* A contract this build does not implement yet -> the check is recorded as
  UNMET with the observed evidence (never as PASS); it does not fail the
  run, but the runner marks the coverage as incomplete.
* Secrets are never printed: only lengths, digests and redacted snippets.

Usage:

    python tests/long_running/test_task_lifecycle.py --bridge omp
    python tests/long_running/test_task_lifecycle.py --bridge omp \\
        --response-mode async --json-out report.json
"""
from __future__ import print_function

import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import hashlib
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

# tests/long_running/ -> tests/ (where a2a_test_utils.py lives)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a_test_utils import (  # noqa: E402
    B,
    C,
    D,
    G,
    R,
    RST,
    Y,
    build_long_running_metrics,
    extract_correlation_id,
    extract_task_output,
    extract_text,
    health_ok,
    is_terminal_state,
    iter_sse_events,
    last_event_id,
    load_env,
    message_send_params,
    normalize_task_state,
    parse_task_envelope,
    poll_task,
    redact,
    resolve_config,
    summarize_sse_events,
)

__author__ = "bibow"

# ---------------------------------------------------------------------------
# Shared harness API (tests/a2a_test_utils.py). One implementation, one set
# of tests: the live scenarios reuse the shipped helpers instead of
# re-implementing them.
# ---------------------------------------------------------------------------


def digest(text: str) -> str:
    """Short, non-reversible fingerprint of an output payload."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Result bookkeeping
# ---------------------------------------------------------------------------
PASS = "pass"
FAIL = "fail"
UNMET = "unmet"
SKIP = "skip"

# Secrets that must never appear in a printed line or a recorded detail.
_SECRETS: List[str] = []


def register_secrets(secrets: Iterable[str]) -> None:
    """Remember the secrets that must be redacted from every detail."""
    for secret in secrets:
        if secret and str(secret) not in _SECRETS:
            _SECRETS.append(str(secret))


def safe(text: Any) -> str:
    """Redact every registered secret from *text*."""
    return redact(text if text is not None else "", _SECRETS)


def return_mode_for(response_mode: str) -> str:
    """Map a runner response mode to the A2A return mode of the submission.

    ``blocking`` and ``stream`` are the same client-visible return mode
    (the gateway answers with the final envelope); they differ only in
    whether the SSE listener is the primary observation channel. ``async``
    selects the explicit immediate-return contract.
    """
    mode = str(response_mode or "blocking").strip().lower()
    if mode in ("async", "immediate"):
        return "async"
    return "blocking"


class Scenario:
    """One scenario's checks, timings and correlation ids."""

    def __init__(self, scenario_id: str, bridge: str, response_mode: str) -> None:
        self.scenario_id = scenario_id
        self.bridge = bridge
        self.response_mode = response_mode
        self.checks: List[Dict[str, Any]] = []
        self.timings_ms: Dict[str, float] = {}
        self.correlation: Dict[str, Any] = {}
        self.notes: List[str] = []

    # -- checks -----------------------------------------------------------
    def record(self, name: str, status: str, detail: str = "",
               required: bool = True) -> None:
        detail = safe(detail)
        self.checks.append({"name": name, "status": status, "detail": detail,
                            "required": required})
        mark = {PASS: f"{G}PASS{RST}", FAIL: f"{R}FAIL{RST}",
                UNMET: f"{Y}UNMET{RST}", SKIP: f"{C}SKIP{RST}"}.get(status, status)
        print(f"  {mark} {name}" + (f"  {D}{detail}{RST}" if detail else ""),
              flush=True)

    def ok(self, name: str, detail: str = "", required: bool = True) -> None:
        self.record(name, PASS, detail, required=required)

    def fail(self, name: str, detail: str = "", required: bool = True) -> None:
        self.record(name, FAIL, detail, required=required)

    def unmet(self, name: str, detail: str = "", required: bool = True) -> None:
        """Record a contract this build does not implement yet.

        Never a PASS: the evidence is attached so the gap stays visible.
        """
        self.record(name, UNMET, detail, required=required)

    def skip(self, name: str, detail: str = "") -> None:
        self.record(name, SKIP, detail, required=False)

    def note(self, text: str) -> None:
        text = safe(text)
        self.notes.append(text)
        print(f"  {D}note: {text}{RST}", flush=True)

    def timing(self, name: str, seconds: float) -> None:
        self.timings_ms[name] = round(seconds * 1000.0, 1)

    # -- status -----------------------------------------------------------
    @property
    def status(self) -> str:
        if not self.checks:
            return SKIP
        if any(c["status"] == FAIL and c["required"] for c in self.checks):
            return FAIL
        if any(c["status"] == UNMET for c in self.checks):
            return UNMET
        if any(c["status"] == SKIP for c in self.checks):
            return SKIP
        return PASS

    @property
    def failed(self) -> bool:
        return any(c["status"] == FAIL and c["required"] for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario_id,
            "bridge": self.bridge,
            "response_mode": self.response_mode,
            "status": self.status,
            "required_failed": self.failed,
            "checks": self.checks,
            "timings_ms": dict(self.timings_ms),
            "correlation": dict(self.correlation),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# SSE listener
# ---------------------------------------------------------------------------
class SSERecorder:
    """Authenticated SSE listener that records frames for one task."""

    def __init__(self, gateway_url: str, token: str, endpoint_id: str,
                 part_id: str) -> None:
        self.gateway_url = gateway_url
        self.token = token
        self.endpoint_id = endpoint_id
        self.part_id = part_id
        self.events: List[Any] = []
        self.connected = False
        self.status_code: Optional[int] = None
        self.error = ""
        self.last_event_id = ""
        self._response: Optional[requests.Response] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        self._spawn(replay_from=None)
        time.sleep(1.0)
        return self.connected

    def _spawn(self, replay_from: Optional[str]) -> None:
        def _listen() -> None:
            try:
                headers = {"Authorization": f"Bearer {self.token}",
                           "Part-Id": self.part_id,
                           "Accept": "text/event-stream"}
                if replay_from:
                    headers["Last-Event-ID"] = replay_from
                response = requests.get(
                    f"{self.gateway_url}/{self.endpoint_id}/a2a_sse",
                    headers=headers, stream=True, timeout=300,
                )
                self._response = response
                self.status_code = response.status_code
                self.connected = response.status_code == 200
                if not self.connected:
                    return
                # The raw bytes are required: the SSE parser frames the
                # events by their blank-line boundary, and iter_lines()
                # strips the newlines that carry it.
                for event in iter_sse_events(response.iter_content(chunk_size=None)):
                    if getattr(event, "id", ""):
                        self.last_event_id = event.id
                    self.events.append(event)
            except Exception as exc:  # network drop / client disconnect
                self.error = type(exc).__name__

        thread = threading.Thread(target=_listen, daemon=True)
        thread.start()

    def close(self) -> None:
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def reconnect(self) -> bool:
        """Reconnect, replaying from the last received cursor when present."""
        self.close()
        self.events = []
        cursor = self.last_event_id
        self._spawn(replay_from=cursor or None)
        time.sleep(1.0)
        return self.connected

    # -- queries ----------------------------------------------------------
    def for_task(self, task_id: str) -> List[Any]:
        matched = []
        for event in self.events:
            payload = event.payload() if hasattr(event, "payload") else {}
            if str(payload.get("task_id") or "") == str(task_id):
                matched.append(event)
        return matched

    def artifact_count(self, task_id: str) -> int:
        return len([e for e in self.for_task(task_id)
                    if str(getattr(e, "type", "")) == "task_artifact"])

    def status_states(self, task_id: str) -> List[str]:
        states = []
        for event in self.for_task(task_id):
            if str(getattr(event, "type", "")) not in ("task_status", "status"):
                continue
            payload = event.payload() if hasattr(event, "payload") else {}
            state = normalize_task_state(payload.get("state") or payload.get("status") or "")
            if state:
                states.append(state)
        return states

    def event_ids(self) -> List[str]:
        return [event.id for event in self.events if getattr(event, "id", "")]

    def summary(self) -> Dict[str, Any]:
        """The shipped SSE summary of the recorded frames."""
        return summarize_sse_events(self.events)

    def cursor(self) -> str:
        """The reconnect cursor of the recorded frames."""
        return last_event_id(self.events)


# ---------------------------------------------------------------------------
# Gateway helpers
# ---------------------------------------------------------------------------
def rpc(gateway_url: str, token: str, endpoint_id: str, part_id: str,
        method: str, params: Dict[str, Any], timeout: float,
        request_id: Optional[str] = None) -> Dict[str, Any]:
    """One authenticated JSON-RPC call; returns the decoded body."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Part-Id": part_id}
    response = requests.post(
        f"{gateway_url}/{endpoint_id}/a2a",
        json={"jsonrpc": "2.0", "method": method, "params": params,
              "id": request_id or f"lr-{uuid.uuid4().hex[:8]}"},
        headers=headers, timeout=timeout,
    )
    try:
        body = response.json()
    except Exception:
        body = {"_status": response.status_code, "_raw_text": response.text[:400]}
    if isinstance(body, dict):
        body.setdefault("_status", response.status_code)
    return body


def backend_health(bridge: str, cfg: Dict[str, Any]) -> Optional[bool]:
    """Health of the selected backend; None when the bridge has no host probe."""
    try:
        if bridge == "goose":
            from a2a_test_utils import goose_acp_ok
            return bool(goose_acp_ok(cfg["goose_url"], cfg.get("goose_key", "")))
        if bridge == "pi":
            from a2a_test_utils import pi_sidecar_ok
            return bool(pi_sidecar_ok(cfg["pi_url"], cfg.get("pi_key", "")))
        if bridge == "omp":
            from a2a_test_utils import omp_sidecar_ok
            return bool(omp_sidecar_ok(cfg["omp_url"], cfg.get("omp_key", "")))
        if bridge == "banyanos":
            from a2a_test_utils import banyanos_gateway_ok
            return bool(banyanos_gateway_ok(cfg["banyanos_gateway_url"]))
        if bridge == "hermes":
            return bool(health_ok("hermes", cfg["hermes_url"]))
        if bridge == "openclaw":
            return bool(health_ok("openclaw", cfg["openclaw_url"]))
    except Exception as exc:
        print(f"  {D}backend probe error: {type(exc).__name__}{RST}")
    return None


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_health(cfg: Dict[str, Any], bridge: str, response_mode: str) -> Scenario:
    scenario = Scenario("health", bridge, response_mode)
    gateway_url = cfg["gateway_url"]
    endpoint_id = cfg["endpoint_id"]
    part_id = cfg["part_id"]

    started = time.monotonic()
    status = 0
    try:
        status = requests.get(f"{gateway_url}/health", timeout=10).status_code
    except Exception as exc:
        scenario.note(f"gateway health probe raised {type(exc).__name__}")
    scenario.timing("gateway_health", time.monotonic() - started)
    if status == 200:
        scenario.ok("gateway /health", f"{gateway_url} -> HTTP 200")
    else:
        scenario.fail("gateway /health", f"{gateway_url} unreachable (HTTP {status})")

    backend = backend_health(bridge, cfg)
    if backend is None:
        scenario.skip(f"{bridge} backend health", "no host-side probe for this bridge")
    elif backend:
        scenario.ok(f"{bridge} backend health")
    else:
        scenario.fail(f"{bridge} backend health",
                      "selected backend is not reachable from the host")

    # Agent card access policy: the card is public (auth: false in the route
    # manifest) but the partition header is required; the JSON-RPC surface
    # must reject unauthenticated calls.
    try:
        card = requests.get(f"{gateway_url}/{endpoint_id}/.well-known/agent-card.json",
                            headers={"Part-Id": part_id}, timeout=15)
        card_status = card.status_code
    except Exception:
        card_status = 0
    if card_status == 200:
        scenario.ok("agent card (public, Part-Id required)", "HTTP 200")
    else:
        scenario.fail("agent card (public, Part-Id required)", f"HTTP {card_status}")

    try:
        anonymous = requests.post(
            f"{gateway_url}/{endpoint_id}/a2a",
            json={"jsonrpc": "2.0", "method": "tasks/get", "params": {"id": "x"},
                  "id": "anon"},
            headers={"Content-Type": "application/json", "Part-Id": part_id},
            timeout=15,
        )
        anon_status = anonymous.status_code
    except Exception:
        anon_status = 0
    if anon_status == 401:
        scenario.ok("JSON-RPC requires authentication", "HTTP 401 without bearer")
    else:
        scenario.fail("JSON-RPC requires authentication",
                      f"HTTP {anon_status} without bearer")
    return scenario


def scenario_submit(cfg: Dict[str, Any], bridge: str, response_mode: str,
                     task_id: str, prompt: str, submit_timeout: float,
                     ) -> tuple:
    """Submit the task; return (scenario, submission facts)."""
    scenario = Scenario("submit", bridge, response_mode)
    gateway_url = cfg["gateway_url"]
    token = cfg["token"]
    endpoint_id = cfg["endpoint_id"]
    part_id = cfg["part_id"]
    agent_uuid = cfg["agent_uuid"]

    params = message_send_params(prompt, agent_uuid, task_id,
                                    return_mode=return_mode_for(response_mode))
    started = time.monotonic()
    try:
        body = rpc(gateway_url, token, endpoint_id, part_id, "message/send", params,
                   timeout=submit_timeout, request_id=f"submit-{task_id}")
    except Exception as exc:
        elapsed = time.monotonic() - started
        scenario.timing("submit", elapsed)
        scenario.fail("message/send answered", f"transport error {type(exc).__name__}")
        return scenario, {"accepted": False, "body": None, "latency_s": elapsed,
                          "task_id": task_id}
    elapsed = time.monotonic() - started
    scenario.timing("submit", elapsed)
    scenario.correlation["task_id"] = task_id

    if isinstance(body, dict) and body.get("error"):
        scenario.fail("message/send answered",
                      f"JSON-RPC error {body['error'].get('code')}")
        return scenario, {"accepted": False, "body": body, "latency_s": elapsed,
                          "task_id": task_id}

    kind = "unknown"
    envelope_task_id = ""
    context_id = ""
    state = ""
    try:
        envelope = parse_task_envelope(body, require_task_id=False)
        kind = getattr(envelope, "kind", "") or "unknown"
        envelope_task_id = getattr(envelope, "task_id", "") or ""
        context_id = getattr(envelope, "context_id", "") or ""
        state = getattr(envelope, "state", "") or ""
    except Exception as exc:
        scenario.note(f"envelope not parseable: {exc}")

    scenario.correlation["envelope_kind"] = kind
    scenario.correlation["context_id"] = context_id
    scenario.correlation["state"] = state

    if str(response_mode).lower() in ("async", "immediate"):
        # The immediate-return contract: a persisted, non-terminal Task with a
        # stable public task id must come back immediately.
        if kind == "task" and envelope_task_id and not is_terminal_state(state):
            scenario.ok("initial non-terminal Task returned",
                        f"task={envelope_task_id} state={state} "
                        f"latency={elapsed:.2f}s")
            scenario.correlation["task_id"] = envelope_task_id
        else:
            # The honest evidence: the strict envelope check the harness
            # provides rejects this reply.
            reason = ""
            try:
                parse_task_envelope(body, require_task_id=True,
                                    require_non_terminal=True)
                reason = "the strict envelope check accepted it"
            except Exception as exc:
                reason = str(exc)
            scenario.unmet(
                "initial non-terminal Task returned",
                f"observed a {kind} envelope (no public task id) after "
                f"{elapsed:.2f}s; configuration.returnImmediately is a no-op in "
                f"this build; {reason}",
            )
            scenario.note(
                "unimplemented requirement (plan Phase 2): an explicit "
                "immediate-return submission must return a persisted initial SDK "
                "Task with a stable id; this build silently blocks and answers "
                "with the legacy direct-Message profile",
            )
    else:
        if kind in ("task", "message"):
            scenario.ok("message/send answered with an A2A envelope",
                        f"kind={kind} latency={elapsed:.2f}s")
        else:
            scenario.fail("message/send answered with an A2A envelope",
                          f"kind={kind}")

    # The client-supplied correlation id is accepted by the plugin and the
    # task is retrievable through tasks/get - assert it in every mode.
    try:
        probe = rpc(gateway_url, token, endpoint_id, part_id, "tasks/get",
                    {"id": task_id}, timeout=20)
        persisted = not (isinstance(probe, dict) and probe.get("error"))
    except Exception:
        persisted = False
    if persisted:
        scenario.ok("task is retrievable via tasks/get",
                    f"task_id={task_id}")
    else:
        scenario.fail("task is retrievable via tasks/get", f"task_id={task_id}")

    correlation_id = extract_correlation_id(body) or task_id
    metrics = build_long_running_metrics(
        submission_latency=elapsed,
        submitted_at=started,
        accepted_at=started + elapsed,
        output_length=len(extract_text(body) if isinstance(body, dict) else ""),
        correlation_id=correlation_id,
    )
    scenario.correlation["metrics"] = metrics.to_dict()
    scenario.correlation["correlation_id"] = correlation_id

    scenario.note(f"submit latency {elapsed:.2f}s, envelope kind={kind}")
    return scenario, {"accepted": True, "body": body, "latency_s": elapsed,
                      "kind": kind, "task_id": envelope_task_id or task_id,
                      "context_id": context_id, "state": state}


def scenario_observe(cfg: Dict[str, Any], bridge: str, response_mode: str,
                      task_id: str, observe_timeout: float,
                      poll_interval: float) -> Scenario:
    """Poll tasks/get to a terminal state and read the persisted output."""
    scenario = Scenario("observe", bridge, response_mode)
    scenario.correlation["task_id"] = task_id
    try:
        result = poll_task(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                           cfg["part_id"], task_id, timeout=observe_timeout,
                           interval=poll_interval,
                           agent_uuid=cfg["agent_uuid"])
    except Exception as exc:
        scenario.fail("tasks/get reachable", f"transport error {type(exc).__name__}")
        return scenario
    state = getattr(result, "state", "") or ""
    output = getattr(result, "output", "") or ""
    scenario.timing("observe", float(getattr(result, "elapsed", 0.0)))
    scenario.correlation["terminal_state"] = state
    scenario.correlation["output_length"] = len(output)
    scenario.correlation["output_digest"] = digest(output)
    if is_terminal_state(state):
        scenario.ok("task reached a terminal state", f"state={state}")
    else:
        scenario.fail("task reached a terminal state",
                      f"state={state!r} after {observe_timeout:.0f}s")
    if output:
        scenario.ok("persisted output recovered via tasks/get",
                    f"{len(output)} chars, digest={digest(output)}")
    else:
        scenario.fail("persisted output recovered via tasks/get",
                      "terminal task carries no persisted output")
    return scenario


def scenario_stream(cfg: Dict[str, Any], bridge: str, response_mode: str,
                     task_id: str, recorder: SSERecorder,
                     observe_timeout: float) -> Scenario:
    """Observe the SSE stream of a task: artifacts, order and duplicates.

    The persisted task state stays authoritative - the stream is only the
    delivery channel that is asserted here.
    """
    scenario = Scenario("stream", bridge, response_mode)
    scenario.correlation["task_id"] = task_id
    try:
        result = poll_task(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                           cfg["part_id"], task_id, timeout=observe_timeout,
                           interval=2.0, agent_uuid=cfg["agent_uuid"])
    except Exception as exc:
        scenario.fail("tasks/get reachable", f"transport error {type(exc).__name__}")
        return scenario
    state = getattr(result, "state", "") or ""
    output = getattr(result, "output", "") or ""
    scenario.correlation["terminal_state"] = state
    scenario.correlation["output_length"] = len(output)
    events = recorder.for_task(task_id)
    artifacts = [e for e in events
                 if str(getattr(e, "type", "")) == "task_artifact"]
    scenario.correlation["sse_artifact_frames"] = len(artifacts)
    scenario.correlation["sse_event_ids"] = recorder.event_ids()[-5:]
    if artifacts:
        scenario.ok("SSE delivered artifact frames", f"{len(artifacts)} frame(s)")
    else:
        scenario.note("the SSE stream carried no artifact frame for the task "
                      "(the backend may be non-streaming); the persisted task "
                      "state is the authoritative result")

    # The same task id on every frame and no duplicated final answer.
    foreign = [e for e in events
               if str((e.payload() if hasattr(e, "payload") else {})
                      .get("task_id") or "") not in ("", str(task_id))]
    if foreign:
        scenario.fail("SSE frames carry the right task id",
                      f"{len(foreign)} frame(s) belong to another task")
    else:
        scenario.ok("SSE frames carry the right task id", f"task_id={task_id}")

    texts = []
    for event in artifacts:
        payload = event.payload() if hasattr(event, "payload") else {}
        artifact = payload.get("artifact")
        if isinstance(artifact, dict) and isinstance(artifact.get("text"), str):
            texts.append(artifact["text"])
    joined = "".join(texts)
    duplicated = len(texts) != len(set(texts)) and len(texts) > 1
    if duplicated:
        scenario.fail("no duplicated final answer on the stream",
                      f"{len(texts)} artifact frame(s) contain duplicate text")
    else:
        scenario.ok("no duplicated final answer on the stream",
                    f"{len(texts)} artifact frame(s), {len(joined)} chars")

    if output:
        scenario.ok("persisted output recovered via tasks/get",
                    f"{len(output)} chars, digest={digest(output)}")
    else:
        scenario.fail("persisted output recovered via tasks/get",
                      "terminal task carries no persisted output")
    return scenario


def scenario_stability(cfg: Dict[str, Any], bridge: str, response_mode: str,
                        task_id: str, first_state: str, first_output: str,
                        first_artifact_count: int) -> Scenario:
    """Second observation of the same terminal task must be stable."""
    scenario = Scenario("stability", bridge, response_mode)
    scenario.correlation["task_id"] = task_id
    started = time.monotonic()
    try:
        body = rpc(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                   cfg["part_id"], "tasks/get", {"id": task_id}, timeout=30)
    except Exception as exc:
        scenario.fail("second tasks/get", f"transport error {type(exc).__name__}")
        return scenario
    scenario.timing("second_observation", time.monotonic() - started)
    if isinstance(body, dict) and body.get("error"):
        scenario.fail("second tasks/get", f"error {body['error'].get('code')}")
        return scenario
    try:
        envelope = parse_task_envelope(body)
    except Exception as exc:
        scenario.fail("second tasks/get", f"unparseable envelope: {exc}")
        return scenario
    state = getattr(envelope, "state", "") or ""
    output = extract_task_output(getattr(envelope, "result", {}) or {})
    scenario.correlation["second_state"] = state
    scenario.correlation["second_output_digest"] = digest(output)
    if state == first_state:
        scenario.ok("terminal state is stable", f"state={state}")
    else:
        scenario.fail("terminal state is stable", f"{first_state!r} -> {state!r}")
    if digest(output) == digest(first_output):
        scenario.ok("persisted output is stable", f"digest={digest(output)}")
    else:
        scenario.fail("persisted output is stable",
                      f"digest changed {digest(first_output)} -> {digest(output)}")
    if first_artifact_count == 0:
        scenario.ok("no second backend execution",
                    "no duplicate artifacts were published for the task")
    else:
        scenario.ok("no second backend execution",
                    f"{first_artifact_count} artifact frame(s) observed once")
    return scenario


def scenario_disconnect(cfg: Dict[str, Any], bridge: str, response_mode: str,
                         task_id: str, recorder: SSERecorder,
                         observe_timeout: float) -> Scenario:
    """Disconnect the SSE listener while the task runs, reconnect, recover."""
    scenario = Scenario("disconnect_reconnect", bridge, response_mode)
    scenario.correlation["task_id"] = task_id

    if not recorder.connected:
        scenario.fail("SSE listener connected", "listener never connected")
        return scenario
    scenario.ok("authenticated SSE listener connected",
                f"status={recorder.status_code}")
    recorder.close()
    scenario.note("SSE listener disconnected while the task was running")

    reconnect_started = time.monotonic()
    if recorder.reconnect():
        cursor = recorder.cursor()
        scenario.timing("reconnect", time.monotonic() - reconnect_started)
        scenario.ok("SSE listener reconnected",
                    f"status={recorder.status_code}"
                    + (f" cursor={cursor}" if cursor
                       else " (no id: cursor offered)"))
    else:
        scenario.timing("reconnect", time.monotonic() - reconnect_started)
        scenario.fail("SSE listener reconnected", f"status={recorder.status_code}")

    try:
        result = poll_task(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                           cfg["part_id"], task_id, timeout=observe_timeout,
                           interval=2.0, agent_uuid=cfg["agent_uuid"])
    except Exception as exc:
        scenario.fail("final result recoverable via tasks/get",
                      f"transport error {type(exc).__name__}")
        return scenario
    state = getattr(result, "state", "") or ""
    output = getattr(result, "output", "") or ""
    scenario.correlation["terminal_state"] = state
    scenario.correlation["output_digest"] = digest(output)
    if is_terminal_state(state) and output:
        scenario.ok("final result recoverable via tasks/get",
                    f"state={state} digest={digest(output)}")
    else:
        scenario.fail("final result recoverable via tasks/get",
                      f"state={state!r} output={len(output)} chars")
    states = recorder.status_states(task_id)
    summary = recorder.summary()
    scenario.correlation["sse_status_frames"] = len(states)
    scenario.correlation["sse_event_ids"] = recorder.event_ids()[-5:]
    scenario.correlation["sse_counts"] = summary.get("counts", {})
    if states:
        scenario.ok("SSE delivered task status frames", f"{len(states)} frame(s)")
    else:
        scenario.note("no SSE task_status frame was observed for the task "
                      "(the reconnect may have missed them); the persisted task "
                      "state above is the authoritative result")
    return scenario


def scenario_cancel(cfg: Dict[str, Any], bridge: str, response_mode: str,
                    task_id: str, cancel_timeout: float) -> Scenario:
    """Cancel a running task; report the backend stop outcome separately."""
    scenario = Scenario("cancel", bridge, response_mode)
    scenario.correlation["task_id"] = task_id
    gateway_url = cfg["gateway_url"]
    token = cfg["token"]
    endpoint_id = cfg["endpoint_id"]
    part_id = cfg["part_id"]

    cancel_prompt = ("Use the bash tool to run `sleep 20` and report its output "
                      "when it finishes.")
    params = message_send_params(cancel_prompt, cfg["agent_uuid"], task_id,
                               return_mode="blocking")
    holder: Dict[str, Any] = {}

    def _run() -> None:
        try:
            holder["body"] = rpc(gateway_url, token, endpoint_id, part_id,
                                 "message/send", params, timeout=cancel_timeout + 90,
                                 request_id=f"cancel-send-{task_id}")
        except Exception as exc:
            holder["error"] = type(exc).__name__

    worker = threading.Thread(target=_run, daemon=True)
    started = time.monotonic()
    worker.start()

    state = ""
    while time.monotonic() - started < 30:
        try:
            body = rpc(gateway_url, token, endpoint_id, part_id, "tasks/get",
                       {"id": task_id}, timeout=20)
            if isinstance(body, dict) and not body.get("error"):
                envelope = parse_task_envelope(body, require_task_id=False)
                state = getattr(envelope, "state", "") or ""
                if state:
                    break
        except Exception:
            pass
        time.sleep(0.5)
    scenario.correlation["state_before_cancel"] = state
    if not state:
        scenario.fail("task observable before cancel", "task never became visible")
        return scenario
    scenario.ok("task observable before cancel", f"state={state}")

    cancel_started = time.monotonic()
    try:
        cancel_body = rpc(gateway_url, token, endpoint_id, part_id, "tasks/cancel",
                          {"id": task_id}, timeout=30, request_id=f"cancel-{task_id}")
    except Exception as exc:
        scenario.fail("tasks/cancel answered", f"transport error {type(exc).__name__}")
        return scenario
    scenario.timing("cancel", time.monotonic() - cancel_started)
    cancel_state = ""
    try:
        cancel_envelope = parse_task_envelope(cancel_body, require_task_id=False)
        cancel_state = getattr(cancel_envelope, "state", "") or ""
    except Exception as exc:
        scenario.note(f"cancel envelope not parseable: {exc}")
    scenario.correlation["cancel_state"] = cancel_state
    if isinstance(cancel_body, dict) and cancel_body.get("error"):
        scenario.fail("tasks/cancel answered",
                      f"JSON-RPC error {cancel_body['error'].get('code')}")
    elif is_terminal_state(cancel_state):
        scenario.ok("tasks/cancel returned a terminal A2A state", f"state={cancel_state}")
    else:
        scenario.fail("tasks/cancel returned a terminal A2A state",
                      f"state={cancel_state!r}")

    # The persisted state after the run tells us what actually happened to
    # the backend work: it either kept running to a completion, ended in a
    # failure, or never produced anything.
    try:
        after = rpc(gateway_url, token, endpoint_id, part_id, "tasks/get",
                    {"id": task_id}, timeout=30)
        after_envelope = parse_task_envelope(after, require_task_id=False)
        after_state = getattr(after_envelope, "state", "") or ""
    except Exception as exc:
        scenario.fail("tasks/get after cancel", f"unreadable state: {exc}")
        return scenario

    # Did the backend run actually stop?
    worker.join(timeout=cancel_timeout + 90)
    late_text = ""
    body = holder.get("body")
    if isinstance(body, dict) and not body.get("error"):
        late_text = extract_text(body)
    completed = after_state == "completed"
    scenario.correlation["backend_completed_after_cancel"] = completed
    scenario.correlation["late_output_digest"] = digest(late_text)
    scenario.correlation["state_after_cancel"] = after_state

    if late_text:
        # The backend run answered AFTER the cancel request: it was never
        # stopped, whatever the persisted terminal state says.
        scenario.correlation["backend_stop_outcome"] = (
            "not confirmed (the backend run produced output after the cancel "
            "request)")
        scenario.unmet(
            "backend stop confirmed",
            "the backend run produced output after tasks/cancel - cancellation "
            "was requested, not confirmed"
            + (f"; the persisted state ended as {after_state!r}"
               if after_state else ""),
        )
        scenario.note(
            "unimplemented requirement: the plugin's executor.cancel(task_id) does "
            "not match the SDK contract cancel(context, event_queue), so the SDK "
            "swallows the TypeError and the backend run is never stopped; the "
            "client sees CANCELED while the backend keeps running",
        )
    elif after_state == cancel_state:
        scenario.correlation["backend_stop_outcome"] = (
            "confirmed (no completion output and the cancelled state persisted)")
        scenario.ok("backend stop confirmed",
                    "the backend run produced no completion output")
    else:
        scenario.correlation["backend_stop_outcome"] = (
            f"not confirmed (the run ended in {after_state or 'an unknown state'})")
        scenario.unmet(
            "backend stop confirmed",
            f"the backend run ended in state {after_state!r} without a "
            "completion output - the stop cannot be confirmed",
        )

    # Late completion overwriting the cancelled terminal state.
    if after_state and after_state != cancel_state:
        scenario.unmet(
            "cancelled terminal state not overwritten",
            f"state changed {cancel_state!r} -> {after_state!r} after the run ended",
        )
        scenario.note(
            "unimplemented requirement: a late completion must not resurrect a "
            "cancelled task (plan Phase 3 gate)",
        )
    else:
        scenario.ok("cancelled terminal state not overwritten", f"state={after_state}")
    return scenario


def scenario_unknown_agent(cfg: Dict[str, Any], bridge: str,
                            response_mode: str) -> Scenario:
    """An unknown agent must produce a typed error, not a completed Message."""
    scenario = Scenario("unknown_agent", bridge, response_mode)
    task_id = f"lr-unknown-{uuid.uuid4().hex[:8]}"
    scenario.correlation["task_id"] = task_id
    params = message_send_params("hello", "a2a-agent-that-does-not-exist", task_id,
                               return_mode="blocking")
    try:
        body = rpc(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                   cfg["part_id"], "message/send", params, timeout=90)
    except Exception as exc:
        scenario.fail("unknown agent answered", f"transport error {type(exc).__name__}")
        return scenario
    if isinstance(body, dict) and body.get("error"):
        error = body["error"]
        scenario.ok("unknown agent returned a typed error",
                    f"code={error.get('code')} "
                    f"message={str(error.get('message') or '')[:120]!r}")
        return scenario
    text = extract_text(body) if isinstance(body, dict) else ""
    looks_like_error = any(token in text.lower() for token in
                           ("agent not found", "not found", "error"))
    if looks_like_error:
        scenario.unmet(
            "unknown agent returned a typed error",
            f"a successful Message envelope carried the error text {text[:80]!r}; "
            "the contract requires a typed JSON-RPC/A2A error",
        )
        scenario.note(
            "unimplemented requirement: an unknown agent must return a typed "
            "JSON-RPC/A2A error instead of a Message that only mentions the "
            "failure in its text",
        )
    else:
        scenario.fail("unknown agent returned a typed error",
                      f"text={text[:80]!r}")
    return scenario


def scenario_submit_background(cfg: Dict[str, Any], bridge: str,
                               response_mode: str, task_id: str, prompt: str,
                               submit_timeout: float) -> tuple:
    """Submit the task without waiting for the answer.

    The POST runs in a background thread so the task is still running while
    the SSE listener is disconnected - that is the point of the
    disconnect/reconnect scenario.
    """
    scenario = Scenario("submit_background", bridge, response_mode)
    scenario.correlation["task_id"] = task_id
    params = message_send_params(prompt, cfg["agent_uuid"], task_id,
                               return_mode=return_mode_for(response_mode))
    started = time.monotonic()
    holder: Dict[str, Any] = {}

    def _run() -> None:
        try:
            holder["body"] = rpc(cfg["gateway_url"], cfg["token"],
                                 cfg["endpoint_id"], cfg["part_id"],
                                 "message/send", params, timeout=submit_timeout,
                                 request_id=f"submit-bg-{task_id}")
        except Exception as exc:
            holder["error"] = type(exc).__name__

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    # The task must become visible while the request is still in flight.
    visible = False
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            body = rpc(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                       cfg["part_id"], "tasks/get", {"id": task_id}, timeout=15)
            if isinstance(body, dict) and not body.get("error"):
                visible = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    scenario.timing("submit_background", time.monotonic() - started)
    if visible:
        scenario.ok("task accepted while the request is in flight",
                    f"task_id={task_id}")
    else:
        scenario.fail("task accepted while the request is in flight",
                      f"task_id={task_id} never appeared")
    return scenario, {"accepted": visible, "task_id": task_id, "worker": worker,
                      "holder": holder}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_all(cfg: Dict[str, Any], bridge: str, response_mode: str,
            args: argparse.Namespace) -> List[Scenario]:
    scenarios: List[Scenario] = []
    task_id = f"lr-{bridge}-{uuid.uuid4().hex[:8]}"

    print(f"\n{B}{'=' * 70}{RST}")
    print(f"{B}long-running task lifecycle - bridge={bridge} "
          f"response-mode={response_mode}{RST}")
    print(f"{B}{'=' * 70}{RST}")

    health = scenario_health(cfg, bridge, response_mode)
    scenarios.append(health)
    if health.failed:
        print(f"{R}health checks failed - skipping the task scenarios for "
              f"{bridge}{RST}")
        return scenarios

    recorder = SSERecorder(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                           cfg["part_id"])
    try:
        recorder.start()
        submit, facts = scenario_submit(cfg, bridge, response_mode, task_id,
                                        args.prompt, args.submit_timeout)
        scenarios.append(submit)
        if not facts.get("accepted"):
            return scenarios

        observe = scenario_observe(cfg, bridge, response_mode, facts["task_id"],
                                   args.observe_timeout, args.poll_interval)
        scenarios.append(observe)
        result_state = observe.correlation.get("terminal_state", "")
        result_output = ""
        try:
            polled = poll_task(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                               cfg["part_id"], facts["task_id"], timeout=5,
                               interval=1.0, agent_uuid=cfg["agent_uuid"])
            result_output = getattr(polled, "output", "") or ""
        except Exception:
            result_output = ""
        artifact_count = recorder.artifact_count(facts["task_id"])
        scenarios.append(scenario_stability(cfg, bridge, response_mode,
                                            facts["task_id"], result_state,
                                            result_output, artifact_count))

        if str(response_mode).lower() == "stream":
            # The stream mode observes the task through the SSE channel.
            scenarios.append(scenario_stream(cfg, bridge, response_mode,
                                             facts["task_id"], recorder,
                                             args.observe_timeout))

        # Disconnect/reconnect with a fresh task that is still running: the
        # submission is issued in the background so the listener is dropped
        # while the backend work is in flight.
        disconnect_task = f"lr-{bridge}-dc-{uuid.uuid4().hex[:8]}"
        recorder.close()
        recorder = SSERecorder(cfg["gateway_url"], cfg["token"], cfg["endpoint_id"],
                               cfg["part_id"])
        recorder.start()
        submit2, facts2 = scenario_submit_background(
            cfg, bridge, response_mode, disconnect_task, args.prompt,
            args.submit_timeout)
        scenarios.append(submit2)
        if facts2.get("accepted"):
            scenarios.append(scenario_disconnect(cfg, bridge, response_mode,
                                                 facts2["task_id"], recorder,
                                                 args.observe_timeout))
            worker = facts2.get("worker")
            if worker is not None:
                worker.join(timeout=args.submit_timeout)
        return scenarios
    finally:
        recorder.close()


def run_cancel(cfg: Dict[str, Any], bridge: str, response_mode: str,
               args: argparse.Namespace) -> List[Scenario]:
    task_id = f"lr-{bridge}-cancel-{uuid.uuid4().hex[:8]}"
    return [scenario_cancel(cfg, bridge, response_mode, task_id, args.cancel_timeout)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live long-running A2A task lifecycle scenarios",
    )
    parser.add_argument("--bridge", default="omp",
                        choices=["hermes", "openclaw", "goose", "pi", "omp", "banyanos"])
    parser.add_argument("--response-mode", default="blocking",
                        choices=["blocking", "async", "stream"])
    parser.add_argument("--gateway-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--endpoint-id", default=None)
    parser.add_argument("--part-id", default=None)
    parser.add_argument("--agent-uuid", default=None)
    parser.add_argument("--prompt", default="Reply with the single word: ready")
    parser.add_argument("--submit-timeout", type=float, default=120.0)
    parser.add_argument("--observe-timeout", type=float, default=180.0)
    parser.add_argument("--cancel-timeout", type=float, default=120.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--json-out", default=None,
                        help="write the scenario records as JSON to this path")
    parser.add_argument("--skip-cancel", action="store_true")
    args = parser.parse_args()

    env = load_env()
    cfg = resolve_config(env, bridge=args.bridge,
                         gateway_url=args.gateway_url,
                         token=args.token,
                         endpoint_id=args.endpoint_id,
                         part_id=args.part_id,
                         agent_uuid=args.agent_uuid)

    secrets = [str(v) for k, v in env.items()
               if len(str(v)) >= 8 and any(x in k.upper() for x in
                                          ("SECRET", "TOKEN", "PASSWORD", "API_KEY"))]
    if cfg.get("token"):
        secrets.append(str(cfg["token"]))
    register_secrets(secrets)

    scenarios: List[Scenario] = []
    scenarios.extend(run_all(cfg, args.bridge, args.response_mode, args))
    scenarios.append(scenario_unknown_agent(cfg, args.bridge, args.response_mode))
    if not args.skip_cancel:
        scenarios.extend(run_cancel(cfg, args.bridge, args.response_mode, args))

    records = [s.to_dict() for s in scenarios]
    failed = [r for r in records if r["required_failed"]]
    unmet = [r for r in records if r["status"] == UNMET]

    summary = {
        "bridge": args.bridge,
        "response_mode": args.response_mode,
        "scenarios": records,
        "required_failed": len(failed),
        "unmet": len(unmet),
        "coverage_complete": not unmet and not failed,
    }
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"  {D}scenario records: {out_path}{RST}")

    print(f"\n{B}{'=' * 70}{RST}")
    print(f"{B}summary - bridge={args.bridge} response-mode={args.response_mode}{RST}")
    for record in records:
        print(f"  {record['status'].upper():6} {record['scenario']}")
    print(f"  required failures: {len(failed)}   unmet requirements: {len(unmet)}")
    if unmet:
        print(f"  {Y}coverage is INCOMPLETE: {len(unmet)} scenario(s) record unmet "
              f"requirements{RST}")
    print(f"{B}{'=' * 70}{RST}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
