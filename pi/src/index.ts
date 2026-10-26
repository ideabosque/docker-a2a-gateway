/**
 * index.ts — pi sidecar entry point.
 *
 * Environment (all optional; see pi/README.md and .env.example in
 * docker-a2a-gateway):
 *   PI_PORT                 HTTP port (default 3300)
 *   PI_SIDECAR_SECRET_KEY   shared secret (X-Secret-Key). If empty at first
 *                           start one is generated, printed once, and saved
 *                           to $PI_DATA_DIR/server-secret.txt (like goose).
 *   PI_DATA_DIR             sidecar state dir (default /data)
 *   PI_SESSION_DIR          pi --session-dir (default $PI_DATA_DIR/sessions)
 *   PI_CWD                  default agent cwd (default /workspace)
 *   PI_MODEL                provider/modelId default (e.g. anthropic/...)
 *   PI_THINKING             default thinking level (off … max)
 *   PI_PROVIDER             explicit provider (overrides PI_MODEL prefix)
 *   PI_MODEL_ID             explicit model id (overrides PI_MODEL suffix)
 *   PI_TOOLS                comma list passed as pi --tools
 *   PI_EXTRA_ARGS           extra pi CLI flags (space-split, unquoted)
 *   PI_APPROVE              1 → allow project-trust gating (default 0 = --no-approve)
 *   PI_OFFLINE              1 → --offline
 *   PI_MAX_PROCESSES        live process cap (default 8)
 *   PI_IDLE_TIMEOUT_S       idle process reaping (default 1800; 0 disables)
 *   PI_STREAM_TIMEOUT_S     per-run stream timeout (default 300; 0 disables)
 *   PI_MAX_BODY_BYTES       request body cap (default 262144000 ≈ 250 MB)
 */

import { execFile } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

import { createServer } from "./server.js";
import { SessionPool, parseModelSpec } from "./pool.js";

const execFileAsync = promisify(execFile);

const log = (level: "info" | "warn" | "error", message: string): void => {
	const line = `[pi-sidecar] ${message}`;
	if (level === "error") console.error(line);
	else console.log(line);
};

const intEnv = (name: string, fallback: number): number => {
	const raw = process.env[name];
	if (!raw || !raw.trim()) return fallback;
	try {
		return Number.parseInt(raw, 10);
	} catch {
		return fallback;
	}
};

const boolEnv = (name: string, fallback = false): boolean => {
	const raw = process.env[name];
	if (raw === undefined || raw === "") return fallback;
	const v = raw.toLowerCase();
	return v === "1" || v === "true" || v === "yes" || v === "on";
};

const DATA_DIR = process.env.PI_DATA_DIR || "/data";
const SESSION_DIR = process.env.PI_SESSION_DIR || path.join(DATA_DIR, "sessions");
const WORKSPACE = process.env.PI_CWD || "/workspace";
const PORT = intEnv("PI_PORT", 3300);
const MAX_PROCESSES = intEnv("PI_MAX_PROCESSES", 8);
const IDLE_TIMEOUT_MS = intEnv("PI_IDLE_TIMEOUT_S", 1800) * 1000;
const STREAM_TIMEOUT_MS = intEnv("PI_STREAM_TIMEOUT_S", 300) * 1000;
const MAX_BODY_BYTES = intEnv("PI_MAX_BODY_BYTES", 262_144_000);

const piVersion = async (): Promise<string> => {
	try {
		const bin = process.env.PI_BIN || "pi";
		const { stdout } = await execFileAsync(bin, ["--version"], { timeout: 15_000 });
		return stdout.trim() || "unknown";
	} catch (err) {
		log(
			"warn",
			`could not read pi --version (PI_BIN=${process.env.PI_BIN || "pi"}): ${err}`,
		);
		return "unknown";
	}
};

