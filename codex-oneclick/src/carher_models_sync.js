#!/usr/bin/env node
/* 把这把 key 在 CarHer 网关上能用的全部模型同步进 Codex 的模型选择器 —— Node 版。

   为什么有两份:Python 版(carher_models_sync.py)是原版;10-16 同事 Windows 上没有 Python ⇒
   同步从没跑过 ⇒ Codex App 下拉里只有它自带的几个 GPT,他 key 里唯一的 deepseek 找不到。
   一键脚本本来就会装 Node(codex 命令行要),所以没有 Python 时改用这一份。
   🔴 两份逻辑必须一致:tests/test_models_sync_js.py 用同一组输入逐项比对两边的输出。

   只用 Node 自带模块(Node ≥ 18)。流程与 Python 版相同:
     1. GET {base_url}/models —— 网关已按 key 的白名单过滤
     2. 模板取本机 codex 自带的目录(codex debug models --bundled)
     3. 生成新目录 → 用 codex 自己解析一遍 → 通过才原子替换
     4. config.toml 里没有 model_catalog_json 就补一行
   任何一步失败都不动现有文件。 */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const CODEX_HOME = process.env.CODEX_HOME || path.join(os.homedir(), ".codex");
const CONFIG = path.join(CODEX_HOME, "config.toml");
const DEFAULT_CATALOG = path.join(CODEX_HOME, "carher-models.json");
const TEMPLATE_SLUG = "gpt-5.5";   // 非 OpenAI 模型套 gpt-5.5 的元数据(普通函数调用),理由见 Python 版
const SKIP = /image|imagine|video|music|^codex-auto-review$/;   // 出图/视频/音乐生成名不能对话
const PREFERRED = "gpt-5.6-terra";   // 与一键脚本的默认模型一致
const LOG_FILE = path.join(CODEX_HOME, "carher-models-sync.log");

function pad(n) { return String(n).padStart(2, "0"); }
function log(msg) {
  const line = `[carher-models] ${msg}`;
  try { console.log(line); } catch (e) { /* 定时任务里没有控制台 */ }
  const d = new Date();
  const ts = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  try { fs.appendFileSync(LOG_FILE, `${ts} ${line}\n`, "utf8"); } catch (e) { /* 写不了日志不影响同步 */ }
}

function readCredentials() {
  let key = "";
  try { key = JSON.parse(fs.readFileSync(path.join(CODEX_HOME, "auth.json"), "utf8")).OPENAI_API_KEY || ""; } catch (e) {}
  const m = /^\s*base_url\s*=\s*"([^"]+)"/m.exec(fs.readFileSync(CONFIG, "utf8"));
  if (!key || !m) throw new Error("auth.json 里没有 key 或 config.toml 里没有 base_url，先跑一键配置脚本");
  return [key, m[1].replace(/\/+$/, "")];
}

async function getJson(url, key) {
  const r = await fetch(url, { headers: { Authorization: `Bearer ${key}` }, signal: AbortSignal.timeout(30000) });
  if (!r.ok) throw new Error(`${url} HTTP ${r.status}`);
  return r.json();
}

async function fetchModelIds(key, base) {
  const data = (await getJson(`${base}/models`, key)).data || [];
  const ids = [...new Set(data.filter((x) => x && typeof x === "object" && x.id).map((x) => x.id))];
  return ids.sort((a, b) => { const x = a.toLowerCase(), y = b.toLowerCase(); return x < y ? -1 : x > y ? 1 : 0; });
}

async function fetchInputLimits(key, base) {
  const limits = {};
  let rows;
  try { rows = (await getJson(`${base}/model/info`, key)).data || []; }
  catch (e) { log(`读不到模型上下文上限，沿用默认值：${e.message}`); return limits; }
  for (const row of rows) {
    const name = row && typeof row === "object" ? row.model_name : null;
    const cap = name ? (row.model_info || {}).max_input_tokens : null;
    if (Number.isInteger(cap) && cap > 0) limits[name] = Math.min(cap, limits[name] === undefined ? cap : limits[name]);
  }
  return limits;
}

/* 本机所有 codex 候选,按优先级:[可执行文件, 前置参数]。npm 装的 codex 是 bin/codex.js,用当前 node 直接跑它:
   🔴 Windows 上 spawn codex.cmd 不带 shell 会被 Node 拒(EINVAL),带 shell 又要过 cmd 的引号转义。
   🔴 10-21 guozhenhua:一键装的 0.163 在 ~/.carher-npm-global/lib,但原来先查 /opt/homebrew/lib ⇒ 抓到他以前
      自己装的老 codex(没有 `debug models` 子命令)⇒ 模板拿不到 ⇒ 同步失败,下拉里只有自带 GPT。
      ⇒ 一键装的那份排第一;调用失败就换下一个(loadTemplates)。 */
