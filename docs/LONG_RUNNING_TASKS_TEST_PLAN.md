# Long-Running A2A Tasks: Docker Gateway Test Plan

Updated: 2026-11-10. Status: **harness and runner implemented** on branch
`feature/long-running-tasks` (the shared long-running harness in
`tests/a2a_test_utils.py`, its 57 offline tests, the bounded runner
`scripts/run_long_running_tests.py` and the live scenarios in
`tests/long_running/`). The measured state, the live findings and the
scenario-by-scenario coverage are recorded in
`docs/LONG_RUNNING_TASKS_CERTIFICATION.md` — a requirement that the current
build does not implement is reported as unmet, never as a pass.
Owner: docker-a2a-gateway maintainers, coordinated with a2a_protocol_plugin and silvaengine_gateway maintainers.

## Purpose and scope

Certify the user-visible long-running A2A task lifecycle through the Docker gateway:
request acceptance, task IDs, streaming or final-only output, SSE delivery, task
observation, disconnect/reconnect, failure, deadline, and cancellation. Cover every
published bridge: Hermes, OpenClaw, Goose, Pi, and BanyanOS. Include an all-external
or mixed configuration where available; optional Compose profiles do not limit which
configured bridges can be tested.

This plan covers the gateway stack and its host-side test harness. Plugin unit tests,
repository transaction tests, protocol contract decisions, and implementation phases
remain owned by the module development plan. A green live test does not certify
production durability, high availability, model quality, or every remote backend's
ability to stop execution.

## Response contract

The client selects blocking or immediate-return behavior. Duration alone does not
change the response contract: an ordinary blocking A2A request must not unexpectedly
return a nonterminal task after a test-side timer expires. Immediate-return scenarios
send `configuration.returnImmediately: true`, capture the accepted A2A Task and its
stable ID, then observe that same task.

Tests exercise both output modes independently:

| Mode | Expected client experience |
|---|---|
| Streaming agent | Task/artifact updates arrive before terminal status when the backend emits incremental content. The final response/artifact is identifiable and not appended twice. |
| Non-streaming agent | No invented intermediate tokens; the final result and terminal status arrive when execution finishes. |
| Either mode, client disconnects | Work continues after acceptance; a later connection or `tasks/get` recovers the task outcome. |

The application's local display wait is measured by the test client. It does not
alter a blocking request or set an A2A server-side fallback deadline.

## Repository starting points

Existing live scripts include:

- `tests/a2a_test_utils.py`: `.env` resolution, auth, JSON-RPC construction,
  gateway calls, and response helpers.
- `tests/<bridge>/test_gateway_live.py`: bridge health, `message/send`, `tasks/get`,
  continuation, cancellation, and failure checks.
- `tests/<bridge>/test_sse_live.py`: GET `/a2a_sse`, live artifact reception,
  terminal status, and bridge-specific streaming checks.
- `tests/<bridge>/test_hello.py` and `test_hello_sse.py`: interactive-independent
  smoke scripts; `test_chatbot.py`: interactive SSE session.
- `scripts/run_banyanos_tests.py`: BanyanOS-specific offline regressions and live
  Docker scenarios, with sanitized evidence output.
- `scripts/run_pi_tests.py`: pi bridge suites and plugin-live integration.
- `docs/BANYANOS_TEST_SOP.md`: approved local BanyanOS integration scope.

Today `message_send_params()` has no return-mode argument. BanyanOS SSE tests wait
for the HTTP result before checking the live listener. The bridge runners have
provider-specific behavior and timeout limits. Add the new scenarios without
removing existing smoke, compatibility, transcript, or live bridge coverage.

## Required test harness updates

### Shared JSON-RPC and observation helpers

Extend `tests/a2a_test_utils.py` with tested helpers to:

1. Build `message/send` params for explicit blocking and
   `configuration.returnImmediately: true` modes without mixing transport
   streaming with return mode.
2. Validate the returned envelope as a Message or A2A Task, extract the public task
   ID and context ID, and fail on malformed or missing task data.