const resolveSecret = async (): Promise<string> => {
	const fromEnv = (process.env.PI_SIDECAR_SECRET_KEY ?? "").trim();
	const secretFile = path.join(DATA_DIR, "server-secret.txt");
	if (fromEnv) {
		if (!existsSync(secretFile)) {
			await writeFile(secretFile, fromEnv + "\n", { mode: 0o600 });
		}
		return fromEnv;
	}
	if (existsSync(secretFile)) {
		const saved = (await readFile(secretFile, "utf-8")).trim();
		if (saved) {
			log("info", `sidecar secret recovered from ${secretFile}`);
			return saved;
		}
	}
	const secret = (await import("node:crypto")).randomBytes(32).toString("hex");
	await writeFile(secretFile, secret + "\n", { mode: 0o600 });
	log("info", "generated a new sidecar secret (first start)");
	log(
		"info",
		`copy it into .env as PI_SIDECAR_SECRET_KEY (and PI_SECRET_KEY) — ` +
			`recovery: docker exec <pi-container> cat ${secretFile}`,
	);
	return secret;
};

// Base pi command line shared by every spawned session. Provider credentials
// are inherited from the environment (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...);
// model selection rides PI_MODEL / PI_PROVIDER / PI_MODEL_ID.
const buildBaseArgs = (): string[] => {
	const args = ["--mode", "rpc"];
	const { provider, model } = parseModelSpec(process.env.PI_MODEL);
	const providerOverride = process.env.PI_PROVIDER || provider;
	const modelOverride = process.env.PI_MODEL_ID || model;
	if (providerOverride) args.push("--provider", providerOverride);
	if (modelOverride) args.push("--model", modelOverride);
	if (process.env.PI_THINKING) args.push("--thinking", process.env.PI_THINKING);
	if (process.env.PI_TOOLS) args.push("--tools", process.env.PI_TOOLS);
	if (!boolEnv("PI_APPROVE")) args.push("--no-approve");
	if (boolEnv("PI_OFFLINE")) args.push("--offline");
	if (boolEnv("PI_NO_EXTENSIONS", false)) args.push("--no-extensions");
	const extra = (process.env.PI_EXTRA_ARGS ?? "").trim();
	if (extra) {
		// Unquoted, shell-style word splitting (same contract as
		// EXTRA_GOOSE_SERVE_ARGS in the goose sibling).
		args.push(...extra.split(/\s+/).filter(Boolean));
	}
	return args;
};

const main = async (): Promise<void> => {
	mkdirSync(SESSION_DIR, { recursive: true });
	const secret = await resolveSecret();
	const version = await piVersion();
	log("info", `pi version: ${version}`);
	log("info", `session dir: ${SESSION_DIR}`);

	const pool = new SessionPool({
		baseArgs: buildBaseArgs(),
		dataDir: DATA_DIR,
		sessionDir: SESSION_DIR,
		maxProcesses: MAX_PROCESSES,
		idleTimeoutMs: IDLE_TIMEOUT_MS,
		defaultCwd: WORKSPACE,
		logger: log,
		onEvent: (_sessionId, event) => {
			// Run-window events are subscribed per HTTP stream; events outside
			// any window (e.g. session_info_changed) are logged at debug noise.
			if (process.env.PI_LOG_EVENTS === "1") {
				log("info", `event: ${JSON.stringify(event).slice(0, 200)}`);
			}
		},
		onStderr: (sessionId, line) => {
			if (line.trim()) log("info", `pi[${sessionId}] stderr: ${line.trimEnd()}`);
		},
		onExit: (sessionId, code, signal) => {
			log(
				"warn",
				`pi process for ${sessionId} exited (code=${code} signal=${signal})`,
			);
		},
	});

	const server = createServer({
		port: PORT,
		secretKey: secret,
		pool,
		piVersion: version,
		streamTimeoutMs: STREAM_TIMEOUT_MS,
		maxBodyBytes: MAX_BODY_BYTES,
		log,
	});

	server.listen(PORT, "0.0.0.0", () => {
		log("info", `pi sidecar listening on 0.0.0.0:${PORT}`);
	});

	const shutdown = async (): Promise<void> => {
		log("info", "shutting down");
		server.close();
		await pool.closeAll();
		process.exit(0);
	};
	process.on("SIGTERM", () => void shutdown());
	process.on("SIGINT", () => void shutdown());
};

main().catch((err) => {
	log("error", `fatal: ${err instanceof Error ? err.stack : err}`);
	process.exit(1);
});