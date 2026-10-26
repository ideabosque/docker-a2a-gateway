/**
 * server.ts — HTTP + SSE boundary of the pi sidecar.
 *
 * Endpoints (all require the shared secret as the X-Secret-Key header; 401
 * otherwise — the same pattern as the bundled goose sibling):
 *
 *   GET    /health                       → liveness + pi version
 *   POST   /sessions                     → new/reopened session
 *   GET    /sessions/{id}                → state + stats (also the resume probe)
 *   DELETE /sessions/{id}                → close the live process
 *   POST   /sessions/{id}/prompt         → SSE stream of pi events + run_end
 *   POST   /sessions/{id}/abort          → abort the running prompt
 *   POST   /sessions/{id}/ui-response    → answer an extension UI dialog
 *
 * The prompt SSE stream forwards pi events verbatim (`data: <pi event JSON>`)
 * and terminates with a final `run_end` frame describing the run before
 * closing. One run per session: a prompt while streaming is rejected with 409
 * unless the body carries `streamingBehavior` ("steer" | "followUp"),
 * forwarded to pi (docs/rpc-commands.md#prompt).
 *
 * On client disconnect the run is NOT aborted — the gateway cancels via
 * POST /abort explicitly; HTTP-fallback clients may disconnect early and
 * re-attach through the gateway later.
 */

import http from "node:http";

import { type JsonRecord, PiRpc, RpcClosedError } from "./rpc.js";
import {
	type CreateOptions,
	PoolError,
	SessionPool,
	type PoolSession,
} from "./pool.js";

export interface ServerOptions {
	port: number;
	secretKey: string;
	pool: SessionPool;
	piVersion: string;
	/** Stream timeout for one prompt run (ms), 0 disables. */
	streamTimeoutMs: number;
	/** Max request body size (bytes) — base64 images ride the prompt body. */
	maxBodyBytes: number;
	log: (level: "info" | "warn" | "error", message: string) => void;
}

/** Grace after agent_settled before declaring the run end (ms). */
const SETTLE_GRACE_MS = 250;
/** Max time one "settled → check state → maybe keep waiting" cycle loops. */
const MAX_SETTLE_POLLS = 20;

