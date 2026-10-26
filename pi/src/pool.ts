/**
 * pool.ts â€” pi session pool (one process per active session).
 *
 * Sessions are keyed by pi's own sessionId (from get_state). The session FILE
 * survives process exit, so the pool may close idle processes at any time and
 * transparently re-open the stored file (switch_session) when the session is
 * prompted again â€” the A2A conversation keeps working across sidecar
 * restarts through the persisted index (sessions-index.json).
 *
 * Guards, per docs/PI_INTEGRATION_PLAN.md Â§1:
 * - one run per session: the second prompt while streaming is a 409 unless
 *   it carries `streamingBehavior` (pass-through â€” the HTTP layer decides);
 * - PI_MAX_PROCESSES caps live processes (503 when full);
 * - idle reaping closes processes with no use for PI_IDLE_TIMEOUT (the
 *   session file stays on the volume).
 */

import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { type JsonRecord, PiRpc, type RpcResponse } from "./rpc.js";

export interface CreateOptions {
	cwd?: string;
	/** "provider/modelId" â€” split on the first "/" into --provider/--model. */
	model?: string;
	/** pi thinking level: off â€¦ max. */
	thinking?: string;
}

export interface PoolInit {
	baseArgs: string[];
	dataDir: string;
	sessionDir: string;
	maxProcesses: number;
	idleTimeoutMs: number;
	/** Sweep period for the idle reaper (ms). Default 30_000. */
	sweepIntervalMs?: number;
	defaultCwd: string;
	env?: Record<string, string | undefined>;
	onEvent?: (sessionId: string, event: JsonRecord) => void;
	onStderr?: (sessionId: string, line: string) => void;
	onExit?: (sessionId: string, code: number | null, signal: string | null) => void;
	logger?: (level: "info" | "warn" | "error", message: string) => void;
}

export class PoolError extends Error {
	readonly status: number;
	constructor(message: string, status: number) {
		super(message);
		this.status = status;
	}
}

export interface PoolSession {
	id: string;
	sessionFile: string | null;
	cwd: string;
	rpc: PiRpc;
	streaming: boolean;
	lastUsed: number;
	/** "provider/modelId" this session was created with (persisted for resume). */
	model?: string;
	thinking?: string;
}

export class SessionPool {
	private readonly sessions = new Map<string, PoolSession>();
	private creating = 0;
	private readonly resuming = new Map<string, Promise<PoolSession>>();
	private readonly indexer: Promise<
		Record<string, { sessionFile: string; cwd: string; model?: string; thinking?: string }>
	>;
	private reaper: NodeJS.Timeout | null = null;

	constructor(private readonly init: PoolInit) {
		this.indexer = this.loadIndex();
		if (this.init.idleTimeoutMs > 0) {
			const period = this.init.sweepIntervalMs ?? 30_000;
			this.reaper = setInterval(() => void this.reapIdle(), period);
			this.reaper.unref();
		}
	}

	get activeCount(): number {
		return this.sessions.size;
	}

	list(): Array<{ id: string; streaming: boolean; sessionFile: string | null }> {
		return [...this.sessions.values()].map((s) => ({
			id: s.id,
			streaming: s.streaming,
			sessionFile: s.sessionFile,
		}));
	}

	get(id: string): PoolSession | undefined {
		return this.sessions.get(id);
	}

	isStreaming(id: string): boolean {
		return this.sessions.get(id)?.streaming ?? false;
	}

