#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the long-running A2A task test suites and write a JSON report.

Usage:
    python scripts/run_long_running_tests.py --suite offline
    python scripts/run_long_running_tests.py --suite live --bridge omp
    python scripts/run_long_running_tests.py --suite live --bridge all --response-mode async

The runner:

* runs the offline harness suite and the existing offline tests as bounded
  subprocesses (the offline suite stops at the first failing step),
* runs the live scenarios of every selected bridge as bounded subprocesses
  with an overall deadline,
* redacts every secret from the captured output (no bearer token, no
  authenticated request body is ever printed),
* writes a machine-readable report to
  ``docs/test_results/long_running_tasks/<suite>-<timestamp>.json``.

Exit code 0 only when every selected scenario passed. A selected bridge that
cannot connect is FAILED; a bridge that was not selected is recorded as
SKIPPED with a reason.

The report shape is documented in
``docs/test_results/long_running_tasks/README.md``.
"""
from __future__ import print_function

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from a2a_test_utils import load_env, redact, redact_structure  # noqa: E402


BRIDGES = ["hermes", "openclaw", "goose", "pi", "omp", "banyanos"]
RESPONSE_MODES = ["blocking", "async", "stream"]
LIVE_SCRIPT = ROOT / "tests" / "long_running" / "test_task_lifecycle.py"
DEFAULT_REPORT_DIR = ROOT / "docs" / "test_results" / "long_running_tasks"

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
UNMET = "unmet"


def child_pythonpath(env: dict) -> str:
    """The PYTHONPATH the offline unit suite needs.

    The unit suite imports the A2A protocol plugin, which lives in the
    silvaengine source tree. The location is read from ``.env``
    (``SILVAENGINE_SRC``) - never hard-coded.
    """
    source = str(env.get("SILVAENGINE_SRC", "") or "").strip()
    parts = []
    if source:
        plugin_root = Path(source) / "a2a_protocol_plugin"
        if plugin_root.is_dir():
            parts.append(str(plugin_root))
    existing = os.environ.get("PYTHONPATH", "").strip()
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------
def new_record(scenario_id: str, bridge: str, response_mode: str) -> dict:
    """A single scenario record of the report."""
    return {
        "scenario": scenario_id,
        "bridge": bridge,
        "response_mode": response_mode,
        "status": SKIP,
        "required": True,
        "timings_ms": {},
        "correlation": {},
        "skip_reason": "",
        "error": "",
        "details": [],
    }


def summarize(records: list) -> dict:
    """Aggregate counts of a list of scenario records."""
    return {
        "required": len([r for r in records if r.get("required", True)]),
        "passed": len([r for r in records if r["status"] == PASS]),
        "failed": len([r for r in records if r["status"] == FAIL]),
        "skipped": len([r for r in records if r["status"] == SKIP]),
        "unmet": len([r for r in records if r["status"] == UNMET]),
    }


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------
def run_bounded(command: list, *, timeout: float, cwd: Path,
                secrets: list, label: str, pythonpath: str = "") -> dict:
    """Run *command* with an overall deadline; never leak secrets."""
    child_env = {**os.environ,
                 "PYTHONIOENCODING": "utf-8",
                 "PYTHONUNBUFFERED": "1",
                 "PYTHONDONTWRITEBYTECODE": "1"}
    if pythonpath:
        child_env["PYTHONPATH"] = pythonpath
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=str(cwd), env=child_env, text=True, encoding="utf-8",
            errors="replace", capture_output=True, timeout=timeout,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        code = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        parts = []
        for chunk in (exc.stdout, exc.stderr):
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            if chunk:
                parts.append(chunk)
        output = "\n".join(parts)
        code = 124
        timed_out = True
    elapsed = time.monotonic() - started
    return {
        "label": label,
        "command": [str(part) for part in command],
        "exit_code": code,
        "timed_out": timed_out,
        "elapsed_ms": round(elapsed * 1000.0, 1),
        "output": redact(output, secrets),
    }


# ---------------------------------------------------------------------------
# Offline suite
# ---------------------------------------------------------------------------
def run_offline(args: argparse.Namespace, secrets: list, pythonpath: str = "") -> list:
    """Run the offline harness suite and the existing offline tests."""
    records = []
    steps = [
        ([sys.executable, "-m", "pytest", "tests/unit/test_long_running_harness.py",
          "-q", "-o", "addopts="], "offline_harness", args.offline_timeout),
        ([sys.executable, "-m", "pytest", "tests/unit", "-q", "-o", "addopts="],
         "offline_unit", args.offline_timeout),
        ([sys.executable, "-m", "pytest", "tests/test_chatbot_stream.py", "-q",
          "-o", "addopts="], "offline_chatbot_stream", args.offline_timeout),
    ]
    for command, scenario_id, timeout in steps:
        if not (ROOT / "tests" / "unit" / "test_long_running_harness.py").exists() \
                and scenario_id == "offline_harness":
            record = new_record(scenario_id, "n/a", "n/a")
            record["status"] = SKIP
            record["required"] = False
            record["skip_reason"] = "tests/unit/test_long_running_harness.py is absent"
            records.append(record)
            continue
        print(f"[offline] {scenario_id}: {' '.join(command)}", flush=True)
        result = run_bounded(command, timeout=timeout, cwd=ROOT, secrets=secrets,
                             label=scenario_id, pythonpath=pythonpath)
        record = new_record(scenario_id, "n/a", "n/a")
        record["timings_ms"]["elapsed"] = result["elapsed_ms"]
        record["status"] = PASS if result["exit_code"] == 0 else FAIL
        if result["exit_code"] != 0:
            record["error"] = _tail(result["output"])
        records.append(record)
        print(f"[offline] {scenario_id}: {record['status'].upper()} "
              f"({result['elapsed_ms']} ms)", flush=True)
        if record["status"] == FAIL:
            # The offline suite stops after the first failing step.
            break
    return records


# ---------------------------------------------------------------------------
# Live suite
# ---------------------------------------------------------------------------
def run_live(args: argparse.Namespace, secrets: list) -> list:
    """Run the live scenarios of every selected bridge."""
    records = []
    selected = BRIDGES if args.bridge == "all" else [args.bridge]
    report_tmp = Path(args.report_dir)
    report_tmp.mkdir(parents=True, exist_ok=True)

    for bridge in BRIDGES:
        if bridge in selected:
            continue
        record = new_record("live", bridge, args.response_mode)
        record["status"] = SKIP
        record["required"] = False
        record["skip_reason"] = f"bridge '{bridge}' was not selected"
        records.append(record)

    for bridge in selected:
        json_out = report_tmp / f"live-{bridge}-{args.response_mode}.json"
        command = [sys.executable, str(LIVE_SCRIPT),
                   "--bridge", bridge,
                   "--response-mode", args.response_mode,
                   "--json-out", str(json_out),
                   "--submit-timeout", str(args.submit_timeout),
                   "--observe-timeout", str(args.observe_timeout),
                   "--cancel-timeout", str(args.cancel_timeout),
                   "--poll-interval", str(args.poll_interval)]
        print(f"[live] {bridge}: {' '.join(command)}", flush=True)
        result = run_bounded(command, timeout=args.live_timeout, cwd=ROOT,
                             secrets=secrets, label=f"live:{bridge}")
        print(f"[live] {bridge}: exit={result['exit_code']} "
              f"({result['elapsed_ms']} ms)", flush=True)
        if result["timed_out"]:
            print(result["output"][-2000:], flush=True)

        scenarios = []
        if json_out.exists():
            try:
                scenarios = json.loads(json_out.read_text(encoding="utf-8")).get(
                    "scenarios", [])
            except Exception:
                scenarios = []
        if not scenarios:
            record = new_record("live", bridge, args.response_mode)
            record["status"] = FAIL
            record["error"] = ("the live scenario subprocess produced no records"
                                if not result["timed_out"]
                                else f"timed out after {args.live_timeout:.0f}s")
            record["timings_ms"]["elapsed"] = result["elapsed_ms"]
            records.append(record)
            continue

        for scenario in scenarios:
            record = new_record(scenario.get("scenario", "unknown"), bridge,
                                scenario.get("response_mode", args.response_mode))
            status = scenario.get("status", FAIL)
            if status == PASS and result["exit_code"] != 0:
                status = FAIL
            record["status"] = status
            record["timings_ms"] = scenario.get("timings_ms", {}) or {}
            record["correlation"] = redact_structure(
                scenario.get("correlation", {}) or {}, secrets)
            record["details"] = redact_structure(
                scenario.get("checks", []) or [], secrets)
            record["notes"] = redact_structure(
                scenario.get("notes", []) or [], secrets)
            if status == FAIL:
                failed_checks = [c for c in record["details"]
                                 if c.get("status") == FAIL and c.get("required", True)]
                record["error"] = redact("; ".join(
                    f"{c.get('name')}: {c.get('detail', '')}" for c in failed_checks
                )[:800] or _tail(result["output"]), secrets)
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tail(text: str, limit: int = 1200) -> str:
    """The last *limit* characters of a captured output."""
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    return cleaned[-limit:].strip()


def collect_secrets(env: dict) -> list:
    """Every secret-like value of the environment (never printed)."""
    secrets = []
    for key, value in env.items():
        if len(str(value)) >= 8 and any(token in key.upper() for token in
                                       ("SECRET", "TOKEN", "PASSWORD", "API_KEY")):
            secrets.append(str(value))
    return secrets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", choices=["offline", "live"], required=True)
    parser.add_argument("--bridge", choices=["all"] + BRIDGES, default="all")
    parser.add_argument("--response-mode", choices=RESPONSE_MODES, default="blocking")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--offline-timeout", type=float, default=900.0)
    parser.add_argument("--live-timeout", type=float, default=900.0)
    parser.add_argument("--submit-timeout", type=float, default=120.0)
    parser.add_argument("--observe-timeout", type=float, default=180.0)
    parser.add_argument("--cancel-timeout", type=float, default=120.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()

    env = load_env()
    secrets = collect_secrets(env)
    pythonpath = child_pythonpath(env)

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    if args.suite == "offline":
        records = run_offline(args, secrets, pythonpath)
    else:
        records = run_live(args, secrets)

    counts = summarize(records)
    failed = counts["failed"]
    report = {
        "suite": args.suite,
        "bridge_selection": args.bridge,
        "response_mode": args.response_mode,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_s": round(time.time() - started, 3),
        "counts": counts,
        "coverage_complete": counts["unmet"] == 0 and failed == 0
        and counts["skipped"] == 0,
        "scenarios": records,
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"{args.suite}-{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nreport: {report_path}")
    print(f"required={counts['required']} passed={counts['passed']} "
          f"failed={counts['failed']} skipped={counts['skipped']} "
          f"unmet={counts['unmet']}")
    for record in records:
        print(f"  {record['status'].upper():6} {record['scenario']} "
              f"[{record['bridge']}/{record['response_mode']}]"
              + (f" - {record['error'][:160]}" if record["error"] else "")
              + (f" - {record['skip_reason']}" if record["skip_reason"] else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
