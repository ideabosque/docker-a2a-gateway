# pi Sidecar (docker-a2a-gateway `pi` sibling)

HTTP + SSE service wrapping [pi](https://github.com/earendil-works/pi) — the
minimal, self-extensible coding agent — in
[RPC mode](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md).
pi has **no network server**; this sidecar is the boundary so the A2A gateway
(`a2a_protocol_plugin/handlers/pi_handler.py`) can bridge tasks to a pi agent
with the same shape as the bundled `goose serve` sibling.

```
A2A client ──▶ gateway ──▶ a2a_protocol_plugin (PiAgentHandler / PiSidecarClient)
                                          │ HTTP + SSE
                                          ▼
                        pi sidecar (this dir, Node) ── stdin/stdout JSONL ──▶ pi --mode rpc
                        (one process per active session)
```

## Endpoints (all require `X-Secret-Key`)

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness + pi version. |
| `POST /sessions` | Start a session → `{sessionId, sessionFile}`. Body: `cwd?`, `model?` (`provider/modelId`), `thinking?`. |
| `GET /sessions/{id}` | `get_state` + `get_session_stats`; also the resume probe. |
| `DELETE /sessions/{id}` | Close the live process (the session file stays on disk). |
| `POST /sessions/{id}/prompt` | Body `{message, images?, streamingBehavior?}` → SSE stream of pi events, terminated by a `run_end` frame. While a run is streaming a second prompt is rejected **409** *unless* it carries `streamingBehavior` (`steer`/`followUp`) — then it returns a plain JSON `{disposition}` acknowledgement and the reply streams on the first caller's window. |
| `POST /sessions/{id}/abort` | Abort the running prompt (runs end `aborted: true`). |
| `POST /sessions/{id}/ui-response` | Forward an `extension_ui_response` (`id` + `value`/`confirmed`/`cancelled`). |

## Framing notes

The driver (`src/rpc.ts`) implements pi's [RPC protocol](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
directly instead of importing pi's `RpcClient`: in pi 1.4.0 `RpcClient`'s stdin
writer is private and it has **no extension-UI response API**, so dialogs
(`extension_ui_request` → `extension_ui_response`) would deadlock. The wire
format is identical; the driver:

- splits stdout records on **LF only** (never `readline`, which also splits on
  U+2028/U+2029 — valid inside JSON strings), tolerating CRLF;
- correlates `response` records by command id (they may arrive out of order);
- forwards session events verbatim to the SSE stream;
- routes `extension_ui_request` dialogs to the HTTP layer (`/ui-response`).

## Process model and guards

- **One pi process per active session** — `PI_MAX_PROCESSES` caps the pool
  (full pool answers 503); idle processes are closed after
  `PI_IDLE_TIMEOUT_S` (stdin close = orderly pi shutdown).
- **Session files survive process and container restarts while the volume is retained.** `--session-dir /data/sessions` keeps
  pi's JSONL session trees on the `pi-data` volume; `sessions-index.json`
  maps session id → file so `GET /sessions/{id}` transparently re-opens a
  session after a sidecar restart (`switch_session`).
- **Client disconnects keep runs going** — cancellation is explicit
  (`POST …/abort`); the gateway's `tasks/cancel` maps to it.
- The A2A handler closes the process after its own turn finishes. A queued
  `steer`/`followUp` request closes only its HTTP client; the original caller
  retains the session until that run finishes. Concurrent resumes share one
  startup, and the process cap includes sessions still starting.

## Tool approval (bundled extension)

`extensions/tool-approval.js` is installed into `<agent-dir>/extensions` by
the container entrypoint (no clobber — an operator file wins). pi's built-in
tools run WITHOUT prompts; this extension intercepts the tools listed in
`PI_SIDE_APPROVE_TOOLS` (e.g. `bash,edit,write`, or `*`) and opens a
`ctx.ui.confirm` dialog — in RPC mode a **blocking** `extension_ui_request` —
which the A2A bridge answers per the agent's `pi_permission_mode`
(`auto_allow` confirms, `deny` cancels, `ask` parks the task
`INPUT_REQUIRED` until the user's `approval_response` arrives). The optional
`PI_SIDE_APPROVE_TIMEOUT_MS` fail-closes an unanswered dialog. Unit tests:
`test/tool-approval.test.ts`; live E2E: `tests/pi/test_approval_live.py` in
docker-a2a-gateway.

## Testing

`npm test` (vitest) drives the framing/pool lifecycle against
`test/fake-pi.mjs`, a scripted `pi --mode rpc` emulator: LF-only split with a
record split across stdout chunk boundaries, U+2028 inside a delta, response
correlation, failed-command errors, process-exit rejection, UI-dialog
forwarding, session index resume, process cap, and idle reaping. Re-capture
behaviour against a real pi in the docker stack when the pinned
`@earendil-works/pi-coding-agent` version changes.

Run the full live validation from the repository root after starting the local
gateway, PostgreSQL, and pi services and configuring `.env`:

```bash
python scripts/run_pi_tests.py --suite spike
python scripts/run_pi_tests.py --suite live
python scripts/run_pi_tests.py --suite plugin-live --plugin-root ../silvaengine/a2a_protocol_plugin
python scripts/run_pi_tests.py --suite chatbot
```

The live suite covers smoke requests, gateway protocol operations, cancellation,
SSE and HTTP replies, reasoning visibility, and approval/denial. The protocol
spike answers its own tool dialogs; the approval suite tests user decisions
through the gateway. Plugin live tests use the local PostgreSQL service for
conversation mappings. Logs and command results are saved under
`docs/test_results/pi_review/`; credentials are read at runtime.