function codexCandidates() {
  if (process.env.CARHER_CODEX === "none") return [];   // 测试用
  const js = (prefix) => path.join(prefix, "node_modules", "@openai", "codex", "bin", "codex.js");
  const out = [], seen = new Set();
  const add = (c) => { const k = c[1][0] || c[0]; if (!seen.has(k)) { seen.add(k); out.push(c); } };
  const prefixes = [path.join(os.homedir(), ".carher-npm-global", "lib"), path.join(os.homedir(), ".carher-npm-global")];
  if (process.env.APPDATA) prefixes.push(path.join(process.env.APPDATA, "npm"));
  prefixes.push(...(process.env.CARHER_CODEX_SYS_PREFIXES ? process.env.CARHER_CODEX_SYS_PREFIXES.split(path.delimiter) : ["/opt/homebrew/lib", "/usr/local/lib"]));   // 环境变量仅测试用
  for (const p of prefixes) if (fs.existsSync(js(p))) add([process.execPath, [fs.realpathSync(js(p))]]);
  // PATH 里的 codex:posix 上多半是指向 codex.js 的软链
  const which = process.platform === "win32" ? null : cp.spawnSync("sh", ["-c", "command -v codex"], { encoding: "utf8" }).stdout.trim();
  if (which) {
    let real = which; try { real = fs.realpathSync(which); } catch (e) {}
    add(real.endsWith(".js") ? [process.execPath, [real]] : [real, []]);
  }
  for (const c of ["/Applications/Codex.app/Contents/Resources/codex", "/Applications/OpenAI Codex.app/Contents/Resources/codex"])
    if (fs.existsSync(c)) add([c, []]);
  return out;
}

function runCodex(codex, args) {
  const [bin, pre] = codex;
  return cp.spawnSync(bin, [...pre, "debug", "models", ...args], {
    encoding: "utf8", timeout: 60000, env: Object.assign({}, process.env, { CODEX_HOME }), windowsHide: true,
  });
}

/* 依次试候选,第一个能吐出自带目录的就用它(也用它校验新目录)。返回 {codex, models}。 */
function loadTemplates(cands, current) {
  const why = [];
  for (const c of cands) {
    const r = runCodex(c, ["--bundled"]);
    if (r.status === 0) {
      try { return { codex: c, models: JSON.parse(r.stdout).models }; } catch (e) { why.push(`${c[1][0] || c[0]}:输出不是 JSON`); continue; }
    }
    why.push(`${c[1][0] || c[0]}:${String(r.stderr || r.error || "").trim().split("\n")[0].slice(0, 120)}`);
  }
  if (why.length) log(`这些 codex 拿不到自带目录(版本太老?):${why.join(" | ")}`);
  if (fs.existsSync(current)) return { codex: null, models: JSON.parse(fs.readFileSync(current, "utf8")).models };
  throw new Error(cands.length ? "本机的 codex 都太老(没有 debug models),也没有现有目录可当模板:重跑一次 codex-mac.command 装新版"
                               : "找不到 codex，也没有现有目录可当模板");
}

const clone = (o) => JSON.parse(JSON.stringify(o));

function buildCatalog(ids, templates, limits) {
  const bySlug = new Map();
  for (const m of templates) if (m.slug && !bySlug.has(m.slug)) bySlug.set(m.slug, m);
  const base = bySlug.get(TEMPLATE_SLUG) || bySlug.values().next().value;
  const wanted = ids.filter((i) => !SKIP.test(i));
  const out = [];
  for (const [slug, m0] of bySlug) {
    const m = clone(m0);
    m.visibility = wanted.includes(slug) ? "list" : "hide";
    out.push(m);
  }
  for (const slug of wanted) {
    if (bySlug.has(slug)) continue;
    const m = clone(base);
    Object.assign(m, { slug, display_name: slug, description: `CarHer 网关模型：${slug}`, visibility: "list", supported_in_api: true });
    Object.assign(m, { tool_mode: null, use_responses_lite: false, multi_agent_version: null, additional_speed_tiers: [], service_tiers: [] });
    m.apply_patch_tool_type = null;   // 非 OpenAI 模型写不出 freeform patch(10-09 实测)
    delete m.upgrade;
    delete m.availability_nux;
    out.push(m);
  }
  out.forEach((m, i) => {
    m.priority = i + 1;
    const cap = limits[m.slug];
    if (cap) for (const f of ["context_window", "max_context_window"])
      if (Number.isInteger(m[f]) && m[f] > cap) m[f] = cap;   // 只往下压,不放大
  });
  return { models: out };
}

/* config 里钉着的 model 已不在这把 key 里 ⇒ 每一轮都 403。换成 PREFERRED(在的话)或列表第一个。
   返回新文本;不用改返回 null。只动顶层 `model = "…"` 那一行(任何 [table] 之前)。 */
function fixDefaultModel(text, shown, preferred, ourProvider) {
  const first = /^\[/m.exec(text);
  const head = first ? text.slice(0, first.index) : text;
  const m = /^(\s*model\s*=\s*)"([^"]*)"/m.exec(head);
  if (!m || !shown.length || shown.includes(m[2])) return null;
  // 只在顶层 provider 是我们网关时才动:用户切到官方登录等别的 provider,他的 model 不归这把 key 管
  const pv = /^\s*model_provider\s*=\s*"([^"]+)"/m.exec(head);
  if (!pv || pv[1] !== ourProvider) return null;
  const next = shown.includes(preferred) ? preferred : shown[0];
  return { text: text.slice(0, m.index) + m[1] + JSON.stringify(next) + text.slice(m.index + m[0].length), from: m[2], to: next };
}

