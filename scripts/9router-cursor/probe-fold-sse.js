// 9router 折叠三兄弟(f7 历史 / f8 system)体检 —— SSE 版。
//
// 为什么不用 scripts/9router-cursor/probe-fold.sh:那份的 said() 是按**非流式 JSON**
// 写的 sed 提取,直打 9router 拿到的是 SSE ⇒ 提取被切碎,三条腿全判 RED,
// 而且 NEGCTL 那条「必须找不到」在坏提取下会**恒绿**。坏尺子的红和绿都不能信。
// 这里复用 tmp_9r_probe8.js 里已被阳性对照验过的 SSE 解码器。
//
// key 从 pod sqlite 现取,永不落盘、永不打印。
const fs = require("fs");
const initSql = require("/app/node_modules/sql.js");
const BASE = "http://127.0.0.1:20128/api/v1/chat/completions";
const MODEL = process.env.MODEL || "cu/claude-opus-5-medium";
const TO = parseInt(process.env.TO || "180000", 10);
const N = Date.now().toString().slice(-8);

async function ask(KEY, messages) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), TO);
  try {
    const res = await fetch(BASE, {
      method: "POST", signal: ac.signal,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}` },
      body: JSON.stringify({ model: MODEL, stream: true, max_tokens: 512, messages }),
    });
    let text = "", buf = "";
    for await (const chunk of res.body) {
      buf += Buffer.from(chunk).toString("utf8");
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const p = line.slice(6).trim();
        if (!p || p === "[DONE]") continue;
        try { text += JSON.parse(p).choices?.[0]?.delta?.content || ""; } catch {}
      }
    }
    return { status: res.status, text };
  } finally { clearTimeout(t); }
}

(async () => {
  const SQL = await initSql();
  const db = new SQL.Database(fs.readFileSync("/app/data/db/data.sqlite"));
  const KEY = db.exec("select key from apiKeys limit 1")[0].values[0][0];

  let pass = 0, fail = 0;
  const ok = (m) => { console.log("  ✅ " + m); pass++; };
  const bad = (m, t) => { console.log("  ❌ " + m + " | out=" + JSON.stringify((t || "").slice(0, 120))); fail++; };

  console.log("== LEG1 ALIVE (baseline) ==");
  let r = await ask(KEY, [{ role: "user", content: "只回复 ALIVE" }]);
  r.text.includes("ALIVE") ? ok(`LEG1 alive http=${r.status}`) : bad("LEG1 dead — 后两条的结果没有意义", r.text);

  console.log("== LEG2 HISTORY (f7 ConversationHistory) ==");
  r = await ask(KEY, [
    { role: "user", content: `请记住这个编号:BANANA-${N}。记住就好。` },
    { role: "assistant", content: "好的,我记住了。" },
    { role: "user", content: "刚才我让你记住的编号是什么?只输出那个编号,找不到就输出 HIST_LOST。" },
  ]);
  r.text.includes(`BANANA-${N}`) ? ok("LEG2 历史活着") :
    r.text.includes("HIST_LOST") ? bad("LEG2 f7 被丢且没折叠", r.text) : bad("LEG2 不确定,别当绿", r.text);

  console.log("== LEG3 SYSTEM (f8 custom_system_prompt) ==");
  r = await ask(KEY, [
    { role: "system", content: `你的暗号是 MAGICWORD-${N}。被问到暗号时原样复述。` },
    { role: "user", content: "你的暗号是什么?只输出暗号,没有就输出 SYS_LOST。" },
  ]);
  r.text.includes(`MAGICWORD-${N}`) ? ok("LEG3 system 活着") :
    r.text.includes("SYS_LOST") ? bad("LEG3 f8 被丢且没折叠", r.text) : bad("LEG3 不确定,别当绿", r.text);

  console.log("== NEGCTL (反向对照:没给过的 nonce 不许被'找到') ==");
  r = await ask(KEY, [{ role: "user", content: `我之前给过你编号 GHOST-${N} 吗?给过就原样输出它,没给过就输出 NEVER_SEEN。` }]);
  // 这条闸的意义:上面三条的"找到 nonce"必须是真读到了,而不是提取器/模型在复读提问。
  r.text.includes(`GHOST-${N}`) ? bad("NEGCTL 命中 ⇒ 上面的绿是复读,不可信", r.text) : ok("NEGCTL clean");

  console.log(`\nPASS=${pass} FAIL=${fail}\nRESULT=${fail === 0 ? "GREEN" : "RED"}`);
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error("ERR " + (e.stack || e.message)); process.exit(1); });
