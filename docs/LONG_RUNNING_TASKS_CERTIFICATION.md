# Long-Running A2A Tasks — Certification Report (Phase 1 increment)

Updated: 2026-11-10. Branch: `feature/long-running-tasks` in
`silvaengine_gateway`, `a2a_protocol_plugin` and `docker-a2a-gateway`.

Status: **partial**. This report certifies the Phase 1 gates and the
host-side test harness only. Phases 2–5 of
`a2a_protocol_plugin/docs/LONG_RUNNING_TASKS_DEVELOPMENT_PLAN.md` (durable
dispatch, shared worker/lease/outbox services, cross-process delivery,
recipient-scoped replay, execution deadlines) are **not** implemented and are
**not** certified here. An unavailable dependency or an unimplemented
requirement is recorded as such — never as a pass.

## 1. Environment

| Item | Value | How verified |
|---|---|---|
| A2A SDK (host dev) | `a2a-sdk 1.0.2` | `importlib.metadata` |
| A2A SDK (gateway image) | `a2a-sdk 1.3.0` | `docker exec a2a-gateway /opt/venv/bin/python` |
| Tested SDK range | `1.0.0 <= a2a-sdk < 2.0.0` | pinned by `test_message_response_contract.py` |
| Cancel contract | `AgentExecutor.cancel(context: RequestContext, event_queue: EventQueue)` — identical in 1.0.2 and 1.3.0 | `a2a/server/agent_execution/active_task.py` |
| Python | 3.12.10 | host |
| Storage backend | PostgreSQL (`silvaengine_restore2`), `db_backend=postgresql` | container env |
| DynamoDB parity | **not exercised** (no DynamoDB backend available) | — |
| Deployment topology | single gateway process, in-memory SSE history | `docker compose ps` |

## 2. Topology used for the live runs

| Service | Host address | Notes |
|---|---|---|
| a2a-gateway | `http://127.0.0.1:8775` | published host port (`CONTAINER_PORT`); the container listens on 8765 (`GATEWAY_PORT`) |
| A2A endpoint / partition | endpoint `a2a`, partition `default` | `a2a#default` — where `scripts/seed_agents.py` seeds the agents |
| omp sidecar | `http://127.0.0.1:3320` (host) / `http://omp:3310` (gateway) | omp 18.18.2, `--approval-mode always-ask` |
| goose sidecar | `http://127.0.0.1:3284` | goose 1.53.0 |
| pi sidecar | `http://127.0.0.1:3310` | pi |
| hermes / openclaw | **not running** | a selected-but-unreachable bridge is a FAIL, not a SKIP |

The gateway container imports the **local** `silvaengine_gateway` and
`a2a_protocol_plugin` checkouts (`PYTHONPATH=/app/src/...`), so the branch's
source is what the live stack exercised.

## 3. What this increment implements

| Repository | Change | Plan item |
|---|---|---|
| `a2a_protocol_plugin` | `A2ADaemonExecutor.cancel` now matches the SDK's two-argument contract; the task id and partition are resolved from the trusted call context; the backend stop is attempted before the task-store update; the CANCELED status event is emitted | Phase 1 §3 |
| `a2a_protocol_plugin` | `tests/test_sdk_cancellation.py` — a **real SDK** cancellation regression (real `DefaultRequestHandler`, executor, request context, event queue, task store; only the backend network surface is mocked) | Phase 1 §3, scenario T19 |
| `a2a_protocol_plugin` | `tests/test_phase10.py` — bounded/offline execution guards (non-loopback connections rejected; a 30 s `faulthandler` watchdog) so the suite can never stall silently | Phase 1 §1 |
| `a2a_protocol_plugin` | `tests/test_message_response_contract.py` — pins the direct-Message reply profile, the blocking profile, the LEGACY collected-event profile and the partition/ownership behaviour | Phase 1 §5 |
| `silvaengine_gateway` | `_make_sse_handler` emits the SSE `id:` cursor, honours `Last-Event-ID`, degrades a malformed cursor to "no replay", and **re-scopes the replay to the caller's partition** (the shared daemon's filter lets partition-less entries through to every partition) | Phase 1 §5, scenarios T10/T11 |
| `docker-a2a-gateway` | `tests/a2a_test_utils.py` — the long-running harness: return-mode params, envelope validation, bounded `tasks/get` polling, a real SSE parser, dedupe, redaction, metrics | Docker test plan §"Required test harness updates" |
| `docker-a2a-gateway` | `tests/unit/test_long_running_harness.py` — 57 offline harness tests (fake clock, scripted transports, no network) | Docker test plan §"Offline and deterministic tests" |
| `docker-a2a-gateway` | `scripts/run_long_running_tests.py` + `tests/long_running/test_task_lifecycle.py` — the bounded live runner and its scenarios, with a machine-readable JSON report | Docker test plan §"Runner and report" |

