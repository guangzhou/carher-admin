// 9router pod-internal regression probe for the Cursor reverse-proxy lane.
// Runs several OpenAI /v1/chat/completions scenarios against 127.0.0.1:20128
// and prints PASS/FAIL per scenario.
//
// WHERE TO RUN: inside the 9router pod (it hits localhost:20128).
//   B64=$(base64 < scripts/9router-cursor/regress_probe.js | tr -d '\n')
//   ssh cltx@10.68.13.198 "echo $B64 | base64 -d > /tmp/tmp_regress.js; \
//     P=\$(sudo kubectl -n litellm-product get pods -l app=9router -o jsonpath='{.items[0].metadata.name}'); \
//     sudo kubectl cp /tmp/tmp_regress.js litellm-product/\$P:/tmp/tmp_regress.js; \
//     sudo kubectl -n litellm-product exec \$P -- sh -c 'NR_KEY=\$(node -e \"…fetch internal key…\") node /tmp/tmp_regress.js'"
//
// KEY (NR_KEY): the internal 9router api key lives in the pod sqlite
//   /app/data/db/data.sqlite -> apiKeys table (name like litellm-bridge).
//   NEVER hardcode it in this shared file. Pass it via env NR_KEY. To read it:
//     sudo kubectl -n litellm-product exec $P -- node -e \
//       "const d=require('/app/node_modules/sql.js'); /* or sqlite */ ..." # see SKILL.md
//   In practice the deploy operator exports NR_KEY before running.
//
// CAVEAT (synthetic green): this exercises the mcp_args tool path + the
// tool-history stateless-fresh-run path. It does NOT reproduce the real-IDE
// interaction_query(web_search field2 / fetch field9) native-tool path — only a
// real Cursor IDE session does. Real IDE = the only terminal judgement.
const http = require("http");
const KEY = process.env.NR_KEY;
const MODEL = process.env.NR_MODEL || "cu/composer-2.5";
if (!KEY) { console.error("FATAL: set NR_KEY (internal 9router key from pod sqlite apiKeys)"); process.exit(2); }

function call(messages, tools) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ model: MODEL, stream: true, messages, ...(tools ? { tools } : {}) });
    const req = http.request(
      { host: "127.0.0.1", port: 20128, path: "/v1/chat/completions", method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + KEY, "Content-Length": Buffer.byteLength(body) } },
      (res) => {
        let buf = "";
        res.on("data", (d) => (buf += d.toString()));
        res.on("end", () => {
          const text = []; const toolCalls = {}; let finish = null, err = null;
          for (const line of buf.split("\n")) {
            if (!line.startsWith("data: ")) continue;
            const p = line.slice(6).trim();
            if (p === "[DONE]") continue;
            let j; try { j = JSON.parse(p); } catch { continue; }
            if (j.error) { err = JSON.stringify(j.error); continue; }
            const ch = j.choices && j.choices[0]; if (!ch) continue;
            if (ch.delta && ch.delta.content) text.push(ch.delta.content);
            if (ch.delta && ch.delta.tool_calls) {
              for (const tc of ch.delta.tool_calls) {
                const i = tc.index || 0;
                toolCalls[i] = toolCalls[i] || { id: "", name: "", args: "" };
                if (tc.id) toolCalls[i].id = tc.id;
                if (tc.function && tc.function.name) toolCalls[i].name = tc.function.name;
                if (tc.function && tc.function.arguments) toolCalls[i].args += tc.function.arguments;
              }
            }
            if (ch.finish_reason) finish = ch.finish_reason;
          }
          resolve({ status: res.statusCode, text: text.join(""), toolCalls: Object.values(toolCalls), finish, err });
        });
      }
    );
    req.on("error", reject);
    req.write(body); req.end();
  });
}

const WEATHER_TOOL = { type: "function", function: { name: "get_weather", description: "Get current weather for a city",
  parameters: { type: "object", properties: { city: { type: "string" } }, required: ["city"] } } };
const TIME_TOOL = { type: "function", function: { name: "get_time", description: "Get current time in a timezone",
  parameters: { type: "object", properties: { tz: { type: "string" } }, required: ["tz"] } } };

(async () => {
  const results = [];
  try {
    const r = await call([{ role: "user", content: "用一句话说明什么是HTTP。" }], null);
    results.push(["A plain-chat", r.status === 200 && r.text.length > 0 && !r.err, `status=${r.status} textLen=${r.text.length} finish=${r.finish} err=${r.err}`]);
  } catch (e) { results.push(["A plain-chat", false, "EXC " + e.message]); }

  let call1 = null;
  try {
    const r = await call([
      { role: "system", content: "Use get_weather for weather questions." },
      { role: "user", content: "无锡今天天气怎么样?用 get_weather 查一下。" },
    ], [WEATHER_TOOL]);
    call1 = r.toolCalls[0];
    results.push(["B single-tool", r.status === 200 && r.finish === "tool_calls" && call1 && call1.name === "get_weather" && !r.err, `status=${r.status} finish=${r.finish} tool=${call1 && call1.name} args=${call1 && call1.args} err=${r.err}`]);
  } catch (e) { results.push(["B single-tool", false, "EXC " + e.message]); }

  try {
    if (!call1) throw new Error("no tool_call from B");
    const r = await call([
      { role: "system", content: "Use get_weather for weather questions." },
      { role: "user", content: "无锡今天天气怎么样?用 get_weather 查一下。" },
      { role: "assistant", content: null, tool_calls: [{ id: call1.id, type: "function", function: { name: "get_weather", arguments: call1.args || '{"city":"无锡"}' } }] },
      { role: "tool", tool_call_id: call1.id, content: '{"city":"无锡","temp_c":27,"condition":"晴","humidity":"55%"}' },
    ], [WEATHER_TOOL]);
    results.push(["C multi-turn tool-result", r.status === 200 && r.text.length > 0 && !r.err && (r.text.includes("27") || r.text.includes("晴")), `status=${r.status} finish=${r.finish} textLen=${r.text.length} text="${r.text.slice(0,60)}" err=${r.err}`]);
  } catch (e) { results.push(["C multi-turn tool-result", false, "EXC " + e.message]); }

  try {
    const r = await call([
      { role: "system", content: "Use the provided tools when relevant." },
      { role: "user", content: "现在北京时间几点?用工具查。" },
    ], [WEATHER_TOOL, TIME_TOOL]);
    const tc = r.toolCalls[0];
    results.push(["D two-tools pick", r.status === 200 && r.finish === "tool_calls" && tc && tc.name === "get_time" && !r.err, `status=${r.status} finish=${r.finish} tool=${tc && tc.name} err=${r.err}`]);
  } catch (e) { results.push(["D two-tools pick", false, "EXC " + e.message]); }

  console.log("\n===== 9router REGRESSION =====");
  let allPass = true;
  for (const [name, ok, detail] of results) { if (!ok) allPass = false; console.log(`${ok ? "PASS" : "FAIL"}  ${name}  | ${detail}`); }
  console.log("===== " + (allPass ? "ALL PASS" : "SOME FAILED") + " =====");
  process.exit(allPass ? 0 : 1);
})();
setTimeout(() => { console.error("[TIMEOUT 120s]"); process.exit(2); }, 120000);
