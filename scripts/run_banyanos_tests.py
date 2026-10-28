#!/usr/bin/env python3
"""Run offline BanyanOS regressions and optional local Docker scenarios."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    framework = ROOT.parent / 'silvaengine'
    parser.add_argument('--plugin-dir', type=Path,
                        default=framework / 'a2a_protocol_plugin')
    parser.add_argument('--daemon-dir', type=Path,
                        default=framework / 'silvaengine_daemon')
    parser.add_argument('--live', action='store_true',
                        help='invoke configured local agents and model provider')
    args = parser.parse_args()
    plugin_tests = args.plugin_dir.resolve() / 'a2a_protocol_plugin/tests'
    if not plugin_tests.is_dir():
        parser.error(f'Plugin tests not found: {plugin_tests}')
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join([
        str(args.plugin_dir.resolve()), str(args.daemon_dir.resolve()),
        env.get('PYTHONPATH', ''),
    ])
    env['PYTHONIOENCODING'] = 'utf-8'
    commands = [[
        sys.executable, '-m', 'pytest', 'tests/unit', 'tests/test_chatbot_stream.py',
        *(str(plugin_tests / name) for name in (
            'test_banyanos_client.py', 'test_banyanos_agent_handler.py',
            'test_banyanos_identity.py')),
        '-q', '-o', 'addopts=', '-o', 'log_cli=false', '-p', 'no:cacheprovider',
    ]]
    if args.live:
        commands.extend([
            [sys.executable, 'tests/banyanos/test_gateway_live.py'],
            [sys.executable, 'tests/banyanos/test_sse_live.py', '--timeout', '60',
             '--prompt', 'Count from one to five.'],
            [sys.executable, 'tests/banyanos/test_hello.py'],
            [sys.executable, 'tests/banyanos/test_hello_sse.py', '--timeout', '60'],
            [sys.executable, 'tests/banyanos/test_chatbot.py', '--hide-reasoning'],
        ])
    marker = 'BANYANOS-E2E-' + uuid.uuid4().hex[:10]
    chat_input = (f'Remember this exact marker: {marker}. Reply with only the marker.\n'
                  'What exact marker did I ask you to remember? Reply with only the marker.\n'
                  'quit\n')
    results = []
    failed = False
    for command in commands:
        started = time.monotonic()
        is_chat = 'tests/banyanos/test_chatbot.py' in command
        stdin_text = chat_input if is_chat else None
        try:
            process = subprocess.run(command, cwd=ROOT, env=env,
                                     capture_output=True, text=True,
                                     input=stdin_text,
                                     encoding='utf-8', errors='replace', timeout=1200)
            output = {'exit_code': process.returncode, 'stdout': process.stdout,
                      'stderr': process.stderr}
        except subprocess.TimeoutExpired:
            output = {'exit_code': 124, 'stdout': '',
                      'stderr': 'Scenario process exceeded 1200-second deadline'}
        output['stdout'] = re.sub(r'\x1b\[[0-9;]*m', '', output['stdout'])
        if is_chat:
            # The interactive CLI can exit zero after a failed startup or turn.
            # Require two replies and actual recall, not just successful exit.
            reply_counts = re.findall(r'\((\d+) chars\)', output['stdout'])
            chat_ok = (len(reply_counts) == 2
                       and all(int(size) > 0 for size in reply_counts)
                       and output['stdout'].count(marker) >= 2
                       and 'Bye!' in output['stdout']
                       and not any(error in output['stdout'] for error in (
                           'Request failed:', 'No response received',
                           'Cannot establish SSE', 'Error:', '[failed]')))
            output['chat_validation'] = {'two_replies_and_marker_recall': chat_ok}
            if not chat_ok and output['exit_code'] == 0:
                output['exit_code'] = 1
        result = {'method': 'subprocess.run', 'arguments': command,
                  'cwd': str(ROOT), 'status': 'pass' if output['exit_code'] == 0 else 'fail',
                  'elapsed_seconds': round(time.monotonic() - started, 3),
                  'output': output}
        if stdin_text is not None:
            result['stdin'] = stdin_text
        results.append(result)
        print(output['stdout'], end='', flush=True)
        if output['stderr']:
            print(output['stderr'], file=sys.stderr, end='', flush=True)
        if output['exit_code']:
            failed = True
            if '-m' in command:
                break  # live checks require offline success
    report = ROOT / 'docs/test_results/banyanos_test_evidence.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({'live': args.live, 'results': results}, indent=2),
                      encoding='utf-8')
    print(f'Evidence: {report}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
