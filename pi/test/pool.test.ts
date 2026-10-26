/**
 * pool.test.ts — SessionPool lifecycle tests against fake-pi:
 *  - create returns pi's sessionId + sessionFile
 *  - the persisted index survives and getOrResume re-opens a session
 *    (switch_session path) after "restart" (fresh pool on the same data dir)
 *  - maxProcesses cap rejects with a 503-style PoolError
 *  - idle reaping closes non-streaming sessions past the idle timeout
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterAll, afterEach, beforeEach, describe, expect, it } from "vitest";

import { PoolError, SessionPool } from "../src/pool.js";

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));

const dirs: string[] = [];

function makePool(
	dataDir: string,
	opts: { maxProcesses?: number; idleTimeoutMs?: number; sweepIntervalMs?: number } = {},
) {
	return new SessionPool({
		baseArgs: [process.execPath, path.join(here, "fake-pi.mjs")],
		dataDir,
		sessionDir: path.join(dataDir, "sessions"),
		maxProcesses: opts.maxProcesses ?? 8,
		idleTimeoutMs: opts.idleTimeoutMs ?? 0,
		sweepIntervalMs: 200,
		defaultCwd: dataDir,
	});
}

beforeEach(() => {});
afterEach(async () => {});

afterAll(() => {
	for (const d of dirs.splice(0)) {
		try {
			rmSync(d, { recursive: true, force: true });
		} catch {
			/* windows file locks — best effort */
		}
	}
});

function newDir(label: string): string {
	const dir = mkdtempSync(path.join(tmpdir(), `pipool-${label}-`));
	dirs.push(dir);
	return dir;
}

describe("SessionPool", () => {
	it("creates sessions keyed by pi's sessionId", async () => {
		const dataDir = newDir("create");
		const pool = makePool(dataDir);
		// A cwd that exists on every OS (/workspace only exists in the
		// container; spawn with a missing cwd reports a misleading ENOENT).
		const session = await pool.create({ cwd: dataDir });
		expect(session.id).toMatch(/^fake-session-/);
		expect(session.sessionFile).toContain("/data/sessions/");
		expect(pool.activeCount).toBe(1);
		await pool.closeAll();
	});

	it("persists an index and resumes via getOrResume after restart", async () => {
		const dataDir = newDir("resume");
		const pool1 = makePool(dataDir);
		const session = await pool1.create({ cwd: dataDir });
		const stats = await pool1.getStats(session.id);
		expect(stats["tokens"]).toBeDefined();
		await pool1.closeAll();

		// "Restart": a fresh pool over the same data dir. The resumed id is
		// the session-file basename (deterministic switch_session mapping in
		// the emulator — as with pi, re-opening a file keeps its session id).
		const pool2 = makePool(dataDir);
		const fileBasename = path.basename(session.sessionFile!, ".jsonl");
		const resumed = await pool2.getOrResume(session.id);
		expect(resumed).toBeDefined();
		expect(resumed!.id).toBe(fileBasename);
		await pool2.closeAll();
	});

	it("unknown ids in getOrResume return undefined", async () => {
		const pool = makePool(newDir("unknown"));
		expect(await pool.getOrResume("nope")).toBeUndefined();
		await pool.closeAll();
	});

	it("enforces the process cap with a 503 PoolError", async () => {
		const pool = makePool(newDir("cap"), { maxProcesses: 1 });
		await pool.create({});
		await expect(pool.create({})).rejects.toSatisfy(
			(err: unknown) => err instanceof PoolError && err.status === 503,
		);
		// After closing the only session, creation works again.
		await pool.closeAll();
		await pool.create({});
		await pool.closeAll();
	});

	it("reaps idle sessions but keeps streaming ones", { timeout: 15_000 }, async () => {
		const pool = makePool(newDir("reap"), { idleTimeoutMs: 500 });
		const idle = await pool.create({});
		const busy = await pool.create({});
		// Rewind the clock: idle is old, busy is fresh and streaming.
		idle.lastUsed = Date.now() - 999_000;
		busy.lastUsed = Date.now() - 999_000;
		pool.setState(busy.id, { streaming: true });
		await new Promise((r) => setTimeout(r, 1_000));
		expect(pool.get(idle.id)).toBeUndefined();
		expect(pool.get(busy.id)).toBeDefined();
		await pool.closeAll();
	});
});
