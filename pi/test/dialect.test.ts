/**
 * dialect.test.ts — the omp dialect of the sidecar, against the scripted
 * fake-omp emulator. Covers:
 *  - the `ready` handshake (the sidecar waits for it before any command)
 *  - the protocol v2 negotiation that follows it
 *  - `rpc_chunk` reassembly (and the rejection of an oversized sequence)
 *  - the run window: `prompt_result` → run_end (completed / error / aborted)
 *  - a prompt that started no run (agentInvoked:false) → stopReason "handled"
 *  - the session-file fork detection of the pool
 */

import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { createDialect } from "../src/dialect.js";
import { PiRpc } from "../src/rpc.js";
import { SessionPool } from "../src/pool.js";
import { createServer } from "../src/server.js";

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));

const omp = createDialect("omp");

/** The emulator command (process.execPath: absolute node binary). */
function fakeOmpCommand(): string[] {
	return [process.execPath, path.join(here, "fake-omp.mjs")];
}

const cleanups: Array<() => Promise<void> | void> = [];

afterEach(async () => {
	for (const fn of cleanups.reverse()) await fn();
	cleanups.length = 0;
});

async function makeRpc(env: Record<string, string> = {}): Promise<PiRpc> {
	const rpc = new PiRpc({
		command: fakeOmpCommand(),
		env,
		requestTimeoutMs: 15_000,
		dialect: omp,
	});
	await rpc.start();
	cleanups.push(() => rpc.stop());
	return rpc;
}

/** Resolve with the first agent frame that matches `predicate`. */
function waitForFrame(
	rpc: PiRpc,
	predicate: (event: Record<string, unknown>) => boolean,
): Promise<Record<string, unknown>> {
	return new Promise((resolve) => {
		const unsubscribe = rpc.addEventListener((event) => {
			if (!predicate(event)) return;
			unsubscribe();
			resolve(event);
		});
	});
}

