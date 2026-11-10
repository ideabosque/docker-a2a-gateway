#!/usr/bin/env node
/**
 * fake-omp.mjs — a scripted `omp --mode rpc` emulator for the sidecar unit
 * tests (vitest). Speaks the omp (oh-my-pi) RPC JSONL protocol (strict LF
 * framing) per the upstream RPC docs. NOT used at runtime — the real sidecar
 * spawns the real omp.
 *
 * Scripted behaviour via environment variables:
 *   FAKE_OMP_LOG=<file>          append a JSON log of received commands
 *   FAKE_OMP_THINKING=1          emit a thinking_delta before the answer
 *   FAKE_OMP_TOOL=1              emit tool_execution_start/update/end
 *   FAKE_OMP_UI=1                emit an approval extension_ui_request
 *                                (method select) and wait for the answer
 *   FAKE_OMP_ERROR=1             the prompt ends with status "error"
 *   FAKE_OMP_ABORT=1             the prompt ends with status "aborted"
 *   FAKE_OMP_CHUNK=1             the answer rides in rpc_chunk frames
 *   FAKE_OMP_CHUNK_BYTES=N       size of the chunked frame (default 300000)
 *   FAKE_OMP_MAX_REASSEMBLED=N   advertise a smaller reassembly limit
 *   FAKE_OMP_DELAY_PROMPT_MS=N   delay the prompt response
 *   FAKE_OMP_UNSETTLED=1         the prompt_result reports sessionSettled:false
 *   FAKE_OMP_FORK=1              switch_session moves to a new session file
 *   FAKE_OMP_QUEUED_TURN=1       a follow-up queued during the run starts its
 *                                own turn after the prompt_result (the session
 *                                stays unsettled until that turn finished)
 *
 * Frames per prompt: agent_start, turn_start, message_update deltas
 * (thinking?), optional tool execution events, message_end, turn_end,
 * agent_end, then the `prompt_result` frame and `session_settled`.
 */

import { appendFileSync } from "node:fs";
import { StringDecoder } from "node:string_decoder";

// Synchronous file logging: async write streams race process.exit on
// Windows child pipes (writes were lost at shutdown).
const LOG_PATH = process.env.FAKE_OMP_LOG || null;
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const decoder = new StringDecoder("utf-8");
let buffer = "";

function send(record) {
	// One JSON object per record, terminated by LF only.
	process.stdout.write(JSON.stringify(record) + "\n");
}

function logCmd(cmd) {
	if (LOG_PATH) {
		try {
			appendFileSync(LOG_PATH, JSON.stringify(cmd) + "\n");
		} catch {
			/* best effort */
		}
	}
}

