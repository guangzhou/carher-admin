#!/usr/bin/env node
/* 「刷模型」—— 按这把 key 重写 Claude Code 的模型菜单(2026-10-19)。

   Claude Code 自己会做网关模型发现(CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1),但非 claude 的
   网关模型要靠 settings.json 里的 modelPicker(behavesAs)才不报「isn't recognized」。那张表以前只在
   安装时写一次 ⇒ 网关删掉的模型一直留在 /model 里(点了 403),新开通的进不来。
   本脚本:读 settings.json 里的 key/地址 → GET /v1/models → 重写 modelPicker(旧的删、新的加)
   → 删模型缓存 → 当前 model 已没权限就清掉(回到默认)→ haiku 默认指到 key 里真有的那个。安装器第 4 步和「刷模型」共用这一份。

   只用 Node 自带模块(Node ≥ 18)。用法:
     node carher_models_refresh.js                 # 拉网关并写入
     CARHER_MODELS="a\nb" node carher_models_refresh.js   # 不出网,用给定列表(安装器/测试用)
   拉不到列表就不动 settings.json(绝不拿空列表把菜单清空)。改前备份到 settings.json.bak-carher-refresh。 */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");

const CLAUDE_HOME = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
const SETTINGS = process.env.CARHER_SETTINGS || path.join(CLAUDE_HOME, "settings.json");
const BEHAVES_AS = "claude-sonnet-5-5";

/* 10-29 换对外域名:网关地址从旧的 /pro 换成 chat.auto-link.com.cn/model(同一个网关,路径改名)。
   旧地址共存一段时间 ⇒ 新旧两种都认作「我们的网关」。是旧的 ⇒ 先用这把 key 打新地址 /v1/models,
   拿到列表(200)才改写;不通就保持原样,下次开会话再试。只认我们那一个地址,用户自己指的别的网关不碰。 */
const NEW_BASE = process.env.CARHER_MIGRATE_TO || "https://chat.auto-link.com.cn/model";   // 环境变量只给测试用
const LEGACY_BASE = /^https:\/\/cc\.auto-link\.com\.cn\/pro\/?$/;
const isLegacyBase = (b) => LEGACY_BASE.test(String(b || "").trim());
const CLAUDE_FAMILY = ["fable", "opus", "sonnet", "haiku"];
const familyRank = (i) => { const k = CLAUDE_FAMILY.findIndex((f) => i.startsWith(`claude-${f}-`)); return k < 0 ? CLAUDE_FAMILY.length : k; };

/* /model 菜单 = 这把 key 能聊天的全部模型,每个只出现一次(10-25)。
   以前只写非 claude 的、再叠上 Claude Code 自带 + 网关发现 ⇒ 同一个模型出现两次、出图模型和 key 没有的 Fable 也在菜单里。
   现在 replaceBuiltInOptions:true ⇒ 菜单只剩「Default + 这里的行」(官方 settings-reference#modelpicker)。
   claude-*:不写 label,Claude Code 自己显示成 Opus 5.5 这类名字;同家族按版本新→旧。
   非 claude:behavesAs 指一个它认识的模型 ⇒ 不再告警「isn't recognized」。出图/视频/音乐、审查专用不进。 */
function pickerRows(ids) {
  const seen = new Set();
  const chat = ids.filter((i) => typeof i === "string" && i && !seen.has(i) && seen.add(i))
    .filter((i) => !/image|imagine|video|music/.test(i) && i !== "codex-auto-review");
  const claude = chat.filter((i) => /^claude-/.test(i))
    .sort((a, b) => familyRank(a) - familyRank(b) || b.localeCompare(a, "en", { numeric: true }));
  return [
    ...claude.map((i) => ({ model: i })),
    ...chat.filter((i) => !/^claude-/.test(i)).map((i) => ({ model: i, label: i, behavesAs: BEHAVES_AS })),
  ];
}