	/** Create a fresh session (new process, new pi session). */
	async create(opts: CreateOptions = {}, resumeFile?: string): Promise<PoolSession> {
		// Reserve capacity before the first await so concurrent requests cannot
		// all pass the cap while their subprocesses are still starting.
		if (this.sessions.size + this.creating >= this.init.maxProcesses) {
			throw new PoolError(
				`pi sidecar at the process cap (${this.init.maxProcesses}); ` +
					"close sessions or raise PI_MAX_PROCESSES",
				503,
			);
		}
		this.creating++;
		let rpc: PiRpc | undefined;
		try {
			const cwd = opts.cwd || this.init.defaultCwd;
			const args = [...this.init.baseArgs];
			const model = opts.model || undefined;
			if (model) {
				const slash = model.indexOf("/");
				if (slash > 0) {
					args.push("--provider", model.slice(0, slash), "--model", model.slice(slash + 1));
				} else {
					args.push("--model", model);
				}
			}
			if (opts.thinking) args.push("--thinking", opts.thinking);
			args.push("--session-dir", this.init.sessionDir);

			// Wait for pi to be ready (the RPC runtime answers commands when up).
			rpc = await this.spawn(cwd, args);
			if (resumeFile) {
				const resp = await rpc.request({ type: "switch_session", sessionPath: resumeFile });
				const data = (resp.data ?? {}) as Record<string, unknown>;
				if (data["cancelled"] === true) {
					throw new PoolError("an extension cancelled the session switch", 409);
				}
			}
			const state = await rpc.getState();
			const id = String(state["sessionId"] ?? `session-${Date.now()}`);
			const session: PoolSession = {
				id,
				sessionFile: state["sessionFile"] ? String(state["sessionFile"]) : null,
				cwd,
				rpc,
				streaming: Boolean(state["isStreaming"]),
				lastUsed: Date.now(),
				...(opts.model ? { model: opts.model } : {}),
				...(opts.thinking ? { thinking: opts.thinking } : {}),
			};
			this.sessions.set(id, session);
			this.log("info", `session ${id} live (${resumeFile ? "resumed" : "new"}) file=${session.sessionFile}`);
			await this.persist(id, session);
			return session;
		} catch (err) {
			await rpc?.stop();
			throw err;
		} finally {
			this.creating--;
		}
	}

	/** Return a live session, or re-open it from the persisted index. */
	async getOrResume(id: string): Promise<PoolSession | undefined> {
		const live = this.sessions.get(id);
		if (live) {
			live.lastUsed = Date.now();
			return live;
		}
		const index = await this.indexer;
		const meta = index[id];
		if (!meta?.sessionFile) return undefined;
		// Resume: new process + switch_session onto the stored file, booted
		// with the model (and thinking level) the session was created with.
		const existing = this.resuming.get(id);
		if (existing) return existing;
		const pending = this.create(
			{ cwd: meta.cwd, ...(meta.model ? { model: meta.model } : {}),
			  ...(meta.thinking ? { thinking: meta.thinking } : {}) },
			meta.sessionFile,
		);
		this.resuming.set(id, pending);
		try {
			return await pending;
		} finally {
			this.resuming.delete(id);
		}
	}

	setState(id: string, patch: Partial<Pick<PoolSession, "streaming">>): void {
		const s = this.sessions.get(id);
		if (!s) return;
		if (patch.streaming !== undefined) s.streaming = patch.streaming;
	}

	/** Abort the running prompt (waits for the session to settle). */
	async abort(id: string): Promise<void> {
		const session = this.sessions.get(id);
		if (!session) return;
		try {
			await session.rpc.request({ type: "abort" }, 30_000);
		} catch (err) {
			this.log("warn", `abort for ${id} failed: ${err}`);
		}
	}

	/** Stop the live process; the session file stays on disk. */
	async close(id: string, reason = "requested"): Promise<void> {
		const session = this.sessions.get(id);
		if (!session) return;
		this.sessions.delete(id);
		await session.rpc.stop();
		this.log("info", `session ${id} closed (${reason})`);
	}

	async closeAll(): Promise<void> {
		await Promise.all([...this.sessions.keys()].map((id) => this.close(id, "shutdown")));
	}

	async getState(id: string): Promise<Record<string, unknown>> {
		const session = this.sessions.get(id);
		if (!session) throw new PoolError(`unknown session ${id}`, 404);
		return session.rpc.getState();
	}

	async getStats(id: string): Promise<Record<string, unknown>> {
		const session = this.sessions.get(id);
		if (!session) throw new PoolError(`unknown session ${id}`, 404);
		const stats = await session.rpc.request({ type: "get_session_stats" }, 30_000);
		return (stats.data ?? {}) as Record<string, unknown>;
	}

