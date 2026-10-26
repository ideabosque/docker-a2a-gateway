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
- **Session files survive everything.** `--session-dir /data/sessions` keeps
  pi's JSONL session trees on the `pi-data` volume; `sessions-index.json`
  maps session id → file so `GET /sessions/{id}` transparently re-opens a
  session after a sidecar restart (`switch_session`).
- **Client disconnects keep runs going** — cancellation is explicit
  (`POST …/abort`); the gateway's `tasks/cancel` maps to it.

## Testing

`npm test` (vitest) drives the framing/pool lifecycle against
`test/fake-pi.mjs`, a scripted `pi --mode rpc` emulator: LF-only split with a
record split across stdout chunk boundaries, U+2028 inside a delta, response
correlation, failed-command errors, process-exit rejection, UI-dialog
forwarding, session index resume, process cap, and idle reaping. Re-capture
behaviour against a real pi in the docker stack when the pinned
`@earendil-works/pi-coding-agent` version changes.