// Unique per process: two sessions must never collide in the pool's map.
const SESSION_UID = `${process.pid.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
const FRESH_ID = `fake-omp-session-${SESSION_UID}`;

const state = {
	sessionId: FRESH_ID,
	sessionFile: `/data/sessions/1700000000000_${FRESH_ID}.jsonl`,
	isStreaming: false,
	isSettled: true,
	queuedMessageCount: 0,
	messageCount: 0,
	thinkingLevel: "high",
	model: { provider: "compat", id: "fake-model" },
};

const READY_FRAME = {
	type: "ready",
	protocolVersion: 1,
	supportedProtocolVersions: [1, 2],
	maxFrameBytes: 1048576,
	maxReassembledFrameBytes: Number(process.env.FAKE_OMP_MAX_REASSEMBLED ?? 67108864),
	supportsBashExcludeFromContext: true,
};

/** Emit one logical frame, optionally split into rpc_chunk frames. */
function emitFrame(record) {
	if (process.env.FAKE_OMP_CHUNK !== "1") {
		send(record);
		return;
	}
	const json = JSON.stringify(record);
	const bytes = Buffer.from(json, "utf-8");
	const size = Math.max(1, Number(process.env.FAKE_OMP_CHUNK_BYTES ?? 1000));
	const count = Math.ceil(bytes.length / size);
	for (let index = 0; index < count; index++) {
		const slice = bytes.subarray(index * size, (index + 1) * size);
		send({
			type: "rpc_chunk",
			chunkId: "rpc-1",
			index,
			count,
			byteLength: bytes.length,
			data: slice.toString("base64"),
		});
	}
}

let uiWaiter = null;
const uiResponses = [];

function waitForUiResponse(id) {
	const existing = uiResponses.find((r) => r.id === id);
	if (existing) return Promise.resolve();
	return new Promise((resolve) => {
		uiWaiter = { id, resolve };
	});
}

async function runPrompt(cmd) {
	state.isStreaming = true;
	state.isSettled = false;
	emitFrame({ type: "agent_start" });
	emitFrame({ type: "turn_start" });
	if (process.env.FAKE_OMP_THINKING === "1") {
		emitFrame({
			type: "message_update",
			assistantMessageEvent: {
				type: "thinking_delta",
				contentIndex: 0,
				delta: "I should think.",
			},
			message: { role: "assistant", content: [{ type: "text", text: "" }] },
		});
	}
	if (process.env.FAKE_OMP_TOOL === "1") {
		emitFrame({
			type: "tool_execution_start",
			toolCallId: "call_1",
			toolName: "bash",
			args: { command: "echo spike-tool" },
		});
		emitFrame({
			type: "tool_execution_update",
			toolCallId: "call_1",
			toolName: "bash",
			args: { command: "echo spike-tool" },
			partialResult: { content: [{ type: "text", text: "spike-" }] },
		});
		emitFrame({
			type: "tool_execution_end",
			toolCallId: "call_1",
			toolName: "bash",
			result: { content: [{ type: "text", text: "spike-tool" }], details: {} },
			isError: false,
		});
	}
	if (process.env.FAKE_OMP_UI === "1") {
		const requestId = "ui-" + Math.random().toString(36).slice(2);
		emitFrame({
			type: "extension_ui_request",
			id: requestId,
			method: "select",
			title: "Allow tool: bash\nCommand: echo spike-tool",
			options: ["Approve", "Approve bash for this session", "Deny"],
		});
		// The real agent blocks until the host answers the dialog.
		await waitForUiResponse(requestId);
	}
	const text = process.env.FAKE_OMP_CHUNK === "1" ? "X".repeat(2000) : "FAKE OMP REPLY";
	emitFrame({
		type: "message_update",
		assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: text },
		message: { role: "assistant", content: [{ type: "text", text }] },
	});
	emitFrame({
		type: "message_end",
		message: { role: "assistant", content: [{ type: "text", text }] },
	});
	emitFrame({ type: "turn_end", message: {}, toolResults: [] });
	emitFrame({ type: "agent_end", messages: [], willRetry: false });

	state.isStreaming = false;
	state.messageCount += 2;
	const status =
		process.env.FAKE_OMP_ERROR === "1"
			? "error"
			: process.env.FAKE_OMP_ABORT === "1"
				? "aborted"
				: "completed";
	const settled = process.env.FAKE_OMP_UNSETTLED !== "1";
	state.isSettled = settled;
	send({
		type: "prompt_result",
		id: cmd.id,
		agentInvoked: true,
		status,
		sessionSettled: settled,
		...(status === "error"
			? {
					error: {
						message: "fake provider failure",
						provider: "compat",
						model: "fake-model",
						retryable: false,
						httpStatus: 400,
					},
				}
			: {}),
	});
	send({ type: "session_settled" });
	if (process.env.FAKE_OMP_QUEUED_TURN === "1") {
		// A follow-up queued during the run starts its OWN turn after this
		// prompt's result: the session is not settled yet, and the queued
		// answer must reach the caller that owns the run window.
		state.isSettled = false;
		state.queuedMessageCount = 1;
		setTimeout(() => {
			emitFrame({ type: "agent_start" });
			emitFrame({
				type: "message_update",
				assistantMessageEvent: {
					type: "text_delta",
					contentIndex: 0,
					delta: "QUEUED ANSWER",
				},
				message: { role: "assistant", content: [] },
			});
			emitFrame({ type: "agent_end", messages: [], willRetry: false });
			state.isSettled = true;
			state.queuedMessageCount = 0;
			send({ type: "session_settled" });
		}, 300);
	}
}

async function handle(cmd) {
	logCmd(cmd);
	const type = cmd.type;
	const id = cmd.id;
	const reply = (data, success = true, error) =>
		send({
			type: "response",
			command: type,
			success,
			...(error ? { error } : {}),
			...(data !== undefined ? { data } : {}),
			id,
		});

	switch (type) {
		case "negotiate_protocol":
			reply({ protocolVersion: Number(cmd.protocolVersion ?? 1) });
			break;
		case "set_subagent_subscription":
			reply({ level: cmd.level });
			break;
		case "get_state":
			reply({
				sessionId: state.sessionId,
				sessionFile: state.sessionFile,
				isStreaming: state.isStreaming,
				isSettled: state.isSettled,
				queuedMessageCount: state.queuedMessageCount,
				queuedMessages: { steering: [], followUp: [], liveSteered: 0 },
				hasPendingAsyncWork: false,
				messageCount: state.messageCount,
				thinkingLevel: state.thinkingLevel,
				model: state.model,
			});
			break;
		case "new_session":
			reply({ cancelled: false });
			break;
		case "switch_session": {
			const target = String(cmd.sessionPath ?? "");
			if (process.env.FAKE_OMP_FORK === "1") {
				// A lease-fork: the session moves to a new file (and id).
				state.sessionFile = `/data/sessions/1700000009999_${state.sessionId}.jsonl`;
				state.sessionId = `${state.sessionId}-fork`;
			} else {
				state.sessionFile = target;
				state.sessionId = target
					.split("/")
					.pop()
					.replace(/\.jsonl$/, "")
					.replace(/^\d+_/, "");
			}
			reply({ cancelled: false });
			break;
		}
		case "abort":
			reply();
			break;
		case "prompt": {
			if (process.env.FAKE_OMP_DELAY_PROMPT_MS) {
				await delay(Number(process.env.FAKE_OMP_DELAY_PROMPT_MS));
			}
			const message = String(cmd.message ?? "");
			if (message.trim().startsWith("/")) {
				// A built-in slash command answers locally — no agent run.
				send({ type: "command_output", text: `Current model: ${state.model.id}` });
				reply({ agentInvoked: false });
				break;
			}
			if (state.isStreaming && !cmd.streamingBehavior) {
				const busyMessage =
					"Agent is already processing. Use steer() or followUp() to queue messages, or wait for completion.";
				reply(undefined, false, busyMessage);
				send({
					type: "prompt_result",
					id,
					agentInvoked: false,
					status: "error",
					sessionSettled: false,
					error: { message: busyMessage, retryable: false },
				});
				break;
			}
			if (cmd.streamingBehavior) {
				// Queued behind the running turn — no new run window.
				reply({ agentInvoked: false });
				send({
					type: "queue_update",
					steering: cmd.streamingBehavior === "steer" ? [message] : [],
					followUp: cmd.streamingBehavior === "followUp" ? [message] : [],
					liveSteered: 0,
				});
				break;
			}
			reply();
			await runPrompt(cmd);
			break;
		}
		case "steer":
		case "follow_up":
			reply({ agentInvoked: false });
			state.queuedMessageCount = 1;
			break;
		case "get_session_stats":
			reply({ sessionId: state.sessionId, tokens: { total: 42 } });
			break;
		default:
			reply(undefined, false, `unknown command: ${type}`);
	}
}

async function onLine(line) {
	let cmd;
	try {
		cmd = JSON.parse(line);
	} catch {
		send({ type: "response", command: "parse", success: false, error: "Failed to parse command" });
		return;
	}
	if (cmd.type === "extension_ui_response") {
		uiResponses.push(cmd);
		if (uiWaiter && uiWaiter.id === cmd.id) {
			uiWaiter.resolve();
			uiWaiter = null;
		}
		return;
	}
	await handle(cmd);
}

process.stderr.write("fake-omp ready\n");

// The real omp announces itself with a `ready` frame before it accepts
// commands, and emits fire-and-forget UI frames (setWidget) around them.
send(READY_FRAME);
send({ type: "extension_ui_request", id: "widget-1", method: "setWidget", widgetKey: "autoresearch" });

// Strict LF-only framing.
process.stdin.on("data", (chunk) => {
	buffer += decoder.write(chunk);
	let index;
	while ((index = buffer.indexOf("\n")) !== -1) {
		const rawLine = buffer.slice(0, index).replace(/\r$/, "");
		buffer = buffer.slice(index + 1);
		if (rawLine.length) void onLine(rawLine);
	}
});
let closedDown = false;
function shutdown() {
	if (closedDown) return;
	closedDown = true;
	logCmd({ type: "__stdin_closed" });
	process.exit(0);
}
process.stdin.on("end", shutdown);
process.stdin.on("close", shutdown);
