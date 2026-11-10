/**
 * index.ts — sidecar entry point (pi or omp dialect).
 *
 * The sidecar drives one agent dialect, selected with AGENT_DIALECT ("pi" by
 * default, "omp" for oh-my-pi). Every setting below is read from the
 * dialect's env namespace — PI_* for pi, OMP_* for omp — so the two
 * siblings share one implementation and one env contract:
 *
 *   ${P}_PORT                 HTTP port (pi 3300 / omp 3310)
 *   ${P}_SIDECAR_SECRET_KEY   shared secret (X-Secret-Key). If empty at first
 *                             start one is generated, printed once, and saved
 *                             to $${P}_DATA_DIR/server-secret.txt (like goose).
 *   ${P}_DATA_DIR             sidecar state dir (default /data)
 *   ${P}_SESSION_DIR          agent --session-dir (default $${P}_DATA_DIR/sessions)
 *   ${P}_CWD                  default agent cwd (default /workspace)
 *   ${P}_SIDE_MODEL           default model (pi: provider/modelId, omp: the
 *                             whole spec — provider/modelId or a role)
 *   ${P}_SIDE_THINKING        default thinking level (off … max)
 *   ${P}_SIDE_TOOLS           comma list passed as --tools
 *   ${P}_SIDE_EXTRA_ARGS      extra agent CLI flags (space-split, unquoted)
 *   ${P}_LOG_EVENTS           1 → log every session event
 *   ${P}_MAX_PROCESSES        live process cap (default 8)
 *   ${P}_IDLE_TIMEOUT_S       idle process reaping (default 1800; 0 disables)
 *   ${P}_STREAM_TIMEOUT_S     per-run stream timeout (default 300; 0 disables)
 *   ${P}_MAX_BODY_BYTES       request body cap (default 262144000 ≈ 250 MB)
 *   ${P}_BIN                  agent binary (default: the dialect's own)
 *
 * pi-only (unchanged): ${P}_SIDE_PROVIDER, ${P}_SIDE_MODEL_ID,
 * ${P}_SIDE_APPROVE, ${P}_SIDE_OFFLINE, ${P}_SIDE_NO_EXTENSIONS.
 * omp-only: ${P}_SIDE_APPROVAL_MODE (default always-ask), ${P}_SIDE_NO_LSP
 * (default 1 → --no-lsp), ${P}_SIDE_MAX_COST (optional → --max-cost <usd>).
 *
 * AGENT_BIN overrides the agent binary for both dialects; AGENT_DIALECT
 * selects the dialect and fails fast on an unknown value.
 *
 * P1 SPIKE FINDING (2026-10-25): pi itself EXPORTS `PI_PROVIDER` / `PI_MODEL`
 * into the shells of its own tool runs (pi docs environment-variables.md) —
 * and on dev machines those very names are often already in the environment.
 * The sidecar's spawn defaults therefore use the PI_SIDE_* namespace, and the
 * pi child env strips PI_MODEL/PI_PROVIDER/PI_THINKING/PI_TOOLS entirely (see
 * pool.spawnEnvClean) so a host value can never masquerade as sidecar config
 * (verified: host PI_PROVIDER=ollama-cloud made resumed sessions boot-fail
 * with `Unknown provider "ollama-cloud"`).
 */

import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

import { createDialect, type AgentDialect } from "./dialect.js";
import { createServer } from "./server.js";
import { SessionPool, parseModelSpec } from "./pool.js";

const execFileAsync = promisify(execFile);

// A misconfigured sidecar must fail at startup, never speak the wrong
// protocol.
const dialect: AgentDialect = (() => {
	const name = (process.env.AGENT_DIALECT ?? "pi").trim() || "pi";
	try {
		return createDialect(name);
	} catch (err) {
		console.error(
			`[sidecar] fatal: ${err instanceof Error ? err.message : String(err)}`,
		);
		process.exit(1);
	}
})();

/** Env namespace of the running dialect: PI_* or OMP_*. */
const P = dialect.envPrefix;

