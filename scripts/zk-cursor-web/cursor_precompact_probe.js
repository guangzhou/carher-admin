#!/usr/bin/env node
/* cursor_precompact_probe.js —— 只读探针:每次 Cursor 自动压缩对话时,把**它自己算出来的
   窗口大小**记一行到 ~/.carher/cursor-precompact.jsonl。

   为什么需要它(2026-10-28):
     「同事说 Cursor 比 Claude 慢」查到根因是件C 补丁失效 ⇒ Cursor 把档位数字(300)当成
     真实窗口(300000)⇒ 触发线 = min(300-10000, 300*0.9) = -9700 ⇒ 每轮都压缩,
     每次 40~100 秒。但要确认某台机器是不是这个病,原来只能让本人双击 DOCTOR 数 marker。
     官方 preCompact 钩子(cursor.com/docs/hooks)在压缩**真发生时**被调用,payload 里带
     `context_window_size` / `context_tokens` / `context_usage_percent` ——
     这就是现场证据:window_size 是 300/500/1 这种档位数字 ⇒ 坏值;是 300000 ⇒ 正常。

   官方对这个钩子的原话:"This is an observational hook that cannot block or modify
   the compaction behavior." ⇒ 它**拦不住压缩**,只能记录。所以本脚本定位是量具,不是修复。

   设计约束:
     - 只写一行 JSON 到自己的日志,**不读对话内容、不上报任何代码/提示词**。
     - 钩子要求输出合法 JSON(或空),出错一律静默成功 —— 绝不能因为探针让同事发不出消息。
     - 日志自带上限(1000 行滚动),不会把磁盘吃满。

   用法(手工装;安装器集成另说):
     把下面这条加进 ~/.cursor/hooks.json 的 hooks.preCompact 数组:
       { "command": "ELECTRON_RUN_AS_NODE=1 \"/Applications/Cursor.app/Contents/MacOS/Cursor\" \"<本文件绝对路径>\"" }
     看结果: node cursor_precompact_probe.js --report
*/
"use strict";
const fs = require("fs"), os = require("os"), path = require("path");

const DIR = path.join(os.homedir(), ".carher");
const LOG = path.join(DIR, "cursor-precompact.jsonl");
const MAX_LINES = 1000;

// 判据:Cursor 自家最小真实窗口是 200000;**小于 4096 的 window_size 一定是档位数字**
// (500k 档 → 500、1m 档 → 1、300k 档 → 300)。界同件C 的 __cxN(两簇中间)。
const TIER_NUMBER_CEIL = 4096;

function classify(win) {
  if (typeof win !== "number" || !Number.isFinite(win) || win <= 0) return "unknown";
  if (win < TIER_NUMBER_CEIL) return "tier-number";   // 🔴 坏值 ⇒ 触发线为负 ⇒ 每轮必压
  return "real-tokens";
}

function report() {
  if (!fs.existsSync(LOG)) {
    console.log("还没有记录。说明装上探针之后这台机器**一次自动压缩都没发生**(这本身是好消息),");
    console.log("或者探针没挂上 —— 检查 ~/.cursor/hooks.json 里有没有 preCompact 这一项。");
    return;
  }
  const rows = fs.readFileSync(LOG, "utf8").trim().split("\n").map((l) => {
    try { return JSON.parse(l); } catch (e) { return null; }
  }).filter(Boolean);
  if (!rows.length) { console.log("日志在但一行都解不出来(文件被别的东西写过?):" + LOG); return; }
  const bad = rows.filter((r) => r.verdict === "tier-number");
  const good = rows.filter((r) => r.verdict === "real-tokens");
  console.log("=== Cursor 自动压缩现场记录(%s)===", LOG);
  console.log("总次数 %d  |  窗口值是档位数字(坏)%d  |  窗口值是真实 token(正常)%d  |  认不出 %d",
    rows.length, bad.length, good.length, rows.length - bad.length - good.length);
  console.log("");
  // 🔴 Node 的 console.log 不支持 %-6s 这种左对齐宽度(那是 printf 的);多参数时它直接
  //    把 "%-6s" 原样打出来再把参数追到行尾 —— 第一版就这样,屏幕上全是 %-8s。手工补空格。
  const pad = (v, n) => String(v ?? "?").padEnd(n);
  for (const r of rows.slice(-12)) {
    console.log("  " + pad((r.ts || "").slice(0, 19).replace("T", " "), 20)
      + " trigger=" + pad(r.trigger, 7)
      + " 窗口=" + pad(r.context_window_size, 9)
      + " 已用=" + pad(r.context_tokens, 9)
      + " 百分比=" + pad(r.context_usage_percent, 9)
      + " 待压=" + pad(r.messages_to_compact, 5)
      + " ⇒ " + r.verdict);
  }
  console.log("");
  if (bad.length) {
    console.log("🔴 抓到坏值:窗口被当成 %s 这种**档位数字**(真实应是 20 万以上)。",
      [...new Set(bad.map((r) => r.context_window_size))].join("/"));
    console.log("   后果:触发线算成负数 ⇒ 每一轮都压缩一次,每次 40~100 秒 = 体感「很慢」。");
    console.log("   处置:完全退出 Cursor → 双击 REPAIR → 重新打开,然后再看这份记录还有没有新坏值。");
  } else if (good.length) {
    console.log("✅ 压缩都发生在真实窗口口径上(窗口 %s),这是正常的「上下文快满了才压」。",
      [...new Set(good.map((r) => r.context_window_size))].join("/"));
  }
}

function main() {
  if (process.argv.includes("--report")) return report();

  // 钩子模式:读 stdin 的 JSON payload,记一行,然后输出空 JSON(不干扰 Cursor)
  let raw = "";
  try { raw = fs.readFileSync(0, "utf8"); } catch (e) { /* 没有 stdin 也不能报错 */ }
  try {
    const p = raw ? JSON.parse(raw) : {};
    // 字段名两种写法都接(官方文档是 snake_case;万一哪版换成 camelCase 也不漏)
    const win = p.context_window_size ?? p.contextWindowSize;
    const used = p.context_tokens ?? p.contextTokens;
    const row = {
      ts: new Date().toISOString(),
      trigger: p.trigger ?? p.triggerType,
      context_window_size: typeof win === "string" ? Number(win) : win,
      context_tokens: typeof used === "string" ? Number(used) : used,
      context_usage_percent: p.context_usage_percent ?? p.contextUsagePercent,
      message_count: p.message_count ?? p.messageCount,
      messages_to_compact: p.messages_to_compact ?? p.messagesToCompact,
      is_first_compaction: p.is_first_compaction ?? p.isFirstCompaction,
      cursor_version: p.cursor_version ?? p.cursorVersion,
      verdict: classify(typeof win === "string" ? Number(win) : win),
    };
    fs.mkdirSync(DIR, { recursive: true });
    fs.appendFileSync(LOG, JSON.stringify(row) + "\n");
    // 滚动:超了就只留后一半,别无限长
    try {
      const lines = fs.readFileSync(LOG, "utf8").split("\n").filter(Boolean);
      if (lines.length > MAX_LINES) fs.writeFileSync(LOG, lines.slice(-Math.floor(MAX_LINES / 2)).join("\n") + "\n");
    } catch (e) { /* 滚动失败不影响记录 */ }
  } catch (e) { /* 解不出来就别记,更不能报错 */ }
  // 官方要求:输出合法 JSON 或空。这里固定输出 {} = 什么都不改。
  process.stdout.write("{}");
}

main();