/* 把网关列表并进 settings 对象。返回 {d, added, removed, modelReset}。纯函数,测试直接调。 */
function applyModels(d, ids) {
  const before = ((d.modelPicker && d.modelPicker.options) || []).map((o) => o && o.model).filter(Boolean);
  const rows = pickerRows(ids);
  const after = rows.map((r) => r.model);
  if (rows.length) d.modelPicker = { ...(d.modelPicker || {}), options: rows, replaceBuiltInOptions: true };
  else delete d.modelPicker;
  // 当前钉着的 model 已不在网关里 ⇒ 每次新开窗口都报错。清掉 = 回到 Claude Code 默认模型。
  // 只认「网关名」:上次菜单里的,或 claude-* 全名(`[1m]` 后缀先剥)。opus / sonnet 这类别名不动。
  let modelReset = null;
  if (typeof d.model === "string" && d.model) {
    const bare = d.model.replace(/\[[^\]]*\]$/, "");
    if ((before.includes(bare) || /^claude-/.test(bare)) && !ids.includes(bare)) { modelReset = d.model; delete d.model; }
  }
  /* 10-19 sunqiang/linsen:Claude Code 2.1.30x 的 haiku 默认是 claude-haiku-5-5(后台起标题、子任务用),
     网关没有这个模型 ⇒ 后台请求一直 403。⇒ 指到这把 key 里真有的 haiku(按版本新→旧挑);
     一个都没有就删掉这项(让 Claude Code 自己决定)。用户自己设的、且仍可用的不动。
     10-25 网关上了 claude-haiku-5-5:之前写进去的老 haiku(4-5)仍可用但不是最新 ⇒ 也升到最新;
     用户指到非 haiku 的(比如 sonnet)仍不动。 */
  d.env = Object.assign({}, d.env || {});
  // 10-25:每次开会话的自动刷新顺带补上这项 ⇒ 装过带 hook 版本的同事以后不用重跑安装也能拿到(用户自己写过不动)
  if (!d.env.ENABLE_TOOL_SEARCH) d.env.ENABLE_TOOL_SEARCH = "true";
  const haikus = ids.filter((i) => /^claude-haiku-\d/.test(i))
    .sort((a, b) => b.localeCompare(a, "en", { numeric: true }));
  const curH = d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  let haikuSet = null;
  const staleHaiku = curH && /^claude-haiku-\d/.test(curH) && haikus.length && curH !== haikus[0];
  if (!(curH && ids.includes(curH)) || staleHaiku) {
    if (haikus.length) { d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL = haikus[0]; haikuSet = haikus[0]; }
    else delete d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  }
  return {
    d,
    haikuSet,
    added: after.filter((m) => !before.includes(m)),
    removed: before.filter((m) => !after.includes(m)),
    modelReset,
  };
}

/* 桌面 App(Code 标签页)的模型菜单(10-27 用户:「刷模型在 App 里也搞定啊」)。
   App 不读 settings.json 的 modelPicker,它自己在**启动时**去网关拉一次 /v1/models,结果只存在内存里。
   App 配置项 `inferenceModels` 一旦写了,App 就不再自己拉、菜单 = 这份列表(本机实测日志:
   「Model discovery: skipped (inferenceModels obviates discovery); picker = 2 (inferenceModels)」)。
   ⇒ 刷模型把 key 里的 claude-* 写进去(App 只认 Claude 模型,非 claude 写进去会被它拒),菜单就和 key 完全一致;
   App 要**完全退出再打开**才读新配置。只改我们自己那条(_meta.json 里叫 CarHer 的),别的配置不碰。 */
