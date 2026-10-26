#!/usr/bin/env node
/**
 * fake-pi.mjs — a scripted `pi --mode rpc` emulator for the sidecar unit
 * tests (vitest). Speaks the pi RPC JSONL protocol (strict LF framing) per
 * pi v1.4.0 docs. NOT used at runtime — the real sidecar spawns the real pi.
 *
 * Scripted behaviour via environment variables:
 *   FAKE_PI_LOG=<file>        append a JSON log of received commands
 *   FAKE_PI_THINKING=1        emit a thinking_delta before the answer
 *   FAKE_PI_TOOL=1            emit tool_execution_start/end
 *   FAKE_PI_UI=confirm        emit an extension_ui_request dialog on prompt
 *                             (waits for the response before settling)
 *   FAKE_PI_DELAY_PROMPT_MS=N delay the prompt response
 *   FAKE_PI_SPLIT_MS=N        emit one event's bytes in two stdout writes
 *                             across that gap (chunk-boundary framing probe)
 *   FAKE_PI_UNICODE=1         include U+2028/U+2029 inside the delta text
 *   FAKE_PI_FAIL_ABORT=1      abort answers success:false
 *
 * Events per prompt: message_update (thinking?) text deltas, optional tool
 * execution events, then message_end, turn_end, agent_end, agent_settled.
 */

import { appendFileSync } from "node:fs";
import path from "node:path";
import { StringDecoder } from "node:string_decoder";

// Synchronous file logging: async write streams race process.exit on
// Windows child pipes (writes were lost at shutdown).
const LOG_PATH = process.env.FAKE_PI_LOG || null;
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const decoder = new StringDecoder("utf-8");
let buffer = "";

function send(record) {
	// One JSON object per record, terminated with LF only.
	process.stdout.write(JSON.stringify(record) + "\n");
}

function emit(record, splitMs) {
	const line = JSON.stringify(record) + "\n";
	if (splitMs) {
		const mid = Math.max(1, Math.floor(line.length / 2));
		process.stdout.write(line.slice(0, mid));
		setTimeout(() => process.stdout.write(line.slice(mid)), splitMs);
	} else {
		process.stdout.write(line);
	}
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
// Resumed sessions take their id from the session-file basename instead.
const SESSION_UID = `${process.pid.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
const FRESH_ID = `fake-session-${SESSION_UID}`;

const state = {
	sessionId: FRESH_ID,
	sessionFile: `/data/sessions/${FRESH_ID}.jsonl`,
	isStreaming: false,
	pendingMessageCount: 0,
	provider: "mock",
	modelId: "mock-model",
};

async function runPrompt(cmd) {
	state.isStreaming = true;
	const unicode = process.env.FAKE_PI_UNICODE === "1";
	const textBits = unicode
		? ["hello world ", "!"]
		: ["FAKE PI ", "REPLY"];
	emit({ type: "agent_start" });
	emit({ type: "turn_start" });
	if (process.env.FAKE_PI_THINKING === "1") {
		emit({
			type: "message_update",
			usage: { input: 5, output: 1, totalTokens: 6 },
			assistantMessageEvent: { type: "thinking_delta", contentIndex: 0, delta: "I should think." },
		});
	}
	if (process.env.FAKE_PI_TOOL === "1") {
		emit({ type: "tool_execution_start", toolCallId: "call_1", toolName: "bash", args: { command: "ls" } });
		emit({
			type: "tool_execution_end",
			toolCallId: "call_1",
			toolName: "bash",
			result: { content: [{ type: "text", text: "file.txt" }] },
			isError: false,
			durationMs: 5,
		});
	}
	emit({
		type: "message_update",
		usage: { input: 10, output: 3, totalTokens: 13 },
		assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: textBits[0] },
	});
	// Second delta split across two stdout writes, if scripted.
	emit({
		type: "message_update",
		usage: { input: 10, output: 4, totalTokens: 14 },
		assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: textBits[1] },
	},
	Number(process.env.FAKE_PI_SPLIT_MS ?? 0));
	if (process.env.FAKE_PI_UI) {
		const method = process.env.FAKE_PI_UI;
		const requestId = "ui-" + Math.random().toString(36).slice(2);
		emit({
			type: "extension_ui_request",
			id: requestId,
			method,
			title: `${method} dialog`,
			sessionId: state.sessionId,
		});
		// real pi blocks until the client answers; wait for it on stdin.
		await waitForUiResponse(requestId);
	}
	emit({
		type: "message_end",
		message: { role: "assistant", content: [{ type: "text", text: textBits.join("") }] },
	});
	emit({ type: "turn_end", message: {}, toolResults: [] });
	emit({ type: "agent_end", messages: [], willRetry: false });
	emit({ type: "agent_settled", aborted: Boolean(cmd._aborted) });
	state.isStreaming = false;
}

let uiWaiter = null;
let uiResponses = [];
function waitForUiResponse(id) {
	const existing = uiResponses.find((r) => r.id === id);
	if (existing) return Promise.resolve();
	return new Promise((resolve) => {
		uiWaiter = { id, resolve };
	});
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
		case "get_state":
			reply({ ...state, model: { provider: state.provider, id: state.modelId } });
			break;
		case "new_session":
			reply({ cancelled: false });
			break;
		case "switch_session":
			// Deterministic: the session id is the file basename (mirrors pi,
			// where re-opening a file keeps the recorded session id).
			state.sessionFile = cmd.sessionPath;
			state.sessionId = path
				.basename(String(cmd.sessionPath))
				.replace(/\.jsonl$/, "");
			reply({ cancelled: false });
			break;
		case "abort":
			if (process.env.FAKE_PI_FAIL_ABORT === "1") {
				reply(undefined, false, "no active run");
			} else {
				state.pendingMessageCount = 0;
				reply();
			}
			break;
		case "prompt":
			if (process.env.FAKE_PI_DELAY_PROMPT_MS) {
				await delay(Number(process.env.FAKE_PI_DELAY_PROMPT_MS));
			}
			reply({ disposition: "started" });
			await runPrompt(cmd);
			break;
		case "steer":
		case "follow_up":
			reply({ disposition: "queued" });
			state.pendingMessageCount = 1;
			break;
		case "get_session_stats":
			reply({ sessionId: state.sessionId, tokens: { total: 42 } });
			break;
		default:
			reply(undefined, false, `unknown command: ${type}`);
	}
}

const pendingPromptAborts = [];

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
	if (cmd.type === "test_crash") process.exit(9);
	if (cmd.type === "abort") pendingPromptAborts.push(true);
	await handle(cmd);
}

process.stderr.write("fake-pi ready\n");

// Strict LF-only framing (Unicode separators must NOT split records).
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
	// pi (the real one) disposes its runtime and exits when stdin closes.
	if (closedDown) return;
	closedDown = true;
	logCmd({ type: "__stdin_closed" });
	process.exit(0);
}
// "end" on an ended pipe; "close" as the Windows fallback ("end" is not
// always delivered for child pipes on Windows).
process.stdin.on("end", shutdown);
process.stdin.on("close", shutdown);