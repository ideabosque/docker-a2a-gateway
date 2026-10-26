import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, it } from "vitest";
import { SessionPool } from "../src/pool.js";
import { createServer } from "../src/server.js";

it("ends a run when prompt acknowledgement and settlement share one stdout read", async () => {
	const dir = await mkdtemp(path.join(tmpdir(), "pi-fast-settle-"));
	const program = `
	let buffer = "";
	process.stdin.on("data", chunk => {
		buffer += chunk;
		let pos;
		while ((pos = buffer.indexOf(String.fromCharCode(10))) >= 0) {
			const cmd = JSON.parse(buffer.slice(0, pos)); buffer = buffer.slice(pos + 1);
			const response = {type:"response", id:cmd.id, command:cmd.type, success:true,
				data:cmd.type === "get_state" ? {sessionId:"fast-session", isStreaming:false} : {disposition:"started"}};
			const records = [response];
			if (cmd.type === "prompt") records.push({type:"agent_settled", aborted:false});
			process.stdout.write(records.map(r=>JSON.stringify(r)).join(String.fromCharCode(10))+String.fromCharCode(10));
		}
	});
	process.stdin.on("end",()=>process.exit(0));`;
	const pool = new SessionPool({ baseArgs: [process.execPath, "-e", program, "--"], dataDir: dir,
		sessionDir: dir, maxProcesses: 1, idleTimeoutMs: 0, defaultCwd: dir, logger: () => {} });
	const server = createServer({ port: 0, secretKey: "test", pool, piVersion: "test",
		streamTimeoutMs: 1000, maxBodyBytes: 4096, log: () => {} });
	try {
		await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
		const address = server.address() as { port: number };
		const session = await pool.create();
		const response = await fetch(`http://127.0.0.1:${address.port}/sessions/${session.id}/prompt`, {
			method: "POST", headers: { "X-Secret-Key": "test", "content-type": "application/json" },
			body: JSON.stringify({ message: "hello" }), signal: AbortSignal.timeout(5000),
		});
		const frames = (await response.text()).trim().split("\n\n")
			.map(frame => JSON.parse(frame.slice(6)));
		expect(frames.at(-1)).toMatchObject({ type: "run_end", stopReason: "end_turn", aborted: false });
	} finally {
		await pool.closeAll();
		await new Promise<void>(resolve => server.close(() => resolve()));
		await rm(dir, { recursive: true, force: true });
	}
});
