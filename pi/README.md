# Agent Sidecar (docker-a2a-gateway `pi` / `omp` siblings)

HTTP + SSE service wrapping a coding agent in its **RPC mode**:

- **pi** — [pi](https://github.com/earendil-works/pi), the minimal,
  self-extensible coding agent, in
  [RPC mode](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md);
- **omp** — [oh-my-pi](https://github.com/can1357/oh-my-pi), in
  [RPC mode](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md).

Neither agent has a **network server**; this sidecar is the boundary so the
A2A gateway (`a2a_protocol_plugin/handlers/pi_handler.py` /
`omp_handler.py`) can bridge tasks to an agent with the same shape as the
bundled `goose serve` sibling.

```
A2A client ──▶ gateway ──▶ a2a_protocol_plugin (PiAgentHandler / OmpAgentHandler)
                                          │ HTTP + SSE
                                          ▼
                       agent sidecar (this dir, Node) ── stdin/stdout JSONL ──▶ agent --mode rpc
                       (one process per active session)
```

## Dialects

One implementation, two dialects, selected with `AGENT_DIALECT`
(`pi` — the default — or `omp`; an unknown value fails at startup). It is both
a **runtime** setting (the env var the sidecar reads) and a **Docker build
arg** (`ARG AGENT_DIALECT=pi` in `Dockerfile`) that decides which agent the
image carries — `AGENT_DIALECT=pi` installs the pinned pi CLI from npm,
`AGENT_DIALECT=omp` installs the pinned standalone omp release binary. The
`omp` compose service builds the image with `AGENT_DIALECT=omp`; the `pi`
service with the default. The dialect decides the env namespace (`PI_*` /
`OMP_*`), the binary, the port, and the protocol details that differ between
the two agents:

| | pi | omp |
| --- | --- | --- |
| env namespace | `PI_*` | `OMP_*` |
| default port / binary | 3300 / `pi` | 3310 / `omp` |
| startup | answers commands when up | emits a `ready` frame first (the sidecar waits for it) |
| protocol | v1 | v1 + v2 negotiation (`negotiate_protocol`), `rpc_chunk` reassembly |
| run end | `agent_settled` event | `prompt_result` frame (echoes the prompt's command id) |
| prompt that started no run | `data.disposition === "handled"` | `data.agentInvoked === false` |
| model flag | `--provider` + `--model` | `--model` (whole spec, roles included) |
| tool approval | bundled `tool-approval.js` extension | native (`--approval-mode always-ask`) |

The HTTP surface and the `run_end` frame are identical for both dialects.

### The omp dialect in detail

- **Ready handshake.** omp emits `{"type":"ready","protocolVersion":1,
  "supportedProtocolVersions":[1,2],"maxFrameBytes":1048576,
  "maxReassembledFrameBytes":67108864,"supportsBashExcludeFromContext":true}`
  before it accepts commands. The sidecar waits for that frame (30 s timeout)
  and sends no command before it — and it reads `maxReassembledFrameBytes`
  from it.
- **Protocol v2.** Right after `ready` the sidecar sends
  `{"id":"protocol-2","type":"negotiate_protocol","protocolVersion":2}` and
  `{"type":"set_subagent_subscription","level":"off"}`. A refused negotiation
  is logged and the session continues on v1.
- **Chunk reassembly.** Protocol v2 splits an oversized frame into base64
  `rpc_chunk` records (`chunkId`, `index`, `count`, `byteLength`, `data`).
  The driver buffers them, requires contiguous indices and a matching count,
  concatenates the segments in index order and parses one JSON object. A gap,
  an interrupted sequence or a frame above the advertised limit discards the
  sequence and emits an `rpc_frame_error` event.
- **Completion.** A run ends with the `prompt_result` frame whose `id` equals
  the prompt's command id: `status: "completed"` → `stopReason: "end_turn"`,
  `"aborted"` → `aborted: true` / `stopReason: "aborted"`, `"error"` →
  `stopReason: "error"` with `error` = `prompt_result.error.message`. While
  `sessionSettled` is not true the sidecar polls `get_state.isSettled`.
- **"Handled" prompts.** A built-in slash command answers the prompt
  response with `data.agentInvoked: false` and no `prompt_result` follows —
  the sidecar maps that to `stopReason: "handled"` (pi's analogue is
  `disposition: "handled"`).
- **Native approval.** A gated tool call arrives as an
  `extension_ui_request` with `method: "select"` and the options
  `Approve` / `Approve <tool> for this session` / `Deny`. The sidecar forwards
  it to `/ui-response`; the bridge answers it per the agent's
  `omp_permission_mode`.
- **Env namespace.** Every sidecar setting is read as `${P}_<NAME>` with
  `P = PI` or `P = OMP`: `OMP_PORT`, `OMP_DATA_DIR`, `OMP_SESSION_DIR`,
  `OMP_CWD`, `OMP_SIDECAR_SECRET_KEY`, `OMP_MAX_PROCESSES`,
  `OMP_IDLE_TIMEOUT_S`, `OMP_STREAM_TIMEOUT_S`, `OMP_MAX_BODY_BYTES`,
  `OMP_SIDE_MODEL`, `OMP_SIDE_THINKING`, `OMP_SIDE_TOOLS`,
  `OMP_SIDE_EXTRA_ARGS`, `OMP_LOG_EVENTS`, plus the omp-only
  `OMP_SIDE_APPROVAL_MODE` (default `always-ask`), `OMP_SIDE_NO_LSP`
  (default `1` → `--no-lsp`) and `OMP_SIDE_MAX_COST` (optional →
  `--max-cost <usd>`). The binary is `AGENT_BIN` → `OMP_BIN` → `omp`.

### The agent binaries

The pi variant installs the pinned npm distribution
`@earendil-works/pi-coding-agent@${PI_VERSION}`. The omp variant installs
the **standalone release binary** instead of the npm package (which needs
Bun): the image downloads `omp-linux-x64` (or `omp-linux-arm64`) from the
`v${OMP_VERSION}` GitHub release, verifies its **SHA-256 against that
release's `SHA256SUMS.txt`** — a mismatch fails the build — and installs it as
`/usr/local/bin/omp`. Neither variant installs Bun.

## Endpoints (all require `X-Secret-Key`)

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness + agent version (`{"status":"ok","dialect":"<name>","<name>":"<version>","sessions":{"active":N}}`). |
| `POST /sessions` | Start a session → `{sessionId, sessionFile}`. Body: `cwd?`, `model?` (`provider/modelId`), `thinking?`. |
| `GET /sessions/{id}` | `get_state` + `get_session_stats`; also the resume probe. |
| `DELETE /sessions/{id}` | Close the live process (the session file stays on disk). |
| `POST /sessions/{id}/prompt` | Body `{message, images?, streamingBehavior?}` → SSE stream of agent events, terminated by a `run_end` frame. While a run is streaming a second prompt is rejected **409** *unless* it carries `streamingBehavior` (`steer`/`followUp`) — then it returns a plain JSON `{disposition}` acknowledgement and the reply streams on the first caller's window. |
| `POST /sessions/{id}/abort` | Abort the running prompt (runs end `aborted: true`). |
| `POST /sessions/{id}/ui-response` | Forward an `extension_ui_response` (`id` + `value`/`confirmed`/`cancelled`). |

## Framing notes

The driver (`src/rpc.ts`) implements the agent's RPC protocol directly
instead of importing the vendor `RpcClient`: pi 1.4.0's `RpcClient` stdin
writer is private and it has **no extension-UI response API**, so dialogs
(`extension_ui_request` → `extension_ui_response`) would deadlock. The wire
format is identical; the driver:

- splits stdout records on **LF only** (never `readline`, which also splits on
  U+2028/U+2029 — valid inside JSON strings), tolerating CRLF;
- correlates `response` records by command id (they may arrive out of order);
- forwards session events verbatim to the SSE stream;
- routes `extension_ui_request` dialogs to the HTTP layer (`/ui-response`);
- (omp) waits for the `ready` frame before sending any command, negotiates
  protocol **v2** right after it, and reassembles `rpc_chunk` sequences
  (protocol v2 splits oversized frames; the reassembly honours the limit the
  `ready` frame advertises).

## Process model and guards

- **One agent process per active session** — `PI_MAX_PROCESSES` /
  `OMP_MAX_PROCESSES` caps the pool (full pool answers 503); idle processes
  are closed after `PI_IDLE_TIMEOUT_S` / `OMP_IDLE_TIMEOUT_S` (stdin close =
  orderly agent shutdown).
- **Session files survive process and container restarts while the volume is retained.** `--session-dir /data/sessions` keeps
  the agent's JSONL session trees on the data volume; `sessions-index.json`
  maps session id → file so `GET /sessions/{id}` transparently re-opens a
  session after a sidecar restart (`switch_session`). A session that moves to
  another file (a lease fork) updates its index entry.
- **Client disconnects keep runs going** — cancellation is explicit
  (`POST …/abort`); the gateway's `tasks/cancel` maps to it.
- The A2A handler closes the process after its own turn finishes. A queued
  `steer`/`followUp` request closes only its HTTP client; the original caller
  retains the session until that run finishes. Concurrent resumes share one
  startup, and the process cap includes sessions still starting.

## Tool approval

**pi** — `extensions/tool-approval.js` is installed into `<agent-dir>/extensions` by
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

**omp** — the extension is NOT installed: omp gates its tools natively
(`OMP_SIDE_APPROVAL_MODE`, default `always-ask`) and asks through the same
`extension_ui_request` channel — a `select` whose `options` carry the
labels `Approve` / `Approve <tool> for this session` / `Deny` (the details
ride the frame's `title`). The bridge answers it per the agent's
`omp_permission_mode`: it matches the allow / deny option by the labels in
`omp_approval_allow_labels` / `omp_approval_deny_labels` or by the
conventional words, and **cancels** a dialog whose options cannot be
classified (fail closed — the bridge never approves blindly).
`OMP_SIDE_APPROVAL_MODE=yolo` disables the prompts entirely — then the
permission mode has no effect.

## Testing

`npm test` (vitest) drives the framing/pool lifecycle against
`test/fake-pi.mjs`, a scripted `pi --mode rpc` emulator: LF-only split with a
record split across stdout chunk boundaries, U+2028 inside a delta, response
correlation, failed-command errors, process-exit rejection, UI-dialog
forwarding, session index resume, process cap, and idle reaping.
`test/dialect.test.ts` does the same for the omp dialect against
`test/fake-omp.mjs`: the `ready` handshake, the protocol v2 negotiation, the
`rpc_chunk` reassembly (and the rejection of an oversized sequence), the
`prompt_result` → `run_end` mapping (completed / error / aborted), a prompt
that started no run, and the session-file fork detection. Re-capture
behaviour against the real agents in the docker stack when the pinned
`@earendil-works/pi-coding-agent` / omp versions change.

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