3. Poll `tasks/get` with monotonic overall deadlines, a configurable polling interval,
   and explicit terminal-state handling. Read the authoritative persisted output
   rather than assuming the SSE connection delivered every frame.
4. Parse SSE fields (`event`, `id`, multi-line `data`), heartbeats, EOF, reconnect,
   fragmented UTF-8 and duplicate events. Do not assume one HTTP chunk equals one
   complete SSE event.
5. Report submission latency, acceptance time, first artifact time, terminal time,
   received event IDs, final output length, and backend invocation correlation without
   recording tokens or sensitive answer content.

Add bounded request connect/read/overall limits as distinct CLI settings. Always close
responses, listeners, and spawned subprocesses in cleanup paths. Keep authentication
as Authorization headers; never put bearer tokens in SSE URLs or logs.

### Runner and report

Add `scripts/run_long_running_tests.py` with explicit `--suite offline|live` and
`--bridge all|hermes|openclaw|goose|pi|banyanos` selection. Reuse each bridge's existing
`.env` and agent configuration. Do not copy provider credentials into test arguments
or evidence. Give each subprocess an overall bound, stop after offline failure, redact
secrets from output, and return nonzero if any required check fails. Save a structured
report under `docs/test_results/long_running_tasks/` with per-scenario status, bridge,
response mode, timings, task correlation IDs safe for local diagnostics, skip reason,
and sanitized error details.

A live run is opt-in and must label an absent optional service as `SKIP` only when that
bridge was not selected. If a selected bridge or required backend cannot connect, mark
it failed. Do not call a scenario certified when its dependencies were skipped.

## Suites and scenarios

### Offline and deterministic tests

Add or extend an offline suite, proposed at `tests/unit/test_long_running_harness.py`,
for response parsing, explicit return-mode construction, polling deadlines, event
parsing, duplicate suppression, result fallback, redaction, report schema, process
timeouts, and cleanup. Use fake clocks, controllable waits, and scripted HTTP/SSE
transports. Exercise malformed JSON-RPC, JSON-RPC error envelopes, GraphQL errors,
HTTP errors, connection loss, heartbeats without data, and stream-idle timeout.

The plugin's controlled-handler tests own deterministic worker crashes, queue leases,
transaction atomicity, fencing, and the exact A2A SDK executor contract. The Docker
harness should not claim to replace those tests.

### Per-bridge acceptance and result recovery

For every selected bridge:

1. Check gateway and selected backend health, authenticated Agent Card access policy,
   and required configuration. Start an authenticated `/a2a_sse` listener first.
2. Submit a unique `messageId` with `returnImmediately: true`; use a short HTTP client
   timeout appropriate to acceptance, not the total model runtime.
3. Require a successful JSON-RPC response containing an initial nonterminal Task,
   stable task ID, and conversation context when provided. Record acceptance latency.
4. For a non-streaming handler, require zero answer chunks before completion, then
   require the persisted final answer/artifact and terminal task state.
5. For a streaming handler, require at least one artifact before terminal state only
   when the configured backend supplies incremental chunks. Require ordered content,
   the correct task ID, final status, and no duplicated final answer.
6. Poll `tasks/get` until terminal or the separate overall observation deadline. Require
   that the same task ID and final output are returned even if the SSE result was missed.
7. Require a second observation of the terminal task to be stable and free of duplicate
   backend execution.
8. Repeat through existing legacy blocking and collected-response checks. Preserve their
   documented response shape; explicitly label the test profile.

Use unique tasks and brief bounded prompts. Live provider prompts are not a reliable
way to force a precise runtime duration. Test timeout boundaries and the longer-than-
client-wait case with controlled handlers offline; use live tests to validate the
Docker routes and real backend stream/result translation.

### Disconnect, reconnect, and multi-worker delivery

Test these separately from the happy path:

- Disconnect the POST client after accepted task response; prove work still finishes.
- Disconnect the SSE GET client while work runs; reconnect with its last received
  cursor and check delivery follows the implemented replay contract.
