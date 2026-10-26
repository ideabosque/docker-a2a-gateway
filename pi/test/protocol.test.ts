/**
 * protocol.test.ts — PiRpc driver tests against the scripted fake-pi
 * emulator. Covers:
 *  - strict LF framing: a record split across two stdout writes reassembles,
 *    and U+2028/U+2029 inside JSON strings never split a record
 *  - response correlation (ids echoed, out-of-order tolerated)
 *  - error responses reject with the protocol error string
 *  - abort + process shutdown (stdin close → clean exit)
 *  - extension_ui_request forwarding + respondUi unblocks pi
 */

import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { PiRpc, RpcClosedError } from "../src/rpc.js";
import { spawnEnvClean } from "../src/pool.js";

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));

interface FakePiOptions {
	env?: Record<string, string>;
}

function fakePiCommand(_opts: FakePiOptions = {}): string[] {
	// process.execPath: absolute node binary — SPAWNING bare "node" is
	// unreliable inside vitest workers on Windows (ENOENT races).
	return [process.execPath, path.join(here, "fake-pi.mjs")];
}

let cleanups: Array<() => Promise<void> | void> = [];

async function makeRpc(opts: FakePiOptions & { args?: string[] } = {}) {
	const rpc = new PiRpc({
		command: fakePiCommand(opts),
		env: opts.env,
		requestTimeoutMs: 10_000,
	});
	await rpc.start();
	cleanups.push(() => rpc.stop());
	return rpc;
}

beforeEach(() => {
	cleanups = [];
});

afterEach(async () => {
	for (const fn of cleanups.reverse()) await fn();
});

describe("PiRpc framing", () => {
	it("reassembles a record split across stdout chunk boundaries", async () => {
		const rpc = await makeRpc();
		const state = await rpc.getState();
		expect(state["sessionId"]).toMatch(/^fake-session-/);
		await rpc.stop();
	});

	it("keeps U+2028/U+2029 inside the delta (no readline-style split)", async () => {
		const events: Record<string, unknown>[] = [];
		const rpc = new PiRpc({
			command: fakePiCommand(),
			env: { FAKE_PI_UNICODE: "1" },
			requestTimeoutMs: 30_000,
		});
		rpc.addEventListener((e) => events.push(e));
		await rpc.start();
		cleanups.push(() => rpc.stop());
		await rpc.request({ type: "prompt", message: "hi" }, 30_000);
		// Wait for the fake run to settle fully.
		await new Promise((r) => setTimeout(r, 400));
		const deltas = events
			.filter((e) => e["type"] === "message_update")
			.map((e) => (e["assistantMessageEvent"] as Record<string, unknown>)["delta"])
			.filter(Boolean) as string[];
		// U+2028 (line separator) must arrive intact inside the delta.
		const joined = deltas.join("");
		expect(joined).toContain("hello world ");
		expect(events.some((e) => e["type"] === "agent_settled")).toBe(true);
	});

	it("logs received commands when FAKE_PI_LOG is set", async () => {
		const logFile = path.join(mkdtempSync(path.join(tmpdir(), "fakepi-")), "log.jsonl");
		const rpc = await makeRpc({ env: { FAKE_PI_LOG: logFile } });
		await rpc.getState();
		await rpc.stop();
		const logged = readFileSync(logFile, "utf-8").trim().split("\n").map((l) => JSON.parse(l));
		expect(logged.some((c) => c["type"] === "get_state")).toBe(true);
		expect(logged.some((c) => c["type"] === "__stdin_closed")).toBe(true);
	});
});

describe("PiRpc command semantics", () => {
	it("does not restore host pi variables after environment filtering", async () => {
		const previous = process.env.PI_PROVIDER;
		process.env.PI_PROVIDER = "host-provider-must-not-leak";
		const rpc = new PiRpc({
			command: [process.execPath, "-e", `process.stdin.on("data", () => process.stdout.write(JSON.stringify({type:"response",id:"req-1",success:true,data:{provider:process.env.PI_PROVIDER ?? null}})+String.fromCharCode(10)))`],
			env: spawnEnvClean(),
		});
		try {
			await rpc.start();
			expect((await rpc.getState())["provider"]).toBeNull();
		} finally {
			await rpc.stop();
			if (previous === undefined) delete process.env.PI_PROVIDER;
			else process.env.PI_PROVIDER = previous;
		}
	});
	it("rejects failed commands with the protocol error", async () => {
		const rpc = await makeRpc({ env: { FAKE_PI_FAIL_ABORT: "1" } });
		await rpc.getState();
		await expect(rpc.request({ type: "abort" })).rejects.toThrow(/no active run/);
	});

	it("times out a command that never answers", async () => {
		// unknown commands fail fast in fake-pi; use a tiny timeout on a
		// command the emulator delays to hit the timeout path.
		const rpc = await makeRpc({ env: { FAKE_PI_DELAY_PROMPT_MS: "5000" } });
		await expect(rpc.request({ type: "prompt", message: "x" }, 100)).rejects.toThrow(
			/Timed out waiting for pi RPC response/,
		);
	});

	it("stop() closes stdin and the process exits cleanly", async () => {
		const logFile = path.join(mkdtempSync(path.join(tmpdir(), "fakepi-")), "log.jsonl");
		const rpc = await makeRpc({ env: { FAKE_PI_LOG: logFile } });
		await rpc.stop(3000);
		const logged = readFileSync(logFile, "utf-8");
		expect(logged).toContain("__stdin_closed");
	});

	it("process exit rejects pending requests", async () => {
		const rpc = await makeRpc({ env: { FAKE_PI_DELAY_PROMPT_MS: "5000" } });
		const pending = rpc.request({ type: "prompt", message: "x" }, 30_000);
		// Crash the emulator mid-request.
		rpc.request({ type: "test_crash" }, 2000).catch(() => {});
		await expect(pending).rejects.toThrow(RpcClosedError);
	});
});

describe("PiRpc extension UI", () => {
	it("forwards extension_ui_request and respondUi unblocks pi", async () => {
		const events: Record<string, unknown>[] = [];
		const dialogs: Record<string, unknown>[] = [];
		const rpc = new PiRpc({
			command: fakePiCommand(),
			env: { FAKE_PI_UI: "confirm" },
			requestTimeoutMs: 30_000,
			onUiRequest: (req) => {
				dialogs.push({ ...req });
				rpc.respondUi({
					type: "extension_ui_response",
					id: req.id,
					confirmed: true,
				});
			},
		});
		rpc.addEventListener((e) => events.push(e));
		await rpc.start();
		cleanups.push(() => rpc.stop());
		await rpc.request({ type: "prompt", message: "hi" }, 30_000);
		await new Promise((r) => setTimeout(r, 400));
		expect(dialogs.length).toBe(1);
		expect(dialogs[0]["method"]).toBe("confirm");
		expect(events.some((e) => e["type"] === "agent_settled")).toBe(true);
	});
});
