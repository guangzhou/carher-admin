// Start `rea mcp` over stdio, page through tools/list, print one JSON line, exit.
// Usage: node mcp_tools.mjs <rea clone root>
// This is the runtime catalog (what a client actually sees), not a parse of the TS contracts.
import { spawn } from "node:child_process";

const root = process.argv[2];
if (!root) {
  console.error("usage: mcp_tools.mjs <rea root>");
  process.exit(64);
}
const child = spawn(process.execPath, ["scripts/rea.mjs", "mcp"], {
  cwd: root,
  stdio: ["pipe", "pipe", "pipe"],
});
let buf = "";
let stderr = "";
const tools = [];
let serverInfo = null;
const send = (m) => child.stdin.write(JSON.stringify(m) + "\n");
let done = false;
const fail = (msg, code) => {
  if (done) return;
  done = true;
  child.kill();
  process.stdout.write(JSON.stringify({ ok: false, error: msg, stderr: stderr.slice(-2000) }) + "\n", () =>
    process.exit(code),
  );
};
const timer = setTimeout(() => fail("timeout waiting for tools/list", 2), 120000);
child.stderr.on("data", (d) => (stderr += d));
child.on("exit", (code) => {
  if (!done && tools.length === 0) fail(`rea mcp exited (${code}) before tools/list`, 3);
});
child.stdout.on("data", (d) => {
  buf += d;
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i);
    buf = buf.slice(i + 1);
    if (!line.trim()) continue;
    const m = JSON.parse(line);
    if (m.error) fail(`JSON-RPC error: ${JSON.stringify(m.error)}`, 4);
    if (m.id === 1) {
      serverInfo = m.result.serverInfo ?? null;
      send({ jsonrpc: "2.0", method: "notifications/initialized" });
      send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
    } else if (m.id >= 2) {
      tools.push(...m.result.tools);
      const cursor = m.result.nextCursor;
      if (cursor) {
        send({ jsonrpc: "2.0", id: m.id + 1, method: "tools/list", params: { cursor } });
      } else {
        done = true;
        clearTimeout(timer);
        const payload = JSON.stringify({
          ok: true,
          server: serverInfo,
          tools: tools.map((t) => ({
            name: t.name,
            description: (t.description ?? "").split("\n")[0],
            annotations: t.annotations ?? {},
          })),
        });
        child.kill();
        // ~130 KB: process.exit() right after console.log truncates a pipe at 64/128 KB.
        // Exit only from the write callback, once the whole line has been handed to the pipe.
        process.stdout.write(payload + "\n", () => process.exit(0));
      }
    }
  }
});
send({
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "cli-anything-rea", version: "1" } },
});
