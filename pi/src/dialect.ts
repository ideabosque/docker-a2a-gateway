/**
 * dialect.ts — the agent protocol dialect layer.
 *
 * The sidecar speaks ONE HTTP + SSE contract (see server.ts) and drives ONE
 * wire protocol (the agent's RPC JSONL protocol over stdin/stdout). pi and
 * omp (oh-my-pi) share that wire shape but differ in the details that
 * matter to a host:
 *
 * - pi answers a prompt with `data.disposition` and finishes a run with an
 *   `agent_settled` event; omp acknowledges with `data.agentInvoked` and
 *   finishes with a `prompt_result` frame that echoes the prompt id.
 * - omp emits a `ready` frame before it accepts commands, and may split
 *   oversized frames into `rpc_chunk` sequences (protocol v2).
 * - omp takes the whole model spec in `--model` (roles included) and has no
 *   `--provider`; pi splits `provider/modelId` over two flags.
 *
 * Everything agent-specific lives here: one interface, two implementations
 * and a factory that throws on an unknown name — a misconfigured sidecar
 * must fail at startup instead of silently speaking the wrong protocol.
 */

/** pi thinking levels (pi docs: `--thinking <level>`, off … max). */
const PI_THINKING_LEVELS = [
	"inherit",
	"off",
	"minimal",
	"low",
	"medium",
	"high",
	"xhigh",
	"max",
] as const;

/**
 * omp thinking levels. `inherit` is NOT a CLI value — the sidecar omits the
 * flag for it and lets omp pick the model's own default.
 */
const OMP_THINKING_LEVELS = [
	"off",
	"minimal",
	"low",
	"medium",
	"high",
	"xhigh",
	"max",
] as const;

/**
 * Variables the pi child must never inherit: pi exports/uses them for its
 * own tool shells, so a host shell's pi state would masquerade as sidecar
 * configuration (P1 spike: a host PI_PROVIDER made sessions boot-fail with
 * "Unknown provider").
 */
const PI_CHILD_ENV_BLOCKED = [
	"PI_MODEL",
	"PI_PROVIDER",
	"PI_THINKING",
	"PI_TOOLS",
	"PI_EXTRA_ARGS",
	"PI_APPROVE",
] as const;

/**
 * The omp child gets the same treatment, plus the pi names: a host that
 * runs pi locally may carry PI_* in its shell, and omp must not pick them up
 * either.
 */
const OMP_CHILD_ENV_BLOCKED = [
	"OMP_MODEL",
	"OMP_PROVIDER",
	"OMP_THINKING",
	"OMP_TOOLS",
	"OMP_EXTRA_ARGS",
	"OMP_APPROVE",
	...PI_CHILD_ENV_BLOCKED,
] as const;

export interface AgentDialect {
	readonly name: "pi" | "omp";
	/** Sidecar env namespace: PI_* or OMP_*. */
	readonly envPrefix: "PI" | "OMP";
	readonly defaultBin: string;
	readonly defaultPort: number;
	/** Wait for the `ready` frame before any command may be sent. */
	readonly needsReadyHandshake: boolean;
	/** Send `negotiate_protocol {protocolVersion: 2}` after `ready`. */
	readonly negotiatesProtocolV2: boolean;
	/** Reassemble `rpc_chunk` frames (protocol v2). */
	readonly supportsChunkReassembly: boolean;
	/** How a run is known to be finished. */
	readonly completion: "agent_settled" | "prompt_result";
	/** How a prompt that started no run is signalled. */
	readonly handledSignal: "disposition" | "agentInvoked";
	/** Allowed `--thinking` values for this dialect. */
	readonly thinkingLevels: readonly string[];
	/** Env names stripped from the spawned child. */
	readonly childEnvBlocked: readonly string[];
	spawnFlags(opts: { model?: string; thinking?: string }): string[];
}

const piDialect: AgentDialect = {
	name: "pi",
	envPrefix: "PI",
	defaultBin: "pi",
	defaultPort: 3300,
	needsReadyHandshake: false,
	negotiatesProtocolV2: false,
	supportsChunkReassembly: false,
	completion: "agent_settled",
	handledSignal: "disposition",
	thinkingLevels: PI_THINKING_LEVELS,
	childEnvBlocked: PI_CHILD_ENV_BLOCKED,
	spawnFlags({ model, thinking }): string[] {
		const flags: string[] = [];
		if (model) {
			// pi takes the provider and the model id as two flags.
			const slash = model.indexOf("/");
			if (slash > 0) {
				flags.push("--provider", model.slice(0, slash), "--model", model.slice(slash + 1));
			} else {
				flags.push("--model", model);
			}
		}
		if (thinking) flags.push("--thinking", thinking);
		return flags;
	},
};

const ompDialect: AgentDialect = {
	name: "omp",
	envPrefix: "OMP",
	defaultBin: "omp",
	defaultPort: 3310,
	needsReadyHandshake: true,
	negotiatesProtocolV2: true,
	supportsChunkReassembly: true,
	completion: "prompt_result",
	handledSignal: "agentInvoked",
	thinkingLevels: OMP_THINKING_LEVELS,
	childEnvBlocked: OMP_CHILD_ENV_BLOCKED,
	spawnFlags({ model, thinking }): string[] {
		const flags: string[] = [];
		// omp takes the whole spec (provider/modelId or a role) in --model;
		// --provider is legacy.
		if (model) flags.push("--model", model);
		// `inherit` is not a CLI value — omp picks the model's own default.
		if (
			thinking &&
			thinking !== "inherit" &&
			(OMP_THINKING_LEVELS as readonly string[]).includes(thinking)
		) {
			flags.push("--thinking", thinking);
		}
		return flags;
	},
};

const DIALECTS: Record<string, AgentDialect> = {
	pi: piDialect,
	omp: ompDialect,
};

/**
 * Build the dialect for `name` (default: pi). Throws on an unknown name —
 * a sidecar started with a bogus AGENT_DIALECT must fail, never guess.
 */
export function createDialect(name?: string): AgentDialect {
	const key = (name ?? "pi").trim() || "pi";
	const dialect = DIALECTS[key];
	if (!dialect) {
		throw new Error(
			`unknown agent dialect "${key}" (expected one of: ${Object.keys(DIALECTS).join(", ")})`,
		);
	}
	return dialect;
}
