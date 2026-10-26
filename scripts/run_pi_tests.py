"""Run the existing pi validation scripts and retain sanitized evidence.

Usage: python scripts/run_pi_tests.py --suite live
       python scripts/run_pi_tests.py --suite plugin-live --plugin-root PATH
"""
from __future__ import annotations

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
from a2a_test_utils import load_env, resolve_config  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["live", "spike", "plugin-live", "chatbot"], required=True)
    parser.add_argument("--plugin-root", type=Path)
    args = parser.parse_args()
    env = load_env()
    cfg = resolve_config(env, bridge="pi")
    output_dir = ROOT / "docs" / "test_results" / "pi_review"
    output_dir.mkdir(parents=True, exist_ok=True)
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    commands = []
    if args.suite == "live":
        commands = [[sys.executable, f"tests/pi/{name}.py"] for name in (
            "test_hello", "test_hello_sse", "test_gateway_live", "test_sse_live", "test_approval_live")]
        commands.insert(4, [sys.executable, "tests/pi/test_sse_live.py", "--hide-reasoning"])
    elif args.suite == "spike":
        commands = [[sys.executable, "tests/pi/test_spike_protocol.py", "--out", str(output_dir / "spike_transcript.json")]]
    elif args.suite == "chatbot":
        commands = [[sys.executable, "tests/pi/test_chatbot.py"]]
    else:
        if args.plugin_root is None:
            parser.error("--plugin-root is required for plugin-live")
        child_env.update({
            "PYTHONPATH": os.pathsep.join([str(args.plugin_root), str(args.plugin_root.parent / "silvaengine_daemon")]),
            "A2A_RUN_LIVE_PI_TESTS": "1", "PI_LIVE_URL": cfg["pi_url"],
            "PI_LIVE_KEY": cfg["pi_key"],
            "PI_LIVE_MODEL": env.get("PI_SIDE_MODEL") or f"compat/{env.get('PI_COMPAT_MODEL') or env.get('OPENAI_COMPAT_MODEL') or 'local-model'}",
            "PI_LIVE_PG_HOST": "127.0.0.1",
            "PI_LIVE_PG_PORT": env.get("POSTGRES_PORT", "5432"),
            "PI_LIVE_PG_USER": env.get("PG_USER", "silvaengine"),
            "PI_LIVE_PG_PASSWORD": env.get("PG_PASSWORD", "silvaengine"),
            "PI_LIVE_PG_DB": env.get("PG_DB", "silvaengine"),
        })
        commands = [[sys.executable, "-m", "pytest", str(args.plugin_root / "a2a_protocol_plugin/tests/test_pi_live.py"),
                     "-v", "-s", "-o", "addopts=", "-o", "log_cli=false", "-p", "no:cacheprovider"]]
    secrets = [str(v) for k, v in env.items() if len(str(v)) >= 8 and any(x in k.upper() for x in ("SECRET", "TOKEN", "PASSWORD", "API_KEY"))]

    def sanitize(text):
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        for value in secrets:
            text = text.replace(value, "[REDACTED]")
        return text

    results = []
    for index, command in enumerate(commands, 1):
        print(f"Running {index}/{len(commands)}: {' '.join(command)}", flush=True)
        start = time.monotonic()
        try:
            completed = subprocess.run(command, cwd=ROOT, env=child_env, text=True, encoding="utf-8",
                                       errors="replace", capture_output=True, timeout=900,
                                       input="Say PI_CHATBOT_OK and nothing else.\nquit\n" if args.suite == "chatbot" else None)
            output = completed.stdout + completed.stderr
            code = completed.returncode
        except subprocess.TimeoutExpired as error:
            output = (error.stdout or b"").decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            output += "\nTIMEOUT after 900 seconds"
            code = 124
        elapsed = round(time.monotonic() - start, 3)
        output = sanitize(output)
        logfile = output_dir / f"{args.suite}_{index}.log"
        logfile.write_text(output, encoding="utf-8")
        result = {"method": "subprocess.run", "arguments": {"command": command, "cwd": str(ROOT)},
                  "status": "pass" if code == 0 else "fail", "elapsed_seconds": elapsed,
                  "output": {"exit_code": code, "log": str(logfile.relative_to(ROOT)), "tail": output[-2000:]}}
        results.append(result)
        (output_dir / f"{args.suite}_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"{result['status'].upper()} ({elapsed}s)\n{output[-1500:]}", flush=True)
    return int(any(result["status"] != "pass" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