/** Poll the emulator's command log until the wanted command shows up. */
async function waitForLoggedCommand(
	logFile: string,
	match: (command: Record<string, unknown>) => boolean,
): Promise<Record<string, unknown>> {
	for (let attempt = 0; attempt < 100; attempt++) {
		const raw = await readFile(logFile, "utf-8").catch(() => "");
		const commands = raw
			.trim()
			.split("\n")
			.filter(Boolean)
			.map((line) => JSON.parse(line) as Record<string, unknown>);
		const found = commands.find(match);
		if (found) return found;
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
	throw new Error("the emulator did not log the expected command");
}

/** A pool that drives the omp emulator. */
function makePool(dataDir: string, env: Record<string, string> = {}): SessionPool {
	return new SessionPool({
		baseArgs: fakeOmpCommand(),
		dataDir,
		sessionDir: path.join(dataDir, "sessions"),
		maxProcesses: 2,
		idleTimeoutMs: 0,
		defaultCwd: dataDir,
		dialect: omp,
		env,
		logger: () => {},
	});
}

interface RunResult {
	frames: Record<string, unknown>[];
	runEnd: Record<string, unknown>;
}

/** Run one prompt through the HTTP boundary and return the SSE frames. */
async function runPrompt(env: Record<string, string>, message = "hello"): Promise<RunResult> {
	const dir = await mkdtemp(path.join(tmpdir(), "omp-dialect-"));
	const pool = makePool(dir, env);
	const server = createServer({
		port: 0,
		secretKey: "test",
		pool,
		piVersion: "test",
		streamTimeoutMs: 10_000,
		maxBodyBytes: 65_536,
		log: () => {},
		dialect: omp,
	});
	try {
		await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
		const address = server.address() as { port: number };
		const session = await pool.create({ cwd: dir });
		const response = await fetch(
			`http://127.0.0.1:${address.port}/sessions/${session.id}/prompt`,
			{
				method: "POST",
				headers: { "X-Secret-Key": "test", "content-type": "application/json" },
				body: JSON.stringify({ message }),
				signal: AbortSignal.timeout(20_000),
			},
		);
		const frames = (await response.text())
			.trim()
			.split("\n\n")
			.map((frame) => JSON.parse(frame.slice(6)) as Record<string, unknown>);
		return { frames, runEnd: frames[frames.length - 1] };
	} finally {
		await pool.closeAll();
		await new Promise<void>((resolve) => server.close(() => resolve()));
		await rm(dir, { recursive: true, force: true });
	}
}

describe("omp transport", () => {
	it("waits for the ready frame and exposes it", async () => {
		const rpc = await makeRpc();
		expect(rpc.readyFrame).toMatchObject({ type: "ready", protocolVersion: 1 });
		expect(rpc.readyFrame?.["supportedProtocolVersions"]).toEqual([1, 2]);
		expect(rpc.maxReassembledFrameBytes).toBe(67_108_864);
		// The handshake gates the commands: the state is answerable right away.
		expect((await rpc.getState())["sessionId"]).toMatch(/^fake-omp-session-/);
	});

	it("negotiates protocol v2 right after the ready frame", async () => {
		const dir = await mkdtemp(path.join(tmpdir(), "omp-negotiate-"));
		const logFile = path.join(dir, "log.jsonl");
		const rpc = await makeRpc({ FAKE_OMP_LOG: logFile });
		await rpc.getState();
		const negotiation = await waitForLoggedCommand(
			logFile,
			(command) => command["type"] === "negotiate_protocol",
		);
		expect(negotiation).toMatchObject({
			id: "protocol-2",
			protocolVersion: 2,
		});
		await rm(dir, { recursive: true, force: true });
	});

	it("reassembles rpc_chunk frames into one logical frame", async () => {
		const events: Record<string, unknown>[] = [];
		const rpc = new PiRpc({
			command: fakeOmpCommand(),
			env: { FAKE_OMP_CHUNK: "1", FAKE_OMP_CHUNK_BYTES: "512" },
			requestTimeoutMs: 15_000,
			dialect: omp,
		});
		rpc.addEventListener((event) => events.push(event));
		await rpc.start();
		cleanups.push(() => rpc.stop());
		const settled = waitForFrame(rpc, (event) => event["type"] === "prompt_result");
		await rpc.request({ type: "prompt", message: "hi" }, 15_000);
		await settled;
		const deltas = events
			.filter((event) => event["type"] === "message_update")
			.map(
				(event) =>
					(event["assistantMessageEvent"] as Record<string, unknown> | undefined)?.[
						"delta"
					],
			)
			.filter(Boolean) as string[];
		expect(deltas.join("")).toHaveLength(2000);
		expect(events.some((event) => event["type"] === "rpc_chunk")).toBe(false);
	});

	it("discards a chunk sequence above the advertised limit", async () => {
		const events: Record<string, unknown>[] = [];
		const rpc = new PiRpc({
			command: fakeOmpCommand(),
			env: {
				FAKE_OMP_CHUNK: "1",
				FAKE_OMP_CHUNK_BYTES: "512",
				FAKE_OMP_MAX_REASSEMBLED: "1024",
			},
			requestTimeoutMs: 15_000,
			dialect: omp,
		});
		rpc.addEventListener((event) => events.push(event));
		await rpc.start();
		cleanups.push(() => rpc.stop());
		const settled = waitForFrame(rpc, (event) => event["type"] === "prompt_result");
		await rpc.request({ type: "prompt", message: "hi" }, 15_000);
		await settled;
		const frameErrors = events.filter((event) => event["type"] === "rpc_frame_error");
		expect(frameErrors.length).toBeGreaterThan(0);
		expect(frameErrors[0]["error"]).toBe("frame exceeded maxReassembledFrameBytes");
		// The oversized frame never reaches the listeners.
		expect(
			events.some(
				(event) =>
					event["type"] === "message_update" &&
					((event["assistantMessageEvent"] as Record<string, unknown> | undefined)?.[
						"delta"
					] as string | undefined)?.length === 2000,
			),
		).toBe(false);
	});
});

describe("omp run window", () => {
	it("maps a completed prompt_result to end_turn", async () => {
		const { frames, runEnd } = await runPrompt({});
		expect(runEnd).toMatchObject({
			type: "run_end",
			stopReason: "end_turn",
			aborted: false,
		});
		// The agent events ride the stream before the run_end frame.
		expect(frames.some((frame) => frame["type"] === "message_update")).toBe(true);
		expect(frames.some((frame) => frame["type"] === "prompt_result")).toBe(true);
	});

	it("keeps the run window open until a queued turn finished", async () => {
		// A follow-up queued during the run starts its own turn after the
		// prompt's result. The window must stay open until that turn is
		// done, or the queued answer never reaches the caller.
		const { frames, runEnd } = await runPrompt({ FAKE_OMP_QUEUED_TURN: "1" });
		const deltas = frames
			.filter((frame) => frame["type"] === "message_update")
			.map(
				(frame) =>
					(frame["assistantMessageEvent"] as Record<string, unknown> | undefined)?.[
						"delta"
					],
			)
			.filter(Boolean) as string[];
		expect(deltas.join("")).toContain("QUEUED ANSWER");
		expect(runEnd).toMatchObject({ type: "run_end", stopReason: "end_turn" });
		// The run_end frame closes the window — it comes last.
		expect(frames[frames.length - 1]).toBe(runEnd);
	});

	it("maps agentInvoked:false to the handled stop reason", async () => {
		const { runEnd } = await runPrompt({}, "/usage");
		expect(runEnd).toMatchObject({
			type: "run_end",
			stopReason: "handled",
			aborted: false,
		});
	});

	it("maps a failed prompt_result to run_end.error", async () => {
		const { runEnd } = await runPrompt({ FAKE_OMP_ERROR: "1" });
		expect(runEnd).toMatchObject({
			type: "run_end",
			stopReason: "error",
			aborted: false,
			error: "fake provider failure",
		});
	});

	it("maps an aborted prompt_result to run_end.aborted", async () => {
		const { runEnd } = await runPrompt({ FAKE_OMP_ABORT: "1" });
		expect(runEnd).toMatchObject({
			type: "run_end",
			stopReason: "aborted",
			aborted: true,
		});
	});

	it("answers the health probe with the dialect key", async () => {
		const dir = await mkdtemp(path.join(tmpdir(), "omp-health-"));
		const pool = makePool(dir);
		const server = createServer({
			port: 0,
			secretKey: "test",
			pool,
			piVersion: "test",
			streamTimeoutMs: 1000,
			maxBodyBytes: 4096,
			log: () => {},
			dialect: omp,
		});
		try {
			await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
			const address = server.address() as { port: number };
			const response = await fetch(`http://127.0.0.1:${address.port}/health`, {
				headers: { "X-Secret-Key": "test" },
			});
			expect(await response.json()).toMatchObject({
				status: "ok",
				dialect: "omp",
				omp: "test",
			});
		} finally {
			await pool.closeAll();
			await new Promise<void>((resolve) => server.close(() => resolve()));
			await rm(dir, { recursive: true, force: true });
		}
	});
});

describe("omp session index", () => {
	it("follows a forked session file and drops the stale entry", async () => {
		const dir = await mkdtemp(path.join(tmpdir(), "omp-fork-"));
		const pool = makePool(dir, { FAKE_OMP_FORK: "1" });
		try {
			const session = await pool.create({ cwd: dir });
			await pool.close(session.id);
			const resumed = await pool.getOrResume(session.id);
			expect(resumed).toBeDefined();
			expect(resumed!.id).not.toBe(session.id);
			const index = JSON.parse(
				await readFile(path.join(dir, "sessions-index.json"), "utf-8"),
			) as { sessions: Record<string, { sessionFile: string }> };
			// The stale entry is gone — the index follows the live session.
			expect(Object.keys(index.sessions)).toEqual([resumed!.id]);
			expect(resumed!.id).toMatch(/-fork$/);
			expect(index.sessions[resumed!.id].sessionFile).toMatch(
				/\/data\/sessions\/1700000009999_.*\.jsonl$/,
			);
		} finally {
			await pool.closeAll();
			await rm(dir, { recursive: true, force: true });
		}
	});
});