function appConfigEntries() {
  const roots = [];
  if (process.platform === "darwin") roots.push(path.join(os.homedir(), "Library", "Application Support", "Claude-3p"));
  else if (process.platform === "win32") {
    if (process.env.LOCALAPPDATA) roots.push(path.join(process.env.LOCALAPPDATA, "Claude-3p"));
    if (process.env.APPDATA) roots.push(path.join(process.env.APPDATA, "Claude-3p"));
    // MSIX 版数据会被重定向到 Packages\Claude_*\LocalCache\Local\Claude-3p
    try {
      const pk = path.join(process.env.LOCALAPPDATA || "", "Packages");
      for (const d of fs.readdirSync(pk)) if (/^Claude_/i.test(d)) roots.push(path.join(pk, d, "LocalCache", "Local", "Claude-3p"));
    } catch (e) { /* 没有就算了 */ }
  } else roots.push(path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "Claude-3p"));
  const out = [];
  for (const r of roots) {
    const lib = path.join(r, "configLibrary");
    let meta; try { meta = JSON.parse(fs.readFileSync(path.join(lib, "_meta.json"), "utf8")); } catch (e) { continue; }
    for (const e of (meta && meta.entries) || []) {
      if (e && e.name === "CarHer" && /^[a-f0-9-]{36}$/.test(e.id || "")) out.push(path.join(lib, e.id + ".json"));
    }
  }
  return out;
}
/* newBase:这次已用这把 key 在新地址上拉到了列表 ⇒ App 我们那条还写着旧地址就一起换过去(10-29 换域名)。 */
function syncAppModels(ids, key, newBase) {
  const want = ids.filter((i) => /^claude-/.test(i) && !/image|imagine|video|music/.test(i))
    .sort((a, b) => familyRank(a) - familyRank(b) || b.localeCompare(a, "en", { numeric: true }));
  const res = [];
  for (const p of appConfigEntries()) {
    let c; try { c = JSON.parse(fs.readFileSync(p, "utf8")); } catch (e) { continue; }
    if (key && c.inferenceGatewayApiKey && c.inferenceGatewayApiKey !== key) continue;   // 不是这把 key 配的那条,不动
    const baseMoved = !!(newBase && isLegacyBase(c.inferenceGatewayBaseUrl));
    if (baseMoved) c.inferenceGatewayBaseUrl = newBase;
    const before = Array.isArray(c.inferenceModels) ? c.inferenceModels.map((m) => (typeof m === "string" ? m : m && m.name)).filter(Boolean) : null;
    let menuChanged = true;
    if (!want.length) {
      // key 里一个 Claude 模型都没有 ⇒ 别写空列表(App 会没有可选的),交回 App 自己拉
      if (before) { delete c.inferenceModels; } else if (baseMoved) menuChanged = false; else continue;
    } else if (before && before.join("\n") === want.join("\n")) {
      if (!baseMoved) { res.push({ p, changed: false, models: want }); continue; }
      menuChanged = false;
    } else c.inferenceModels = want;
    const tmp = p + ".carher-tmp-" + process.pid;
    fs.writeFileSync(tmp, JSON.stringify(c, null, 2) + "\n", { mode: 0o600 });
    fs.renameSync(tmp, p);
    res.push({ p, changed: true, menuChanged, baseMoved, models: want, added: want.filter((m) => !(before || []).includes(m)), removed: (before || []).filter((m) => !want.includes(m)) });
  }
  return res;
}

/* settings.json 里是旧网关地址 ⇒ 用这把 key 打新地址;拿到列表才改 d.env.ANTHROPIC_BASE_URL。
   fetch 可注入(测试)。返回 {ids, base, moved:bool, why?}:moved=false 时 ids 为 null,调用方照旧地址拉。 */
async function migrateIfReachable(d, key, fetch = fetchIdsLive) {
  const cur = d && d.env && d.env.ANTHROPIC_BASE_URL;
  if (!isLegacyBase(cur)) return { ids: null, base: cur, moved: false };
  try {
    const ids = await fetch(NEW_BASE, key);
    if (!ids.length) return { ids: null, base: cur, moved: false, why: "新地址没返回任何模型" };
    d.env.ANTHROPIC_BASE_URL = NEW_BASE;
    return { ids, base: NEW_BASE, moved: true };
  } catch (e) { return { ids: null, base: cur, moved: false, why: e.message }; }
}

/* 把这把 key 记成「已同意」(10-27 王德升:命令行右下角一直「Not logged in · Run /login」)。
   Claude Code 第一次看到 settings 里的 ANTHROPIC_API_KEY 会问「Do you want to use this API key?」,默认选中 **No**;
   回车一下就记进全局配置 customApiKeyResponses.rejected,之后**每次启动都不用这把 key** ⇒ 显示未登录(请求照样走,
   因为 App / -p 另有来源,但交互界面认为没登录)。本机隔离复现:rejected ⇒ Not logged in;approved ⇒ 正常。
   比较的是 key 末尾 20 个字符(Claude Code 自己的 Vpe())。用户换 key 后旧条目留着无害。
   配置文件:CLAUDE_CONFIG_DIR/.claude.json,没设时是 ~/.claude.json(和 settings.json 不在一个目录)。 */
function approveApiKey(key, configDir) {
  const tail = String(key || "").trim().slice(-20);
  if (!tail) return false;
  const p = configDir ? path.join(configDir, ".claude.json") : path.join(os.homedir(), ".claude.json");
  let g = {};
  try { g = JSON.parse(fs.readFileSync(p, "utf8").replace(/^\uFEFF/, "")) || {}; } catch (e) { if (fs.existsSync(p)) return false; }
  const r = (g.customApiKeyResponses && typeof g.customApiKeyResponses === "object") ? g.customApiKeyResponses : {};
  const approved = Array.isArray(r.approved) ? r.approved.filter((x) => typeof x === "string") : [];
  const rejected = Array.isArray(r.rejected) ? r.rejected.filter((x) => typeof x === "string") : [];
  if (approved.includes(tail) && !rejected.includes(tail)) return false;
  g.customApiKeyResponses = { ...r, approved: approved.includes(tail) ? approved : [...approved, tail], rejected: rejected.filter((x) => x !== tail) };
  const tmp = p + ".carher-tmp-" + process.pid;
  fs.writeFileSync(tmp, JSON.stringify(g, null, 2) + "\n", { mode: 0o600 });
  fs.renameSync(tmp, p);
  return true;
}

