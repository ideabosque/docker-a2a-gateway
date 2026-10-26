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

export class PiRpc {
	readonly connectionId = `a2a-${randomUUID()}`;
	private readonly opts: RpcOptions;
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

	constructor(opts: RpcOptions) {
		this.opts = opts;
	}

	/** Whether the child is running. */
	get alive(): boolean {
		return this.child !== null && this.child.exitCode === null && !this.exited;
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
				this.opts.onExit?.(code, signal, err ?? undefined);
				if (!this.stopRequested) {
					reject(err ?? new RpcClosedError("pi RPC process exited"));
					return;
				}
			});

			// The spawn itself succeeds unless the executable is missing; those
			// failures surface via "error"/"close" above.
			resolve();
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
			this.opts.onUiRequest?.(data as unknown as UiRequest);
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

	private failAllPending(error: Error): void {
		for (const pending of this.pending.values()) {
			clearTimeout(pending.timer);
			pending.reject(error);
		}
		this.pending.clear();
	}
}