export function createServer(opts: ServerOptions): http.Server {
	const json = (res: http.ServerResponse, status: number, body: unknown): void => {
		res.writeHead(status, { "content-type": "application/json" });
		res.end(JSON.stringify(body));
	};

	const readBody = (req: http.IncomingMessage): Promise<string> =>
		new Promise((resolve, reject) => {
			let size = 0;
			const chunks: Buffer[] = [];
			req.on("data", (chunk: Buffer) => {
				size += chunk.length;
				if (size > opts.maxBodyBytes) {
					reject(new PoolError("request body too large", 413));
					req.destroy();
					return;
				}
				chunks.push(chunk);
			});
			req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
			req.on("error", reject);
		});

	const parseJsonBody = async (req: http.IncomingMessage): Promise<Record<string, unknown>> => {
		const raw = await readBody(req);
		if (!raw.trim()) return {};
		try {
			return JSON.parse(raw) as Record<string, unknown>;
		} catch {
			throw new PoolError("invalid JSON body", 400);
		}
	};

	const safeAbort = async (rpc: PiRpc): Promise<void> => {
		try {
			await rpc.request({ type: "abort" }, 15_000);
		} catch {
			/* best effort */
		}
	};

	/**
	 * Drive one run: subscribe events, send the prompt, stream SSE until the
	 * session settles (and any queued streamingBehavior work finishes), then
	 * emit run_end. The run_end `usage` carries pi's last message_update
	 * usage (docs/json.md — cumulative provider usage for the response).
	 */
	const streamRun = async (
		rpc: PiRpc,
		session: PoolSession,
		body: Record<string, unknown>,
		res: http.ServerResponse,
	): Promise<void> => {
		const message = String(body["message"] ?? "");
		if (!message.trim()) {
			json(res, 400, { error: "message is required" });
			return;
		}
		const streamingBehaviorRaw = body["streamingBehavior"];
		const streamingBehavior =
			streamingBehaviorRaw === "steer" || streamingBehaviorRaw === "followUp"
				? (streamingBehaviorRaw as "steer" | "followUp")
				: undefined;
		const images = Array.isArray(body["images"]) ? body["images"] : undefined;

		res.writeHead(200, {
			"content-type": "text/event-stream",
			"cache-control": "no-cache",
			connection: "keep-alive",
		});
		res.write(
			`data: ${JSON.stringify({
				type: "session",
				sessionId: session.id,
				sessionFile: session.sessionFile,
			})}\n\n`,
		);

		// Writes to a disconnected socket are simply dropped.
		let closed = false;
		res.on("close", () => {
			closed = true;
		});
		const send = (event: JsonRecord): void => {
			if (closed) return;
			try {
				res.write(`data: ${JSON.stringify(event)}\n\n`);
			} catch {
				/* socket gone */
			}
		};

		let lastUsage: unknown = undefined;
		// Capture settlement before sending the command: pi may emit the
		// response and all run events in one stdout read, before request()
		// resumes and waitForSettle installs its waiter.
		const settledEvents: JsonRecord[] = [];
		const usageUnsubscribe = rpc.addEventListener((event) => {
			if (event["type"] === "agent_settled") settledEvents.push(event);
			if (event["type"] === "message_update" && event["usage"] !== undefined) {
				lastUsage = event["usage"];
			}
			send(event);
		});

		const deadline = opts.streamTimeoutMs > 0 ? Date.now() + opts.streamTimeoutMs : 0;

		const waitForSettle = async (): Promise<{ aborted: boolean; timedOut: boolean }> => {
			for (let poll = 0; poll < MAX_SETTLE_POLLS; poll++) {
				if (deadline !== 0 && Date.now() > deadline) {
					await safeAbort(rpc);
					return { aborted: true, timedOut: true };
				}
				// Wait for agent_settled, racing the run deadline.
				const settled = await new Promise<{ aborted: boolean; timedOut: boolean }>((resolve) => {
					const queued = settledEvents.shift();
					if (queued) {
						resolve({ aborted: queued["aborted"] === true, timedOut: false });
						return;
					}
					let done = false;
					const finish = (
						result: { aborted: boolean; timedOut: boolean },
					): void => {
						if (done) return;
						done = true;
						unsubscribeSettled();
						resolve(result);
					};
					const unsubscribeSettled = rpc.addEventListener((event) => {
						if (event["type"] === "agent_settled") {
							settledEvents.shift();
							finish({ aborted: event["aborted"] === true, timedOut: false });
						}
					});
					const checkDeadline = (): void => {
						if (done) return;
						if (deadline !== 0 && Date.now() > deadline) {
							finish({ aborted: true, timedOut: true });
							return;
						}
						setTimeout(checkDeadline, 1000);
					};
					setTimeout(checkDeadline, 1000);
				});
				if (settled.timedOut) {
					await safeAbort(rpc);
					return { aborted: true, timedOut: true };
				}
				if (settled.aborted) return { aborted: true, timedOut: false };
				// Queued steering/follow-up work may still follow this settle;
				// poll the state (docs: settled = no *automatic* continuation).
				await new Promise((r) => setTimeout(r, SETTLE_GRACE_MS));
				const state = await rpc
					.getState()
					.catch(() => ({}) as Record<string, unknown>);
				const stillGoing =
					state["isStreaming"] === true ||
					Number(state["pendingMessageCount"] ?? 0) > 0;
				if (!stillGoing) return { aborted: false, timedOut: false };
			}
			return { aborted: false, timedOut: false };
		};

		const runEnd: Record<string, unknown> = {
			type: "run_end",
			sessionId: session.id,
			sessionFile: session.sessionFile,
			aborted: false,
		};
		try {
			const response = await rpc.request(
				{
					type: "prompt",
					message,
					...(images ? { images } : {}),
					...(streamingBehavior ? { streamingBehavior } : {}),
				},
				60_000,
			);
			const data = (response.data ?? {}) as Record<string, unknown>;
			const disposition = String(data["disposition"] ?? "started");
			if (disposition === "handled") {
				// No run started (extension command / input handler): nothing to
				// wait for — no agent_settled follows (docs/rpc.md).
				runEnd["stopReason"] = "handled";
			} else {
				const settled = await waitForSettle();
				runEnd["aborted"] = settled.aborted;
				runEnd["stopReason"] = settled.timedOut
					? "timeout"
					: settled.aborted
						? "aborted"
						: "end_turn";
			}
			runEnd["usage"] = lastUsage ?? null;
		} catch (err) {
			runEnd["aborted"] = false;
			runEnd["error"] = err instanceof Error ? err.message : String(err);
		} finally {
			usageUnsubscribe();
			send(runEnd);
			res.end();
		}
	};

	const server = http.createServer(async (req, res) => {
		const url = new URL(req.url ?? "/", "http://localhost");
		const path = url.pathname.replace(/\/+$/, "") || "/";

		// ── auth ──────────────────────────────────────────────────────────
		if (req.headers["x-secret-key"] !== opts.secretKey) {
			json(res, 401, { error: "invalid or missing X-Secret-Key" });
			return;
		}

		try {
			// ── GET /health ────────────────────────────────────────────────
			if (req.method === "GET" && path === "/health") {
				json(res, 200, {
					status: "ok",
					pi: opts.piVersion,
					sessions: { active: opts.pool.activeCount },
				});
				return;
			}

			// ── POST /sessions — create (or re-open) a session ────────────
			if (req.method === "POST" && path === "/sessions") {
				const body = await parseJsonBody(req);
				const createOpts: CreateOptions = {};
				if (typeof body["cwd"] === "string") createOpts.cwd = body["cwd"];
				if (typeof body["model"] === "string") createOpts.model = body["model"];
				if (typeof body["thinking"] === "string")
					createOpts.thinking = body["thinking"];
				const session = await opts.pool.create(
					createOpts,
					typeof body["sessionFile"] === "string"
						? (body["sessionFile"] as string)
						: undefined,
				);
				json(res, 200, {
					sessionId: session.id,
					sessionFile: session.sessionFile,
					resumed: typeof body["sessionFile"] === "string",
				});
				return;
			}

			const sessionMatch = /^\/sessions\/([^/]+)(?:\/(prompt|abort|ui-response))?$/.exec(
				path,
			);
			if (sessionMatch) {
				const id = decodeURIComponent(sessionMatch[1]);
				const action = sessionMatch[2];

				// ── GET /sessions/{id} — state + stats (resume probe) ───────
				if (req.method === "GET" && !action) {
					let session = opts.pool.get(id);
					if (!session) {
						// Not live: try the persisted index (sidecar restart).
						session = await opts.pool.getOrResume(id).catch(() => undefined);
					}
					if (!session) {
						json(res, 404, { error: `unknown session ${id}` });
						return;
					}
					const state = await opts.pool.getState(id);
					const stats = await opts.pool.getStats(id).catch(() => ({}));
					json(res, 200, { state, stats, streaming: session.streaming });
					return;
				}

				// ── DELETE /sessions/{id} ─────────────────────────────────────
				if (req.method === "DELETE" && !action) {
					if (!opts.pool.get(id)) {
						json(res, 404, { error: `unknown session ${id}` });
						return;
					}
					await opts.pool.close(id);
					json(res, 200, { closed: true });
					return;
				}

				if (!action) {
					json(res, 405, { error: "method not allowed" });
					return;
				}

				const session = opts.pool.get(id);
				if (!session) {
					json(res, 404, { error: `unknown session ${id}` });
					return;
				}

				// ── POST /sessions/{id}/prompt — one SSE run window ─────────
				if (action === "prompt" && req.method === "POST") {
					const body = await parseJsonBody(req);
					if (session.streaming && !body["streamingBehavior"]) {
						json(res, 409, {
							error:
								`session ${id} is already streaming; send ` +
								`streamingBehavior "steer" or "followUp"`,
						});
						return;
					}
					if (session.streaming) {
						// steering / followUp during an active run: pass through
						// WITHOUT starting a second run window.
						const behavior =
							body["streamingBehavior"] === "steer" ? "steer" : "followUp";
						const message = String(body["message"] ?? "");
						if (!message.trim()) {
							json(res, 400, { error: "message is required" });
							return;
						}
						await session.rpc.request(
							{
								...(behavior === "steer"
									? { type: "steer" }
									: { type: "follow_up" }),
								message,
							},
							60_000,
						);
						json(res, 200, { disposition: behavior });
						return;
					}
					session.streaming = true;
					session.lastUsed = Date.now();
					try {
						await streamRun(session.rpc, session, body, res);
					} finally {
						session.streaming = false;
						session.lastUsed = Date.now();
					}
					return;
				}

				// ── POST /sessions/{id}/abort ────────────────────────────────
				if (action === "abort" && req.method === "POST") {
					await opts.pool.abort(id);
					json(res, 200, { aborted: true, sessionId: id });
					return;
				}

				// ── POST /sessions/{id}/ui-response — answer a dialog ───────
				if (action === "ui-response" && req.method === "POST") {
					const payload = (await parseJsonBody(req)) as JsonRecord;
					if (typeof payload["id"] !== "string") {
						json(res, 400, { error: "ui-response payload needs an id" });
						return;
					}
					session.rpc.respondUi({
						type: "extension_ui_response",
						...payload,
					});
					json(res, 200, { ok: true });
					return;
				}
			}

			json(res, 404, { error: `no route ${req.method} ${path}` });
		} catch (err) {
			if (err instanceof PoolError) {
				json(res, err.status, { error: err.message });
				return;
			}
			if (err instanceof RpcClosedError) {
				json(res, 502, { error: err.message });
				return;
			}
			opts.log("error", `request failed: ${err instanceof Error ? err.stack : err}`);
			if (!res.headersSent) {
				json(res, 500, {
					error: err instanceof Error ? err.message : "internal error",
				});
			} else {
				res.end();
			}
		}
	});

	return server;
}
