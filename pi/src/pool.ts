/**
 * pool.ts — pi session pool (one process per active session).
 *
 * Sessions are keyed by pi's own sessionId (from get_state). The session FILE
 * survives process exit, so the pool may close idle processes at any time and
 * transparently re-open the stored file (switch_session) when the session is
 * prompted again — the A2A conversation keeps working across sidecar
 * restarts through the persisted index (sessions-index.json).
 *
 * Guards, per docs/PI_INTEGRATION_PLAN.md §1:
 * - one run per session: the second prompt while streaming is a 409 unless
 *   it carries `streamingBehavior` (pass-through — the HTTP layer decides);
 * - PI_MAX_PROCESSES caps live processes (503 when full);
 * - idle reaping closes processes with no use for PI_IDLE_TIMEOUT (the
 *   session file stays on the volume).
 */

import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { type JsonRecord, PiRpc, type RpcResponse } from "./rpc.js";

export interface CreateOptions {
	cwd?: string;
	/** "provider/modelId" — split on the first "/" into --provider/--model. */
	model?: string;
	/** pi thinking level: off … max. */
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
	busyBehaviorPrompt?: JsonRecord;
}

export class SessionPool {
	private readonly sessions = new Map<string, PoolSession>();
	private readonly indexer: Promise<Record<string, { sessionFile: string; cwd: string }>>;
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
		if (this.sessions.size >= this.init.maxProcesses) {
			throw new PoolError(
				`pi sidecar at the process cap (${this.init.maxProcesses}); ` +
					"close sessions or raise PI_MAX_PROCESSES",
				503,
			);
		}
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
		const rpc = await this.spawn(cwd, args, opts);
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
		};
		this.sessions.set(id, session);
		this.log("info", `session ${id} live (${resumeFile ? "resumed" : "new"}) file=${session.sessionFile}`);
		await this.persist(id, session);
		return session;
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
		// Resume: new process + switch_session onto the stored file.
		return this.create({ cwd: meta.cwd }, meta.sessionFile);
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

	private async loadIndex(): Promise<Record<string, { sessionFile: string; cwd: string }>> {
		try {
			const raw = await readFile(path.join(this.init.dataDir, "sessions-index.json"), "utf-8");
			const parsed = JSON.parse(raw) as { sessions?: Record<string, { sessionFile: string; cwd: string }> };
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
		opts: CreateOptions,
	): Promise<PiRpc> {
		// Session id is only known after get_state; events before that carry a
		// placeholder that is mapped to the real id by the caller (the server
		// subscribes only around its own run window).
		let id = "booting";
		const rpc = new PiRpc({
			command: args,
			cwd,
			env: this.init.env,
			onEvent: (event) => this.init.onEvent?.(id, event),
			onStderr: (line) => this.init.onStderr?.(id, line),
			onExit: (code, signal) => {
				this.sessions.delete(id);
				this.init.onExit?.(id, code, signal);
			},
		});
		void opts;
		await rpc.start();
		const state = await rpc.getState();
		id = String(state["sessionId"] ?? id);
		return rpc;
	}

	private log(level: "info" | "warn" | "error", message: string): void {
		if (this.init.logger) this.init.logger(level, message);
		else if (level === "error") console.error(message);
		else console.log(message);
	}
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