	/** Persist the session index entry, then rewrite the index file. */
	private async persist(id: string, session: PoolSession): Promise<void> {
		const index = await this.indexer;
		if (session.sessionFile) {
			index[id] = {
				sessionFile: session.sessionFile,
				cwd: session.cwd,
			};
			// Resumed sessions boot with the model they were created with â€”
			// pi rejects unknown providers (P1 spike: a host PI_PROVIDER leak
			// otherwise wins the boot for model-less resume).
			if (session.model) index[id].model = session.model;
			if (session.thinking) index[id].thinking = session.thinking;
		}
		try {
			await writeFile(
				path.join(this.init.dataDir, "sessions-index.json"),
				JSON.stringify({ version: 1, sessions: index }, null, 2),
			);
		} catch (err) {
			this.log("warn", `index persistence failed: ${err}`);
		}
	}

	private async loadIndex(): Promise<
		Record<string, { sessionFile: string; cwd: string; model?: string; thinking?: string }>
	> {
		try {
			const raw = await readFile(
				path.join(this.init.dataDir, "sessions-index.json"),
				"utf-8",
			);
			const parsed = JSON.parse(raw) as {
				sessions?: Record<
					string,
					{ sessionFile: string; cwd: string; model?: string; thinking?: string }
				>;
			};
			return parsed.sessions ?? {};
		} catch {
			return {};
		}
	}

	private async reapIdle(): Promise<void> {
		const now = Date.now();
		for (const session of [...this.sessions.values()]) {
			if (session.streaming) continue;
			if (now - session.lastUsed > this.init.idleTimeoutMs) {
				await this.close(session.id, "idle timeout");
			}
		}
	}

	private async spawn(
		cwd: string,
		args: string[],
	): Promise<PiRpc> {
		// Session id is only known after get_state; events before that carry a
		// placeholder that is mapped to the real id by the caller (the server
		// subscribes only around its own run window).
		let id = "booting";
		const rpc = new PiRpc({
			command: args,
			cwd,
			env: { ...spawnEnvClean(), ...(this.init.env ?? {}) },
			onEvent: (event) => this.init.onEvent?.(id, event),
			onStderr: (line) => this.init.onStderr?.(id, line),
			onExit: (code, signal) => {
				// switch_session changes the id after boot. Remove only entries
				// belonging to this process, never a replacement for the same id.
				for (const [sessionId, session] of this.sessions) {
					if (session.rpc === rpc) {
						id = sessionId;
						this.sessions.delete(sessionId);
					}
				}
				this.init.onExit?.(id, code, signal);
			},
		});
		try {
			await rpc.start();
			const state = await rpc.getState();
			id = String(state["sessionId"] ?? id);
			return rpc;
		} catch (err) {
			await rpc.stop();
			throw err;
		}
	}

	private log(level: "info" | "warn" | "error", message: string): void {
		if (this.init.logger) this.init.logger(level, message);
		else if (level === "error") console.error(message);
		else console.log(message);
	}
}

/**
 * The pi child inherits the sidecar's environment MINUS the variables pi
 * itself exports/uses for its own tool shells (PI_MODEL / PI_PROVIDER /
 * PI_THINKING / PI_TOOLS). P1 spike (2026-10-25): on dev hosts those names
 * hold the operator's local pi state (e.g. PI_PROVIDER=ollama-cloud), and a
 * naive env passthrough made model-less sessions boot with a provider pi
 * rejects ("Unknown provider"). Provider credentials and the PI_COMPAT_*
 * interpolation targets are preserved.
 */
export function spawnEnvClean(): Record<string, string | undefined> {
	const blocked = new Set([
		"PI_MODEL",
		"PI_PROVIDER",
		"PI_THINKING",
		"PI_TOOLS",
		"PI_EXTRA_ARGS",
		"PI_APPROVE",
	]);
	const env: Record<string, string | undefined> = { ...process.env };
	// Explicit undefined overrides PiRpc's process.env merge; deleting keys
	// here would let the original host values back into the child.
	for (const key of blocked) env[key] = undefined;
	return env;
}

export function parseModelSpec(spec: string | undefined): { provider?: string; model?: string } {
	if (!spec) return {};
	const slash = spec.indexOf("/");
	if (slash > 0) {
		return { provider: spec.slice(0, slash), model: spec.slice(slash + 1) };
	}
	return { model: spec };
}

export type { RpcResponse };