const log = (level: "info" | "warn" | "error", message: string): void => {
	const line = `[${dialect.name}-sidecar] ${message}`;
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

/** Read a setting from the dialect's env namespace. */
const env = (name: string): string | undefined => process.env[`${P}_${name}`];

const DATA_DIR = env("DATA_DIR") || "/data";
const SESSION_DIR = env("SESSION_DIR") || path.join(DATA_DIR, "sessions");
const WORKSPACE = env("CWD") || "/workspace";
const PORT = intEnv(`${P}_PORT`, dialect.defaultPort);
const MAX_PROCESSES = intEnv(`${P}_MAX_PROCESSES`, 8);
const IDLE_TIMEOUT_MS = intEnv(`${P}_IDLE_TIMEOUT_S`, 1800) * 1000;
const STREAM_TIMEOUT_MS = intEnv(`${P}_STREAM_TIMEOUT_S`, 300) * 1000;
const MAX_BODY_BYTES = intEnv(`${P}_MAX_BODY_BYTES`, 262_144_000);

// The agent binary: AGENT_BIN wins over the dialect's own name.
const BIN = process.env.AGENT_BIN || env("BIN") || dialect.defaultBin;

const agentVersion = async (): Promise<string> => {
	try {
		const { stdout } = await execFileAsync(BIN, ["--version"], { timeout: 15_000 });
		return stdout.trim() || "unknown";
	} catch (err) {
		log("warn", `could not read ${BIN} --version: ${err}`);
		return "unknown";
	}
};

const resolveSecret = async (): Promise<string> => {
	const fromEnv = (env("SIDECAR_SECRET_KEY") ?? "").trim();
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
	const secret = randomBytes(32).toString("hex");
	await writeFile(secretFile, secret + "\n", { mode: 0o600 });
	log("info", "generated a new sidecar secret (first start)");
	log(
		"info",
		`copy it into .env as ${P}_SIDECAR_SECRET_KEY (and ${P}_SECRET_KEY) — ` +
			`recovery: docker exec <${dialect.name}-container> cat ${secretFile}`,
	);
	return secret;
};

// Base agent command line shared by every spawned session. Provider
// credentials are inherited from the environment (ANTHROPIC_API_KEY,
// OPENAI_API_KEY, ...); model selection uses the ${P}_SIDE_* namespace.
//
// The per-session flags (model / thinking) are added by the pool from the
// dialect's spawnFlags(); the omp defaults ride there instead of here, so a
// per-session model never duplicates the flag.
const buildBaseArgs = (): string[] => {
	// The agent binary must ride in position 0 — PiRpc spawns command[0] with
	// the tail as arguments.
	const args = [BIN, "--mode", "rpc"];
	if (dialect.name === "omp") {
		const tools = (env("SIDE_TOOLS") ?? "").trim();
		if (tools) args.push("--tools", tools);
		args.push("--approval-mode", (env("SIDE_APPROVAL_MODE") ?? "").trim() || "always-ask");
		if (boolEnv(`${P}_SIDE_NO_LSP`, true)) args.push("--no-lsp");
		const maxCost = (env("SIDE_MAX_COST") ?? "").trim();
		if (maxCost) args.push("--max-cost", maxCost);
	} else {
		// pi takes the provider and the model id as two flags.
		const { provider, model } = parseModelSpec(env("SIDE_MODEL"));
		const providerOverride = env("SIDE_PROVIDER") || provider;
		const modelOverride = env("SIDE_MODEL_ID") || model;
		if (providerOverride) args.push("--provider", providerOverride);
		if (modelOverride) args.push("--model", modelOverride);
		const thinking = env("SIDE_THINKING");
		if (thinking) args.push("--thinking", thinking);
		const tools = env("SIDE_TOOLS");
		if (tools) args.push("--tools", tools);
		if (!boolEnv(`${P}_SIDE_APPROVE`)) args.push("--no-approve");
		if (boolEnv(`${P}_SIDE_OFFLINE`)) args.push("--offline");
		if (boolEnv(`${P}_SIDE_NO_EXTENSIONS`, false)) args.push("--no-extensions");
	}
	const extra = (env("SIDE_EXTRA_ARGS") ?? "").trim();
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
	const version = await agentVersion();
	log("info", `${dialect.name} version: ${version}`);
	log("info", `session dir: ${SESSION_DIR}`);

	const pool = new SessionPool({
		baseArgs: buildBaseArgs(),
		dataDir: DATA_DIR,
		sessionDir: SESSION_DIR,
		maxProcesses: MAX_PROCESSES,
		idleTimeoutMs: IDLE_TIMEOUT_MS,
		defaultCwd: WORKSPACE,
		dialect,
		...(dialect.name === "omp"
			? {
					// omp takes the whole model spec in --model; the pool adds
					// it per session so a per-session model never duplicates
					// the flag.
					defaultModel: (env("SIDE_MODEL") ?? "").trim() || undefined,
					defaultThinking: (env("SIDE_THINKING") ?? "").trim() || undefined,
				}
			: {}),
		logger: log,
		onEvent: (_sessionId, event) => {
			// Run-window events are subscribed per HTTP stream; events outside
			// any window (e.g. session_info_changed) are logged at debug noise.
			if (process.env[`${P}_LOG_EVENTS`] === "1") {
				log("info", `event: ${JSON.stringify(event).slice(0, 200)}`);
			}
		},
		onStderr: (sessionId, line) => {
			if (line.trim()) log("info", `${dialect.name}[${sessionId}] stderr: ${line.trimEnd()}`);
		},
		onExit: (sessionId, code, signal) => {
			log(
				"warn",
				`${dialect.name} process for ${sessionId} exited (code=${code} signal=${signal})`,
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
		dialect,
	});

	server.listen(PORT, "0.0.0.0", () => {
		log("info", `${dialect.name} sidecar listening on 0.0.0.0:${PORT}`);
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
