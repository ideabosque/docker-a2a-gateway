/**
 * rpc.ts — pi RPC subprocess driver.
 *
 * Implements the official pi RPC protocol (pi docs: rpc.md, rpc-commands.md,
 * json.md, rpc-extension-ui.md) over stdin/stdout JSONL:
 *
 * - Strict LF framing: records are split on LF only (pi warns that Node's
 *   readline also splits on U+2028/U+2029, which are valid inside JSON
 *   strings — so we decode UTF-8 chunk boundaries ourselves and split the
 *   decoded stream on "\n"; an LF byte never occurs inside a multi-byte
 *   UTF-8 sequence, so this is safe).
 * - Commands carry an id; the `response` record echoes it and may arrive out
 *   of order. Malformed input yields a `command: "parse"` response.
 * - Session events (no id) are forwarded to onEvent listeners.
 * - `extension_ui_request` records (dialogs: select/confirm/input/editor —
 *   blocking; notify/setStatus/... fire-and-forget) go to onUiRequest; the
 *   host answers by writing an `extension_ui_response` record to stdin via
 *   respondUi().
 *
 * The pi-shipped RpcClient (v1.4.0) cannot answer extension dialogs (its
 * stdin writer is private and it has no UI-response API), which is why this
 * small driver exists instead of importing it. The wire format is identical.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { StringDecoder } from "node:string_decoder";

import { createDialect, type AgentDialect } from "./dialect.js";

export type JsonRecord = Record<string, unknown>;

export interface RpcResponse {
	type: "response";
	command: string;
	success: boolean;
	data?: unknown;
	error?: string;
	id?: string;
}

export interface UiRequest {
	type: "extension_ui_request";
	id: string;
	method: string;
	[key: string]: unknown;
}

export interface RpcOptions {
	/** Full command line, e.g. ["pi", "--mode", "rpc", "--session-dir", "/data/sessions"] */
	command: string[];
	/** Working directory for the pi process. */
	cwd?: string;
	/** Extra environment variables layered over process.env. */
	env?: Record<string, string | undefined>;
	/** Default per-command response timeout (ms). Default 60_000. */
	requestTimeoutMs?: number;
	/** Every stdout record that is not a pending command response. */
	onEvent?: (event: JsonRecord) => void;
	/** `extension_ui_request` records (dialogs + fire-and-forget). */
	onUiRequest?: (request: UiRequest) => void;
	/** Diagnostics from stderr (never protocol data). */
	onStderr?: (chunk: string) => void;
	/** Called once when the process exits (code/signal or spawn error). */
	onExit?: (code: number | null, signal: string | null, err?: Error) => void;
	/** Protocol dialect of the spawned agent. Default: pi. */
	dialect?: AgentDialect;
}

export class RpcClosedError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "RpcClosedError";
	}
}

interface Pending {
	resolve: (response: RpcResponse) => void;
	reject: (error: Error) => void;
	timer: NodeJS.Timeout;
	command: string;
}

/** A partially received `rpc_chunk` sequence (protocol v2). */
interface ChunkSequence {
	chunkId: string;
	count: number;
	byteLength: number;
	segments: Map<number, Buffer>;
	bytes: number;
}

/** How long the agent may take to emit its `ready` frame (ms). */
const READY_TIMEOUT_MS = 30_000;

/** Reassembly limit used until the `ready` frame advertises its own. */
const DEFAULT_MAX_REASSEMBLED_BYTES = 64 * 1024 * 1024;

export class PiRpc {
	readonly connectionId = `a2a-${randomUUID()}`;
	private readonly opts: RpcOptions;
	private readonly dialect: AgentDialect;
	private child: ChildProcessWithoutNullStreams | null = null;
	private readonly decoder = new StringDecoder("utf-8");
	private lineBuf = "";
	private readonly pending = new Map<string, Pending>();
	private readonly eventListeners = new Set<(event: JsonRecord) => void>();
	private nextRequestId = 1;
	private exited = false;
	private stopRequested = false;
	private exitError: Error | null = null;
	private _stderrTail = "";
	private _readyFrame: JsonRecord | null = null;
	private readyWaiter: { resolve: () => void; reject: (error: Error) => void } | null = null;
	private chunkSequence: ChunkSequence | null = null;

	constructor(opts: RpcOptions) {
		this.opts = opts;
		this.dialect = opts.dialect ?? createDialect("pi");
	}

	/** Whether the child is running. */
	get alive(): boolean {
		return this.child !== null && this.child.exitCode === null && !this.exited;
	}

	/** The agent's `ready` frame (null until it arrives). */
	get readyFrame(): JsonRecord | null {
		return this._readyFrame;
	}