/* 在 settings.hooks.SessionStart 里挂(或更新)我们那一条自动刷新 hook。重跑幂等:先摘掉旧的
   (认 command 里的 carher_models_refresh.js),用户自己的 hook 一条不动。
   exec 形式(args)不经 shell ⇒ 路径带空格/中文、Windows 没 Git Bash 都不怕(hook args 需 2.1.139+)。 */
function hookEntry(d, nodeAbs, scriptAbs) {
  const ours = (h) => h && JSON.stringify(h).includes("carher_models_refresh.js");
  d.hooks = d.hooks && typeof d.hooks === "object" && !Array.isArray(d.hooks) ? d.hooks : {};
  const list = (Array.isArray(d.hooks.SessionStart) ? d.hooks.SessionStart : [])
    .map((g) => (g && Array.isArray(g.hooks) ? { ...g, hooks: g.hooks.filter((h) => !ours(h)) } : g))
    .filter((g) => !(g && Array.isArray(g.hooks) && g.hooks.length === 0));
  list.push({ matcher: "startup", hooks: [{ type: "command", command: nodeAbs, args: [scriptAbs, "--quiet"], async: true, timeout: 60 }] });
  d.hooks.SessionStart = list;
  return d;
}

async function fetchIds(base, key) {
  if (process.env.CARHER_MODELS != null) return process.env.CARHER_MODELS.split("\n").map((s) => s.trim()).filter(Boolean);
  return fetchIdsLive(base, key);
}
/* 一定真打网关(不吃 CARHER_MODELS):换地址前「新地址真的通」只能靠真请求判 */
async function fetchIdsLive(base, key) {
  const r = await fetch(String(base).replace(/\/+$/, "") + "/v1/models", {
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01" }, signal: AbortSignal.timeout(30000),
  });
  if (r.status === 401 || r.status === 403) throw new Error(`key 无效或已停用(HTTP ${r.status})`);
  if (!r.ok) throw new Error(`网关返回 HTTP ${r.status}`);
  return ((await r.json()).data || []).map((m) => m && m.id).filter(Boolean);
}

/* 每次开 Claude Code 自动刷一次(10-25):安装器在 settings.json 里挂一个 SessionStart 异步 hook 跑
   `node carher_models_refresh.js --quiet`。后台跑、不挡输入;--quiet 不往屏幕/对话里打任何东西,
   拉不到就原样退出 0(断网不报错),结果只记到 ~/.claude/carher/refresh.log。
   ⇒ 管理员给 key 开/关模型后,用户下次开 Claude Code 菜单就对了,不用记得说「刷模型」。 */
const QUIET = process.argv.includes("--quiet");
const LOG = path.join(CLAUDE_HOME, "carher", "refresh.log");
function logLine(s) {
  if (!QUIET) return;
  try {
    fs.mkdirSync(path.dirname(LOG), { recursive: true });
    let old = ""; try { old = fs.readFileSync(LOG, "utf8"); } catch (e) { /* 第一次 */ }
    const lines = (old + new Date().toISOString() + " " + s + "\n").split("\n").slice(-200);
    fs.writeFileSync(LOG, lines.join("\n"), { mode: 0o600 });
  } catch (e) { /* 记不了日志也不影响 */ }
}
const say = (s) => { if (!QUIET) console.log(s); };