/* config.toml 里 base_url 等于这次同步所用网关的那个 [model_providers.X] 的名字 X(没有返回 null)。 */
function ourProviderName(text, base) {
  const re = /^\[model_providers\.([^\]\s]+)\]\s*$([\s\S]*?)(?=^\[|(?![\s\S]))/gm;
  let m;
  while ((m = re.exec(text))) {
    const bu = /^\s*base_url\s*=\s*"([^"]+)"/m.exec(m[2]);
    if (bu && bu[1].replace(/\/+$/, "") === String(base).replace(/\/+$/, "")) return m[1];
  }
  return null;
}

function catalogPathFromConfig(text) {
  const m = /^\s*model_catalog_json\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')/m.exec(text);
  if (!m) return null;
  const raw = m[1];
  const v = raw.startsWith('"') ? JSON.parse(raw) : raw.slice(1, -1);
  return v.startsWith("~") ? path.join(os.homedir(), v.slice(1)) : v;
}

function ensureConfigLine(text, p) {
  const line = `model_catalog_json = ${JSON.stringify(String(p))}\n`;
  const first = /^\[/m.exec(text);   // 顶层键必须在任何 [table] 之前
  if (!first) return text.replace(/\n+$/, "") + "\n" + line;
  return text.slice(0, first.index) + line + "\n" + text.slice(first.index);
}

async function main() {
  const [key, base] = readCredentials();
  const ids = await fetchModelIds(key, base);
  if (!ids.length) throw new Error("网关没返回任何模型，保持现有目录不变");
  const configText = fs.readFileSync(CONFIG, "utf8");
  const target = catalogPathFromConfig(configText) || DEFAULT_CATALOG;
  const tpl = loadTemplates(codexCandidates(), target);
  const codex = tpl.codex;
  const catalog = buildCatalog(ids, tpl.models, await fetchInputLimits(key, base));
  let before = [];
  try { before = JSON.parse(fs.readFileSync(target, "utf8")).models.filter((m) => m.visibility === "list").map((m) => m.slug); } catch (e) {}

  const tmp = path.join(CODEX_HOME, `.carher-models-${process.pid}-${Date.now()}.json`);
  fs.writeFileSync(tmp, JSON.stringify(catalog, null, 1), "utf8");
  try {
    if (codex) {   // 让 codex 自己解析一遍,通不过就不替换
      const r = runCodex(codex, ["-c", `model_catalog_json=${tmp}`]);
      if (r.status !== 0) throw new Error(`codex 解析新目录失败，未替换：${String(r.stderr || r.error || "").trim().slice(0, 300)}`);
    }
    if (fs.existsSync(target)) fs.copyFileSync(target, target + ".bak");
    fs.renameSync(tmp, target);
  } finally {
    try { fs.unlinkSync(tmp); } catch (e) {}
  }
  if (catalogPathFromConfig(configText) === null) {
    fs.writeFileSync(CONFIG, ensureConfigLine(configText, target), "utf8");
    log(`已在 config.toml 加 model_catalog_json -> ${target}`);
  }
  const shown = catalog.models.filter((m) => m.visibility === "list").map((m) => m.slug);
  log(`同步完成：选择器 ${shown.length} 个模型（网关可见 ${ids.length} 个）-> ${target}`);
  const added = shown.filter((x) => !before.includes(x)), removed = before.filter((x) => !shown.includes(x));
  if (before.length && added.length) log(`新增 ${added.length}：${added.join(", ")}`);
  if (before.length && removed.length) log(`删除 ${removed.length}：${removed.join(", ")}`);
  if (before.length && !added.length && !removed.length) log("和上次一样，没有变化");
  const ours = ourProviderName(configText, base);
  for (const f of [CONFIG, path.join(CODEX_HOME, "chatgpt.config.toml")]) {
    let t; try { t = fs.readFileSync(f, "utf8"); } catch (e) { continue; }
    const r = fixDefaultModel(t, shown, PREFERRED, ours);
    if (r) { fs.writeFileSync(f, r.text, "utf8"); log(`默认模型 ${r.from} 你的 key 已不能用，改成 ${r.to}（${path.basename(f)}）`); }
  }
  log("Codex App 要完全退出重开才会看到新模型");
  return 0;
}

module.exports = { buildCatalog, catalogPathFromConfig, ensureConfigLine, fixDefaultModel, ourProviderName, codexCandidates, loadTemplates };

if (require.main === module) {
  try { if (fs.existsSync(LOG_FILE) && fs.statSync(LOG_FILE).size > 512 * 1024) fs.writeFileSync(LOG_FILE, ""); } catch (e) {}
  main().then((rc) => process.exit(rc), (e) => { log(`失败：${e.message}`); process.exit(1); });
}