	/** Reassembly limit advertised by the `ready` frame (bytes). */
	get maxReassembledFrameBytes(): number {
		const advertised = Number(this._readyFrame?.["maxReassembledFrameBytes"]);
		return Number.isFinite(advertised) && advertised > 0
			? advertised
			: DEFAULT_MAX_REASSEMBLED_BYTES;
	}

	/** Last stderr chunk (for diagnostics in errors); truncated tail. */
	get stderrTail(): string {
		return this._stderrTail.slice(-2000);
	}

	/** Spawn the pi process and resolve once stdin/stdout are wired. */
	start(): Promise<void> {
		return new Promise((resolve, reject) => {
			let child: ChildProcessWithoutNullStreams;
			try {
				child = spawn(this.opts.command[0], this.opts.command.slice(1), {
					cwd: this.opts.cwd,
					env: { ...process.env, ...(this.opts.env ?? {}) },
					stdio: ["pipe", "pipe", "pipe"],
				});
			} catch (err) {
				reject(err instanceof Error ? err : new Error(String(err)));
				return;
			}
			this.child = child;

			child.stdout.on("data", (buf: Buffer) => this.read(buf));
			child.stderr.on("data", (buf: Buffer) => {
				const text = buf.toString("utf-8");
				this._stderrTail += text;
				if (this._stderrTail.length > 4000) {
					this._stderrTail = this._stderrTail.slice(-2000);
				}
				this.opts.onStderr?.(text);
			});
			child.on("error", (err: Error) => {
				this.exited = true;
				this.exitError = err instanceof Error ? err : new Error(String(err));
				this.failAllPending(this.exitError);
				this.failReady(this.exitError);
				this.opts.onExit?.(null, null, this.exitError);
				if (!this.stopRequested) reject(this.exitError);
			});
			child.on("close", (code: number | null, signal: string | null) => {
				this.exited = true;
				const err =
					this.exitError ??
					(this.stopRequested
						? null
						: new RpcClosedError(
								`pi RPC process exited (code=${code} signal=${signal}). ` +
									`Stderr: ${this.stderrTail}`,
							));
				if (err) this.failAllPending(err);
				this.failReady(err ?? new RpcClosedError("pi RPC process exited"));
				this.opts.onExit?.(code, signal, err ?? undefined);
				if (!this.stopRequested) {
					reject(err ?? new RpcClosedError("pi RPC process exited"));
					return;
				}
			});

		if (!this.dialect.needsReadyHandshake) {
			// The spawn itself succeeds unless the executable is missing; those
			// failures surface via "error"/"close" above.
			resolve();
			return;
		}

		// The agent gates every command behind its `ready` frame.
		const timer = setTimeout(() => {
			this.failReady(
				new Error(`${this.dialect.name} did not emit a ready frame within 30s`),
			);
		}, READY_TIMEOUT_MS);
		this.readyWaiter = {
			resolve: () => {
				clearTimeout(timer);
				resolve();
			},
			reject: (error: Error) => {
				clearTimeout(timer);
				reject(error);
			},
		};
	});
	}

	/** Subscribe to session events; returns an unsubscribe function. */
	addEventListener(listener: (event: JsonRecord) => void): () => void {
		this.eventListeners.add(listener);
		return () => {
			this.eventListeners.delete(listener);
		};
	}

	/**
	 * Send a command and wait for its `response` record.
	 * Rejects when success:false (with the protocol error string), on timeout,
	 * or when the process exits before answering.
	 */
	request(command: JsonRecord, timeoutMs?: number): Promise<RpcResponse> {
		const id = (command.id as string | undefined) ?? `req-${this.nextRequestId++}`;
		const wire = { ...command, id };
		const effective = timeoutMs ?? this.opts.requestTimeoutMs ?? 60_000;
		return new Promise<RpcResponse>((resolve, reject) => {
			const timer = setTimeout(() => {
				this.pending.delete(id);
				reject(
					new Error(
						`Timed out waiting for pi RPC response to ${String(command["type"])}. ` +
							`Stderr: ${this.stderrTail}`,
					),
				);
			}, effective);
			this.pending.set(id, { resolve, reject, timer, command: String(command["type"]) });
			this.writeJson(wire);
		});
	}

	/** Write an arbitrary protocol record (e.g. extension_ui_response). */
	sendRecord(record: JsonRecord): void {
		this.writeJson(record);
	}

	/** Answer an extension UI dialog (the raw extension_ui_response payload). */
	respondUi(payload: JsonRecord): void {
		this.writeJson(payload);
	}

