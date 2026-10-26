/**
 * tool-approval.js — pi extension shipped by the docker-a2a-gateway sidecar
 * (mounted into <agent-dir>/extensions by the entrypoint, no clobber).
 *
 * pi's built-in tools (bash / edit / write / read / ...) run WITHOUT approval
 * prompts — the pi process uses its own OS permissions and the container is
 * the boundary. This extension adds the interactive layer: gated tool calls
 * open a ctx.ui dialog, which in RPC mode is a BLOCKING extension_ui_request
 * — answered upstream by the A2A bridge according to the agent's
 * pi_permission_mode (handlers/pi_handler.py):
 *
 *   auto_allow  → dialog confirmed immediately (kept for the round-trip)
 *   deny        → dialog cancelled (confirm → confirmed: false) → blocked
 *   ask         → task INPUT_REQUIRED, the user approves over the A2A
 *                 surface (`metadata.operation = "approval_response"`),
 *                 which flows back to pi as the dialog answer.
 *
 * Configuration (sidecar env, read by every spawned pi process):
 *   PI_SIDE_APPROVE_TOOLS      comma list of tool names to gate
 *                              (e.g. "bash,edit,write"); "*" or "all" gates
 *                              every tool; empty/unset → passive (no gate).
 *   PI_SIDE_APPROVE_TIMEOUT_MS optional dialog timeout; on expiry pi
 *                              auto-resolves the dialog with its default
 *                              (confirm → false) — i.e. fail-closed.
 *
 * Fail-closed everywhere: no UI, dialog timeout, or a "no" answer blocks the
 * tool with a reason; the model sees the block and can continue.
 *
 * Unit tests: test/tool-approval.test.ts (mocked pi/context).
 */

/** Summarize tool input to a short, human-readable dialog message. */
function summarizeInput(toolName, input) {
	try {
		if (input && typeof input === "object") {
			if (input.command) return String(input.command).slice(0, 400);
			if (input.path || input.file_path) return String(input.path || input.file_path).slice(0, 400);
			if (input.url) return String(input.url).slice(0, 400);
			return JSON.stringify(input).slice(0, 400);
		}
		return String(input ?? "").slice(0, 400);
	} catch {
		return `${toolName} call`;
	}
}

export default function (pi) {
	const raw = (process.env.PI_SIDE_APPROVE_TOOLS || "").trim().toLowerCase();
	if (!raw || raw === "off" || raw === "none") return; // passive extension

	const timeoutRaw = (process.env.PI_SIDE_APPROVE_TIMEOUT_MS || "").trim();
	const timeout = /^\d+$/.test(timeoutRaw) && Number(timeoutRaw) > 0
		? Number(timeoutRaw)
		: undefined;

	const names = raw
		.split(",")
		.map((s) => s.trim())
		.filter(Boolean);
	const gateAll = names.includes("*") || names.includes("all");

	pi.on("tool_call", async (event, ctx) => {
		const toolName = String(event.toolName || "");
		const gated = gateAll || names.includes(toolName.toLowerCase());
		if (!gated) return undefined; // not gated → pi's normal behavior

		if (!ctx.hasUI) {
			return {
				block: true,
				reason: `${toolName} blocked for approval: no UI available in this runtime`,
			};
		}

		const detail = summarizeInput(toolName, event.input);
		const ok = await ctx.ui.confirm(
			`Allow ${toolName}?`,
			detail,
			...(timeout !== undefined ? [{ timeout }] : []),
		);
		if (!ok) {
			return { block: true, reason: `${toolName} was not approved` };
		}
		return undefined; // approved → run the tool
	});

	// Diagnostics (stderr — never stdout, which is protocol-only in RPC mode).
	process.stderr.write(
		`[tool-approval] active: tools=${raw} timeout=${timeout ?? "unset"}\n`,
	);
}