- Test the replay boundary while events are being published; duplicates are allowed
  only if clients can deduplicate them, and no terminal result may become unrecoverable.
- Reconnect after event retention expires; use `tasks/get` to recover the final result.
- Run the listener and submitting request against different gateway workers. If the
  stack uses more than one worker, configure the documented shared task, SSE, and rate
  limit backends before running this case; in-memory mode cannot pass cross-process
  acceptance.
- Simulate a stopped/slow SSE reader. Require bounded queue behavior, no impact on
  task execution, and recoverable output from persisted task state.

Record an SSE reconnect case as pending/unsupported until the deployed gateway emits
and validates event IDs plus the module implementation scopes replay to the authorized
user and tenant. Do not treat partition-wide broadcast as proof of private user delivery.

### Failure, deadline, and cancellation

- Unknown agent/backend and invalid input return a typed JSON-RPC/A2A error, not a false
  successful completed Message.
- Queue wait timeout and execution deadline are independently asserted; token activity
  must not extend a total execution deadline.
- Disconnect, worker restart, expired execution lease, and publication retry must not
  start a second backend run. Use offline fault injection for crash windows.
- User cancellation goes through `tasks/cancel`; require correct owner checks, a terminal
  A2A state, and the documented backend stop outcome.
- Distinguish cancellation requested, backend cancellation confirmed, and backend stop
  unsupported/unconfirmed. In particular, OpenClaw's local stream stop is not evidence
  that remote execution stopped.
- Race cancellation, timeout, and completion; the persisted terminal state and SSE
  completion event must agree and late output must not replace them.
- Expired credentials or backend errors produce recoverable task diagnostics without
  exposing bearer tokens, provider keys, or raw authenticated request bodies.

## CLI and report shape

The new runner should offer flags equivalent to:

```text
python scripts/run_long_running_tests.py --suite offline
python scripts/run_long_running_tests.py --suite live --bridge banyanos
python scripts/run_long_running_tests.py --suite live --bridge all --response-mode async
```

These commands describe the proposed interface and do not work until the runner is
implemented. Where useful, existing bridge scripts should accept additive options such
as `--return-mode blocking|immediate`, `--submit-timeout`, `--observe-timeout`, and
`--sse-idle-timeout`. Keep their existing defaults and behavior unchanged.

The report contains aggregate required/passed/failed/skipped counts and one record per
scenario. Include a skip reason and distinguish a green run with skipped bridges from
complete all-bridge coverage. Preserve `docs/test_results/banyanos_test_evidence.json`
and the pi review report format until any compatible report migration is agreed.

## Execution order and completion gate

1. Run offline harness tests and existing selected plugin regressions.
2. Build/start the Docker gateway, PostgreSQL, and only the selected bridge profiles;
   verify health and agent registration.
3. Run existing smoke and blocking compatibility tests for selected bridges.
4. Run immediate-return, streaming/non-streaming, `tasks/get`, and disconnect cases.
5. Run cancellation/deadline live cases only after SDK cancellation is verified.
6. Run multi-worker/reconnect cases only with the configured shared backends.
7. Run all selected bridge suites; inspect sanitized evidence and failure/skip counts.

A release candidate passes only when selected bridges return accepted tasks promptly,
results persist and can be recovered independently of SSE, streaming produces real
ordered chunks where supported, non-streaming returns one final result, task status is
consistent across A2A/SSE/task retrieval, and the test runner fails honestly on a
missing required dependency. Report provider/backend cancellation limitations and
unsupported replay explicitly. Never infer production readiness from local Docker
success alone.

## Existing documentation

- `README.md`, section **Test scripts**, is the repository test entry point.
- `docs/BANYANOS_TEST_SOP.md` keeps its approved BanyanOS local scenario scope.
- The a2a_protocol_plugin long-running-task development plan defines the module,
  shared-daemon, protocol and repository implementation gates. This Docker plan owns
  gateway-stack E2E scenarios and the cross-bridge host-side harness.