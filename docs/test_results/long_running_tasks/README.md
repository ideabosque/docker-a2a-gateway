# Long-running task test reports

This directory holds the machine-readable reports produced by
`scripts/run_long_running_tests.py`.

## Files

| File | Content |
|---|---|
| `README.md` | this description of the report shape |
| `<suite>-<YYYYmmdd-HHMMSS>.json` | one report per runner invocation |
| `live-<bridge>-<response-mode>.json` | the raw scenario records of a live bridge run (intermediate artifact of the runner) |

## Report shape

```jsonc
{
  "suite": "offline | live",
  "bridge_selection": "all | hermes | openclaw | goose | pi | omp | banyanos",
  "response_mode": "blocking | async | stream",
  "generated_at": "2026-11-10T14:05:00+0000",
  "duration_s": 51.2,
  "counts": {
    "required": 7,   // scenarios that had to run and had to pass
    "passed": 6,
    "failed": 0,
    "skipped": 0,    // a bridge that was NOT selected
    "unmet": 1       // a contract this build does not implement yet
  },
  "coverage_complete": false,  // true only when nothing failed, was skipped or unmet
  "scenarios": [ /* one record per scenario, see below */ ]
}
```

### Scenario record

```jsonc
{
  "scenario": "observe",
  "bridge": "omp",
  "response_mode": "blocking",
  "status": "pass | fail | skip | unmet",
  "required_failed": false,
  "checks": [
    {
      "name": "persisted output recovered via tasks/get",
      "status": "pass",
      "detail": "5 chars, digest=b24d6d33736ecd56",
      "required": true
    }
  ],
  "timings_ms": {"submit": 1860.2, "observe": 4210.5},
  "correlation": {
    "task_id": "lr-omp-4acf0dfd",      // client-supplied correlation id
    "context_id": "0828c170-2c3d-48d9-bd20-30bc66995a25",
    "terminal_state": "completed",
    "output_length": 5,
    "output_digest": "b24d6d33736ecd56",  // digest, never the answer text
    "metrics": { "submission_latency_ms": 1860.2, ... }
  },
  "notes": ["..."]
}
```

## Status semantics

* `pass` — every required check of the scenario passed.
* `fail` — a **required** check failed. The runner exits non-zero.
* `skip` — the scenario did not apply (only a bridge that was **not**
  selected may be skipped; a selected bridge that cannot connect is a
  `fail`).
* `unmet` — the contract is **not implemented in this build**. The evidence
  is attached to the check `detail` and to the scenario `notes`. An unmet
  requirement never counts as a pass, and it makes `coverage_complete`
  `false` so an incomplete run can never be read as full coverage.

Known gaps in the current build (see
`docs/LONG_RUNNING_TASKS_TEST_PLAN.md`):

* `configuration.returnImmediately` is a no-op: the request blocks and
  answers with the legacy direct-Message profile instead of a persisted,
  non-terminal Task (plan Phase 2).
* `tasks/cancel` is reported but not confirmed: the plugin's
  `executor.cancel(task_id)` does not match the SDK contract
  `cancel(context, event_queue)`, so the backend run keeps going and the
  late completion overwrites the cancelled terminal state (plan Phase 3
  gate).
* An unknown agent answers with a successful Message whose text mentions
  the failure instead of a typed JSON-RPC/A2A error.

## Secrets

Reports never contain bearer tokens, provider keys, authenticated request
bodies or answer text. Output payloads are represented by their length and
a SHA-256 digest; the captured subprocess output is redacted with
`a2a_test_utils.redact` before it is stored or printed.