	/**
	 * Orderly shutdown: closing stdin tells pi to dispose its runtime and exit.
	 * If it does not exit quickly, SIGTERM then SIGKILL. Resolves immediately
	 * when the process is already gone (no fresh `close` will ever fire).
	 */
	async stop(graceMs = 5000): Promise<void> {
		this.stopRequested = true;
		const child = this.child;
		if (!child) return;
		if (child.exitCode !== null || this.exited) return; // already gone
		await new Promise<void>((resolve) => {
			const done = () => resolve();
			child.once("close", done);
			try {
				child.stdin.end();
			} catch {
				/* already gone */
			}
			setTimeout(() => {
				if (child.exitCode === null) child.kill("SIGTERM");
				resolve();
			}, graceMs).unref();
			setTimeout(() => {
				if (child.exitCode === null) child.kill("SIGKILL");
				resolve();
			}, graceMs * 2).unref();
		});
	}

	/** Command wrappers for the common pi RPC commands (rpc-commands.md). */
	getState(timeoutMs?: number): Promise<Record<string, unknown>> {
		return this.request({ type: "get_state" }, timeoutMs).then((r) => (r.data ?? {}) as Record<string, unknown>);
	}

	getMessages(timeoutMs?: number): Promise<unknown[]> {
		return this.request({ type: "get_messages" }, timeoutMs).then((r) => ((r.data as { messages?: unknown[] }) ?? {}).messages ?? []);
	}

	// ------------------------------------------------------------------
	// internals
	// ------------------------------------------------------------------

	private writeJson(record: JsonRecord): void {
		const child = this.child;
		if (!child || child.exitCode !== null) {
			throw new RpcClosedError(`pi RPC process not writable. Stderr: ${this._stderrTail}`);
		}
		child.stdin.write(JSON.stringify(record) + "\n");
	}

	private read(buf: Buffer): void {
		// Strict LF framing on the decoded UTF-8 stream (docs: split on LF
		// only; strip an optional preceding carriage return). StringDecoder
		// keeps multi-byte sequences intact across chunk boundaries.
		this.lineBuf += this.decoder.write(buf);
		let index: number;
		while ((index = this.lineBuf.indexOf("\n")) !== -1) {
			const raw = this.lineBuf.slice(0, index);
			this.lineBuf = this.lineBuf.slice(index + 1);
			const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
			if (line.trim()) this.handleLine(line);
		}
	}

	private handleLine(line: string): void {
		let data: JsonRecord;
		try {
			data = JSON.parse(line) as JsonRecord;
		} catch {
			return; // non-JSON stdout noise — ignore (pi keeps stdout protocol-clean)
		}
		if (this.dialect.supportsChunkReassembly && data["type"] === "rpc_chunk") {
			this.handleChunk(data);
			return;
		}
		this.dispatch(data);
	}

	/** Route one protocol record (parsed line or reassembled frame). */
	private dispatch(data: JsonRecord): void {
		if (data["type"] === "ready") {
			this._readyFrame = data;
			const waiter = this.readyWaiter;
			this.readyWaiter = null;
			if (waiter) {
				waiter.resolve();
				void this.afterReady();
			}
		}
		if (data["type"] === "response" && typeof data["id"] === "string") {
			const pending = this.pending.get(data["id"]);
			if (pending) {
				this.pending.delete(data["id"]);
				clearTimeout(pending.timer);
				if (data["success"] === false) {
					pending.reject(
						new Error(
							`pi RPC command ${data["command"] ?? pending.command} failed: ` +
								`${String(data["error"] ?? "unknown error")}`,
						),
					);
				} else {
					pending.resolve(data as unknown as RpcResponse);
				}
				return;
			}
			// Response without a matching pending request: fall through to the
			// event listeners so nothing gets lost.
		}
		if (data["type"] === "extension_ui_request") {
			// DUAL DELIVERY (verified live, P1 spike): pi delivers extension
			// UI records as a separate RPC subprotocol — rpc-mode does NOT
			// emit them as AgentSessionEvents. Dialogs must BOTH reach the
			// dedicated onUiRequest host (optional policy) AND the event
			// listeners, which is how the sidecar's SSE run window forwards
			// them to the A2A bridge for approval handling.
			this.opts.onUiRequest?.(data as unknown as UiRequest);
			for (const listener of [...this.eventListeners]) {
				try {
					listener(data);
				} catch (err) {
					// A listener must never stall the stdout pump.
					this.opts.onStderr?.(`[listener error] ${err}
`);
				}
			}
			return;
		}
		// Session event: constructor listener first, then run-window listeners.
		this.opts.onEvent?.(data);
		for (const listener of [...this.eventListeners]) {
			try {
				listener(data);
			} catch (err) {
				// A listener must never stall the stdout pump.
				this.opts.onStderr?.(`[listener error] ${err}
`);
			}
		}
	}