## 4. Live findings (measured on this stack, before the fixes in §3)

| # | Finding | Evidence | Status |
|---|---|---|---|
| F1 | **Cancellation was reported but not confirmed.** `tasks/cancel` returned `TASK_STATE_CANCELED`, yet the backend run completed: the original `message/send` returned "`sleep 20` completed … wall time 20.11s". Two causes: the SDK calls `agent_executor.cancel(context, event_queue)` while the plugin implemented `cancel(task_id)` (the SDK swallowed the `TypeError`, so the backend stop never ran); and a cancellation that arrived while the backend was still starting found no registered run, so the stop request was lost. | live probe | **fixed** in this branch: the SDK contract, the tenant-correct task-store context, and a remembered cancellation that stops the run as soon as it registers. Live: the bridge now answers "Request canceled." and the omp/goose cancel scenarios pass |
| F2 | **A late completion resurrected a cancelled task.** After the cancel, `tasks/get` showed `TASK_STATE_COMPLETED` with the tool output. With F1 fixed the run is actually stopped, so the late completion no longer arrives (the live cancel scenario's "cancelled terminal state not overwritten" check passes). The general case is still unguarded: a backend that completes BEFORE the cancel is processed still overwrites the terminal decision. | live probe | **symptom gone** (the run is stopped); the terminal-revision guard itself is **open** — Phase 3 |
| F3 | **`configuration.returnImmediately` is a no-op.** The request blocked 13.00 s and returned a direct Message (`ROLE_AGENT`, `parts`, `contextId`) — no Task, no task id. The flag only applies when the reply is a Task; the plugin always takes the legacy direct-Message profile. | live probe | **open** — Phase 2 item 4 |
| F4 | **SSE frames carried no `id:` field**, so a browser `EventSource` could not resume automatically; the cursor existed only inside the JSON payload. | live probe | **fixed** in this branch |
| F5 | **Replay was partition-scoped, not recipient-scoped.** The shared daemon's history entries without a `partition_key` are returned to *every* partition (`silvaengine_daemon/sse.py`: `msg.get("partition_key") in (None, partition_key)`). The A2A plugin's SSE path does pass the partition (`a2a_protocol_plugin/main.py:551` → `send_to_user(..., partition_key=partition_key)`), so the window is narrow — but the daemon's `send_to_client` / `broadcast_message` paths, and any call with an empty partition, still append partition-less entries that any partition can replay. | source + live probe | **mitigated** in the gateway (partition-scoped replay); **recipient-level** isolation remains **open** — it belongs to `silvaengine_daemon` |
| F6 | **An unknown agent answered with a successful Message** whose text said "AI agent error: Agent not found: …" - a caller decoding the A2A lifecycle saw a false success. The structurally-failed Task shape existed for the `message_response` path but not for the two `task_execution` paths. | live scenario | **fixed** in this branch: every bridge failure on the task-execution paths now emits a FAILED Task |

### Verified-working behaviour (no regression)

| Property | Evidence |
|---|---|
| Partition isolation of live SSE delivery | a client on `Part-Id: other` received **0** `task_artifact`/`task_status` frames from a task submitted on `Part-Id: default` |
| Work continues after the client disconnects | submitted over a raw socket, closed after 3 s → the run completed; `tasks/get` recovered `TASK_STATE_COMPLETED` and the output |
| Durable output + `tasks/get` recovery | the persisted task carries the final message parts |
| Approval (HITL) interruption | `task_permission` (`Allow tool: bash / Command: echo approval-probe`) + `INPUT_REQUIRED` → `approval_response` → `Approval granted for task …` → the run continued to completion |
| Long-running task suite (live, per bridge) | omp / goose / pi -> **8 passed, 0 failed, 0 unmet** each: health, agent-card policy, submission, terminal observation via `tasks/get`, a stable second observation, disconnect/reconnect with recovery, the unknown-agent typed error, and a confirmed cancellation |
| Banyanos bridge (long-running lifecycle) | the task never reaches a terminal state within 180 s, the completed task carries no output, and `tasks/cancel` is refused with -32002. Verified against the pre-change plugin as well, where the bridge fails even harder (7 scenarios) - these gaps are pre-existing, and this branch's fixes improved it (5 of 8 scenarios now pass). | owner: the plugin's banyanos agent path |
| Existing bridge suites | omp **10/10**, goose **10/10**, pi **10/10** (`tests/<bridge>/test_gateway_live.py`) |

## 5. Scenario coverage

Status legend: **COVERED** — implemented and evidenced here · **PARTIAL** — part
of the scenario is evidenced, the rest is unimplemented · **OPEN** — not
implemented in this increment · **N/A** — belongs to a phase that does not
exist yet.

| ID | Scenario | Status | Evidence / gap |
|---|---|---|---|
| T01 | Legacy fast blocking request | COVERED | direct-Message profile pinned (`test_message_response_contract.py`); bridge suites 10/10 |
| T02 | Blocking task exceeds display budget | PARTIAL | no premature `WORKING` is pinned; the 15 s display budget is frontend-only |
| T03 | Explicit immediate-return request | **OPEN** | F3 — not implemented in this build |
| T04 | Fast task through the new lifecycle | OPEN | needs T03 |
| T05 | Slow non-streaming backend | PARTIAL | acceptance precedes completion and the final output persists (`sleep 10`/`sleep 20` runs) |
| T06 | Slow streaming backend | COVERED | ordered `task_artifact` frames then `task_status` |
| T07 | Reasoning/files/structured output | PARTIAL | reasoning visibility pinned in the plugin handler suites; not re-run here |
| T08 | Request loop teardown | COVERED | raw-socket disconnect → work continued |
| T09 | Client disconnect and late connection | COVERED | `tasks/get` recovered the final state |
| T10 | SSE reconnect / cursor boundary | COVERED | `id:` framing + `Last-Event-ID`; a manual cursor replayed 31 missed frames |
| T11 | Expired cursor or history | PARTIAL | a malformed cursor degrades to "no replay"; snapshot recovery via `tasks/get` |
| T12 | Different tenant / different actor | PARTIAL | partition isolation measured; **recipient-level** isolation open (F5) |
| T13 | Spoofed metadata | PARTIAL | client metadata cannot override the trusted partition (contract test) |
| T14 | Duplicate concurrent submission | OPEN | needs the durable reservation (Phase 2) |
| T15 | Queue submission failure / crash | OPEN | no durable dispatch |
| T16 | Crash around backend acceptance | OPEN | no idempotency/ambiguity handling |
| T17 | Completion commit then publisher crash | OPEN | no outbox |
| T18 | Worker lease expires | OPEN | no leases |
| T19 | User cancel before and during execution | COVERED | the real SDK cancellation regression plus the live scenario: the backend is asked to stop, the bridge answers "Request canceled.", the persisted state stays cancelled (omp and goose) |
| T20 | Unsupported / rejected remote stop | PARTIAL | the live scenario reports the backend-stop outcome separately (requested / confirmed / not confirmed); a backend that cannot be stopped is recorded as "requested, not confirmed" and still cancels the task |
| T21 | Deadline during continuous output | OPEN | no execution deadline (Phase 3) |
| T22 | Race: cancel, timeout and completion | PARTIAL | the cancellation now stops the run, so no late completion arrives (live: the cancelled state persists). A backend that completes before the cancel is processed still overwrites the terminal decision — the terminal-revision guard is open (Phase 3) |
| T23 | Queue backlog / exhausted worker pool | OPEN | no queue |
| T24 | Approval / auth interruption | COVERED | measured end-to-end through the gateway |
| T25 | Cross-process gateway / worker | OPEN | single worker, in-memory SSE history |
| T26 | Slow SSE consumer | OPEN | bounded queues are the daemon's concern |
| T27 | Banyanos queued continuation | OPEN | Banyanos poller integration |
| T28 | Backend parity and session reuse | PARTIAL | PostgreSQL only; DynamoDB parity not run |
| T29 | Shutdown and rollout disable | OPEN | Phase 5 |
| T30 | Native streaming vs collected compatibility | PARTIAL | the legacy collected profile is pinned; native streaming = the SSE route with cursor framing |
| T31 | Browser authentication | OPEN | `EventSource` cannot send an Authorization header; no cookie auth |
| T32 | Worker credential expiry | OPEN | credential lifetime is Phase 2 |

**Not certified:** T03/T04, T14–T18, T21–T23, T25–T29, T31–T32, and the
recipient-level half of T12.

## 6. Reproducing this certification

```bash
# plugin — unit (the plugin is not pip-installed in the host interpreter)
cd a2a_protocol_plugin
export PYTHONPATH="C:/Users/bibo7/gitrepo/silvaengine/a2a_protocol_plugin;C:/Users/bibo7/gitrepo/silvaengine/silvaengine_daemon;C:/Users/bibo7/gitrepo/silvaengine/silvaengine_gateway"
python -m pytest a2a_protocol_plugin/tests/test_sdk_cancellation.py \
                 a2a_protocol_plugin/tests/test_executor_unit.py \
                 a2a_protocol_plugin/tests/test_phase10.py \
                 a2a_protocol_plugin/tests/test_message_response_contract.py \
                 a2a_protocol_plugin/tests/test_omp_handler.py \
                 a2a_protocol_plugin/tests/test_omp_executor.py \
                 a2a_protocol_plugin/tests/test_pi_handler.py \
                 a2a_protocol_plugin/tests/test_goose_handler.py -q -o addopts=""

# gateway — unit
cd silvaengine_gateway
python -m pytest silvaengine_gateway/tests -q -o addopts=""

# docker — offline harness
cd docker-a2a-gateway
PYTHONPATH="C:/Users/bibo7/gitrepo/silvaengine/a2a_protocol_plugin" \
  python -m pytest tests/unit -q -o addopts=""

# docker — live gateway-stack suite (needs the stack up)
python scripts/run_long_running_tests.py --suite offline
python scripts/run_long_running_tests.py --suite live --bridge omp
```

## 7. Known pre-existing failures (not caused by this work)

| Suite | Failure | Why |
| `a2a_protocol_plugin/tests/test_jwt_validation.py` | 11 failures | pre-existing on pristine `origin/main` (verified in a clean worktree) |
| `a2a_protocol_plugin/tests/test_postgresql_backend.py` | 13 errors | needs a PostgreSQL on `localhost:5432` |
| `docker-a2a-gateway/tests/unit/test_banyanos_{reasoning,client_regressions}.py` | collection error | `a2a_protocol_plugin` is only installed inside the image; use the `PYTHONPATH` above |
| `docker-a2a-gateway/tests/{goose,hermes,openclaw,pi}/*` | 19 ruff findings | pre-existing |

## 8. Open work and owners

| Item | Owner |
|---|---|
| Durable acceptance, dispatch, leases, outbox (Phases 2–3) | `a2a_protocol_plugin` + `silvaengine_daemon` |
| Recipient-scoped SSE history and replay (F5) | `silvaengine_daemon` (`silvaengine_daemon/sse.py`) |
| Terminal-revision fencing: a completion that arrives after a cancel must not overwrite the terminal decision (F2's general case) | `a2a_protocol_plugin` (Phase 3) |
| Explicit immediate-return mode (F3) | `a2a_protocol_plugin` (Phase 2 item 4) |
| Execution deadlines, cancellation budgets (T21/T22) | `a2a_protocol_plugin` (Phase 3) |
| Cross-process delivery, shared SSE backends (T25) | `silvaengine_daemon` + deployment |
| Browser `EventSource` authentication (T31) | `silvaengine_gateway` |
