/**
 * tool-approval.test.ts — unit tests for the bundled tool-approval extension
 * against a mocked pi ExtensionAPI + ui context.
 *
 * Covers: passive when PI_SIDE_APPROVE_TOOLS unset/off, gating list + "*",
 * fail-closed without UI, block on "no", allow on confirm, dialog timeout
 * passthrough, and the input summary rendering.
 */

import path from "node:path";

import { beforeEach, describe, expect, it } from "vitest";

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));

interface CallResult {
	block?: boolean;
	reason?: string;
}

interface Harness {
	toolCall(event: { toolName: string; input?: unknown }): Promise<CallResult | undefined>;
	dialogs: Array<{ kind: string; args: unknown[] }>;
	/** The extension registers nothing when passive. */
	registered: boolean;
}

/** Install the extension on a mock pi and return a tool-call harness. */
async function install(env: Record<string, string>): Promise<Harness> {
	process.env.PI_SIDE_APPROVE_TOOLS = env.PI_SIDE_APPROVE_TOOLS ?? "";
	delete process.env.PI_SIDE_APPROVE_TIMEOUT_MS;
	if (env.PI_SIDE_APPROVE_TIMEOUT_MS !== undefined) {
		process.env.PI_SIDE_APPROVE_TIMEOUT_MS = env.PI_SIDE_APPROVE_TIMEOUT_MS;
	}
	const handlers: Record<string, Function> = {};
	const dialogs: Array<{ kind: string; args: unknown[] }> = [];
	const mod = await import(
		/* @vite-ignore */ new URL("../extensions/tool-approval.js", import.meta.url).href
	);
	mod.default({
		on: (event: string, handler: Function) => {
			handlers[event] = handler;
		},
	});
	const hasUI = env.__no_ui ? false : true;
	const approve = env.__approve !== "false";
	const confirmImpl = async (...args: unknown[]) => {
		dialogs.push({ kind: "confirm", args });
		return approve;
	};
	const harness: Harness = {
		dialogs,
		registered: Boolean(handlers["tool_call"]),
		async toolCall(event) {
			const handler = handlers["tool_call"];
			expect(handler, "tool_call handler registered").toBeTruthy();
			return handler(event, { hasUI, ui: { confirm: confirmImpl } });
		},
	};
	return harness;
}

describe("tool-approval extension", () => {
	beforeEach(() => {
		delete process.env.PI_SIDE_APPROVE_TIMEOUT_MS;
	});

	it("is passive when PI_SIDE_APPROVE_TOOLS is unset/empty", async () => {
		const h = await install({});
		expect(h.registered).toBe(false); // no gate configured → no handler
	});

	it("is passive for off/none", async () => {
		const h = await install({ PI_SIDE_APPROVE_TOOLS: "Off" });
		expect(h.registered).toBe(false);
	});

	it("gates only the listed tools", async () => {
		const h = await install({ PI_SIDE_APPROVE_TOOLS: "Bash, Edit,write" });
		expect(await h.toolCall({ toolName: "bash", input: { command: "echo hi" } })).toBeUndefined();
		expect(h.dialogs).toHaveLength(1); // bash was gated
		expect(await h.toolCall({ toolName: "read", input: { path: "/x" } })).toBeUndefined();
		expect(h.dialogs).toHaveLength(1); // read was not gated
	});

	it("gates everything with *", async () => {
		const h = await install({ PI_SIDE_APPROVE_TOOLS: "*" });
		await h.toolCall({ toolName: "read", input: { path: "/x" } });
		await h.toolCall({ toolName: "webfetch", input: { url: "https://x" } });
		expect(h.dialogs).toHaveLength(2);
	});

	it("fails closed without a UI", async () => {
		const h = await install({ PI_SIDE_APPROVE_TOOLS: "bash", __no_ui: "1" });
		const result = await h.toolCall({ toolName: "bash", input: { command: "echo hi" } });
		expect(result?.block).toBe(true);
		expect(result?.reason).toContain("no UI");
		expect(h.dialogs).toHaveLength(0);
	});

	it("blocks when the user answers no, allows on yes", async () => {
		const deny = await install({ PI_SIDE_APPROVE_TOOLS: "bash", __approve: "false" });
		const denied = await deny.toolCall({ toolName: "bash", input: { command: "rm -rf /tmp/x" } });
		expect(denied?.block).toBe(true);
		expect(denied?.reason).toContain("was not approved");
		// the dialog carries the summarized command text
		expect(String(deny.dialogs[0].args[1])).toContain("rm -rf /tmp/x");

		const allow = await install({ PI_SIDE_APPROVE_TOOLS: "bash" });
		const allowed = await allow.toolCall({ toolName: "bash", input: { command: "echo hi" } });
		expect(allowed).toBeUndefined();
	});

	it("passes the dialog timeout through when set", async () => {
		const h = await install({
			PI_SIDE_APPROVE_TOOLS: "bash",
			PI_SIDE_APPROVE_TIMEOUT_MS: "45000",
		});
		await h.toolCall({ toolName: "bash", input: { command: "echo hi" } });
		const opts = h.dialogs[0].args[2] as { timeout?: number } | undefined;
		expect(opts?.timeout).toBe(45_000);
	});
});