	/**
	 * Commands sent right after the `ready` frame: the protocol v2
	 * negotiation (omp splits oversized frames into `rpc_chunk` sequences
	 * only in v2) and the subagent subscription (the sidecar does not render
	 * subagent frames).
	 */
	private async afterReady(): Promise<void> {
		if (this.dialect.negotiatesProtocolV2) {
			try {
				const response = await this.request(
					{ id: "protocol-2", type: "negotiate_protocol", protocolVersion: 2 },
					10_000,
				);
				if (response.success === false) {
					this.opts.onStderr?.(
						`[${this.dialect.name}] protocol v2 negotiation refused ` +
							`(${String(response.error ?? "unknown error")}); continuing on v1\n`,
					);
				}
			} catch (err) {
				this.opts.onStderr?.(
					`[${this.dialect.name}] protocol v2 negotiation failed: ${err}\n`,
				);
			}
			try {
				await this.request({ type: "set_subagent_subscription", level: "off" }, 10_000);
			} catch {
				/* optional command — older agents answer "unknown command" */
			}
		}
	}

	/**
	 * Reassemble a `rpc_chunk` sequence (protocol v2). The chunks of one
	 * sequence arrive uninterrupted and in order; a gap, an interleaved
	 * sequence or a frame above the advertised limit discards the sequence.
	 */
	private handleChunk(record: JsonRecord): void {
		const chunkId = typeof record["chunkId"] === "string" ? record["chunkId"] : "";
		const index = Number(record["index"]);
		const count = Number(record["count"]);
		const byteLength = Number(record["byteLength"]);
		const segment =
			typeof record["data"] === "string" ? Buffer.from(record["data"], "base64") : null;
		if (
			!chunkId ||
			segment === null ||
			!Number.isInteger(index) ||
			!Number.isInteger(count) ||
			count <= 0 ||
			index < 0 ||
			index >= count
		) {
			this.chunkSequence = null;
			this.emitFrameError("malformed rpc_chunk frame");
			return;
		}
		if (!this.chunkSequence || this.chunkSequence.chunkId !== chunkId) {
			// A new sequence interrupts the previous one.
			this.discardChunkSequence("interrupted rpc_chunk sequence");
			this.chunkSequence = { chunkId, count, byteLength, segments: new Map(), bytes: 0 };
		}
		const sequence = this.chunkSequence;
		if (sequence.count !== count) {
			this.discardChunkSequence("rpc_chunk sequence changed its chunk count");
			return;
		}
		if (index !== sequence.segments.size) {
			this.discardChunkSequence("rpc_chunk sequence has a gap");
			return;
		}
		sequence.segments.set(index, segment);
		sequence.bytes += segment.length;
		const limit = this.maxReassembledFrameBytes;
		if (sequence.bytes > limit || (byteLength > 0 && byteLength > limit)) {
			this.discardChunkSequence("frame exceeded maxReassembledFrameBytes");
			return;
		}
		if (sequence.segments.size < sequence.count) return;

		const parts = [...sequence.segments.entries()]
			.sort((a, b) => a[0] - b[0])
			.map(([, buffer]) => buffer);
		const total = Buffer.concat(parts);
		this.chunkSequence = null;
		const declared = sequence.byteLength;
		if (
			declared > 0 &&
			total.length !== declared &&
			!parts.some((part) => part.length === declared)
		) {
			this.emitFrameError("reassembled frame length mismatch");
			return;
		}
		let text: string;
		try {
			text = new TextDecoder("utf-8", { fatal: true }).decode(total);
		} catch {
			this.emitFrameError("reassembled frame is not valid UTF-8");
			return;
		}
		let parsed: JsonRecord;
		try {
			parsed = JSON.parse(text) as JsonRecord;
		} catch {
			this.emitFrameError("reassembled frame is not valid JSON");
			return;
		}
		this.dispatch(parsed);
	}

	/** Drop the pending chunk sequence and report why. */
	private discardChunkSequence(reason: string): void {
		const had = this.chunkSequence !== null;
		this.chunkSequence = null;
		if (had) this.emitFrameError(reason);
	}

	/** Surface a transport-level frame error as a session event. */
	private emitFrameError(error: string): void {
		this.dispatch({ type: "rpc_frame_error", error });
	}

	/** Reject the pending `ready` handshake (process died / timed out). */
	private failReady(error: Error): void {
		const waiter = this.readyWaiter;
		this.readyWaiter = null;
		if (waiter) waiter.reject(error);
	}

	private failAllPending(error: Error): void {
		for (const pending of this.pending.values()) {
			clearTimeout(pending.timer);
			pending.reject(error);
		}
		this.pending.clear();
	}
}