async function main() {
  let raw;
  try { raw = fs.readFileSync(SETTINGS, "utf8").replace(/^\uFEFF/, ""); }
  catch (e) { throw new Error(`找不到 ${SETTINGS},先跑一键配置脚本`); }
  const d = raw.trim() ? JSON.parse(raw) : {};
  const env = d.env || {};
  const key = process.env.CARHER_KEY || env.ANTHROPIC_API_KEY, base = process.env.CARHER_BASE || env.ANTHROPIC_BASE_URL;
  if (!key || !base) throw new Error("settings.json 里没有 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,先跑一键配置脚本");
  let ids;
  // 10-29 换域名:旧地址 ⇒ 新地址拿得到列表才改写(只在没被 CARHER_BASE 指定时;安装器/测试会指定)
  const mig = process.env.CARHER_BASE ? { ids: null, moved: false } : await migrateIfReachable(d, key);
  if (mig.moved) { ids = mig.ids; logLine(`moved base ${base} -> ${mig.base}`); }
  else if (mig.why) logLine(`keep old base (新地址不通:${mig.why})`);
  try { if (!ids) ids = await fetchIds(base, key); }
  catch (e) { throw new Error(`没拉到模型列表(${e.message}),菜单没动`); }
  if (!ids.length) throw new Error("网关没返回任何模型,菜单没动");
  const r = applyModels(d, ids);
  const out = JSON.stringify(r.d, null, 2) + "\n";
  if (out !== raw) {
    if (raw.trim()) fs.writeFileSync(SETTINGS + ".bak-carher-refresh", raw, { mode: 0o600 });
    // 先写临时文件再改名:Claude Code 在监视这个文件,写到一半被它读到会当成坏 JSON
    const tmp = SETTINGS + ".carher-tmp-" + process.pid;
    fs.writeFileSync(tmp, out, { mode: 0o600 });
    fs.renameSync(tmp, SETTINGS);
    try { fs.chmodSync(SETTINGS, 0o600); } catch (e) { /* Windows */ }
  }
  try { fs.unlinkSync(path.join(CLAUDE_HOME, "cache", "gateway-models.json")); } catch (e) { /* 没有就算了 */ }
  let app = [];
  // App 那条只在「这一趟真从新地址拉到了列表」时才换地址(CARHER_MODELS 注入的不算真通)
  const newVerified = mig.moved || (base === NEW_BASE && process.env.CARHER_MODELS == null);
  try { app = syncAppModels(ids, key, newVerified ? NEW_BASE : null); } catch (e) { logLine("app sync failed " + e.message); }
  for (const a of app) if (a.changed) logLine("app menu " + a.models.join(",") + (a.baseMoved ? " base->" + NEW_BASE : ""));
  let keyFixed = false;
  try { keyFixed = approveApiKey(key, process.env.CLAUDE_CONFIG_DIR); } catch (e) { /* 写不了就算了,不影响菜单 */ }
  if (keyFixed) logLine("approved api key (was rejected/unanswered)");
  const n = r.d.modelPicker ? r.d.modelPicker.options.length : 0;
  logLine(`ok menu=${n} added=${r.added.join(",") || "-"} removed=${r.removed.join(",") || "-"}` +
    (r.modelReset ? ` reset=${r.modelReset}` : "") + (r.haikuSet ? ` haiku=${r.haikuSet}` : ""));
  say(`/model 菜单现在一共 ${n} 个模型,就是你的 key 能用来聊天的全部(画图/视频类不放进菜单)`);
  if (r.added.length) say(`新增 ${r.added.length}:${r.added.join(", ")}`);
  if (r.removed.length) say(`删除 ${r.removed.length}:${r.removed.join(", ")}`);
  if (!r.added.length && !r.removed.length) say("菜单和上次一样,没有变化");
  if (r.modelReset) say(`你之前选的 ${r.modelReset} 已经没有权限,已改回默认模型`);
  if (r.haikuSet) say(`后台小任务用的模型设为 ${r.haikuSet}(你的 key 里最新的 haiku)`);
  if (mig.moved) say(`网关地址已换成新地址 ${NEW_BASE}(同一个网关,换了域名)`);
  for (const a of app) {
    if (!a.changed) { say(`桌面 App 菜单和上次一样(${a.models.length} 个 Claude 模型)`); continue; }
    if (a.baseMoved) say(`桌面 App 的网关地址也换成了 ${NEW_BASE}`);
    if (a.menuChanged === false) { say("  ⚠️ 桌面 App 要**完全退出再打开**才会用新地址"); continue; }
    say(`桌面 App 菜单已更新为 ${a.models.length} 个 Claude 模型:${a.models.join(", ")}`);
    if (a.added && a.added.length) say(`  App 新增:${a.added.join(", ")}`);
    if (a.removed && a.removed.length) say(`  App 删除:${a.removed.join(", ")}`);
    say("  ⚠️ 桌面 App 要**完全退出再打开**才会显示新菜单(Mac:Cmd+Q;Windows:右下角托盘图标右键退出)");
  }
  if (keyFixed) say("已修好「Not logged in」:这把 key 之前被拒绝过,现在记成已同意");
  say("新开一个 Claude Code 会话(或桌面 App 新开对话)后 /model 里就是新列表");
}

module.exports = { pickerRows, applyModels, hookEntry, approveApiKey, syncAppModels, appConfigEntries, migrateIfReachable, isLegacyBase, NEW_BASE };

if (require.main === module) {
  main().catch((e) => {
    if (QUIET) { logLine("skip " + e.message); process.exit(0); }   // 后台自动刷:失败不打扰用户
    console.error("✗ " + e.message); process.exit(1);
  });
}
