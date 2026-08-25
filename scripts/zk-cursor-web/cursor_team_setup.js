#!/usr/bin/env node
/*
cursor_team_setup.js — 一条命令给同事装好 cursor-g(Cursor 3.16.x / macOS+Windows+Linux)。

═══ 零依赖原理 ═══
不需要 Python、不需要装 Node:Cursor 本体就是 Electron,自带完整 Node 运行时
(ELECTRON_RUN_AS_NODE=1)+ 内置 node:sqlite。本脚本用 Cursor 自己跑自己:
  macOS   : ./cursor_team_setup.sh  [--apply|--revert|...]
  Windows : cursor_team_setup.cmd   [--apply|--revert|...]
两个启动器只做一件事:找到 Cursor 可执行文件,以 node 模式运行本 JS。
安装目录/用户目录全部从 process.execPath 推导,不猜路径。

═══ 做的 6 件事(与 cursor_team_setup.py 完全同源,全幂等+自动备份+失败即拒) ═══
  1. 版本闸:只认 Cursor 3.16.x;不匹配拒绝(--force-version 自担风险)。
  2. Cursor GUI 必须已退出(排除本进程自身;外部写 state.vscdb 有内存覆盖竞态)。
  3. 两条 workbench bundle(desktop+glass)打 3 处解锁补丁 + 排队泵 v3:
     锚点=稳定语义地标+通用捕获,全文件恰好命中 1 次才动手;补完整 bundle 过
     `--check` 语法校验才落盘。旧版排队泵(v1/v2diag)在场则原地升级 v3。
  4. BYOK 配置并进 state.vscdb applicationUser blob(node:sqlite,读旧去重合并)。
  5. settings.json 写 "update.mode":"none"(自动升级会掀翻所有补丁)。
  6. 打印唯一人工步:Cursor 设置里粘 key。

用法(不带参数=dry-run 只检查不写):
  --apply / --revert / --base-url <url> / --models a,b,c / --force-version
回滚备份在 ~/.cursor-team-setup-backup(与 Python 版同格式,互相可 revert)。
⚠️ 装过 CursorX 的机器:3 解锁锚点命中 0 → 自动拒绝(防重复打),先还原 pristine bundle。
*/
"use strict";
const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawnSync } = require("child_process");

/* ── 平台路径推导(全部从运行时自身出发,不猜安装位置) ── */
const EXEC = process.execPath; // = Cursor 可执行文件(node 模式跑在它身上)
function appRoot() {
  if (process.env.CURSOR_APP_ROOT) return process.env.CURSOR_APP_ROOT; // 测试钩子
  const d = path.dirname(EXEC);
  if (process.platform === "darwin") return path.join(d, "..", "Resources", "app");
  return path.join(d, "resources", "app"); // win32 / linux
}
function userDir() {
  if (process.env.CURSOR_USER_DIR) return process.env.CURSOR_USER_DIR; // 测试钩子
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Application Support", "Cursor", "User");
  if (process.platform === "win32") return path.join(process.env.APPDATA || "", "Cursor", "User");
  return path.join(os.homedir(), ".config", "Cursor", "User");
}
const RES = appRoot();
const BUNDLES = [
  path.join("out", "vs", "workbench", "workbench.desktop.main.js"),
  path.join("out", "vs", "workbench", "workbench.glass.main.js"),
];
const STATE_DB = path.join(userDir(), "globalStorage", "state.vscdb");
const SETTINGS_JSON = path.join(userDir(), "settings.json");
const APP_USER_KEY = "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl" +
  ".persistentStorage.applicationUser";
const BACKUP_ROOT = path.join(os.homedir(), ".cursor-team-setup-backup");

const SUPPORTED_MAJOR_MINOR = "3.16";
const DEFAULT_BASE_URL = "https://cc.auto-link.com.cn/pro/v1";
const DEFAULT_MODELS = [
  "cursor-g-5.6-sol", "cursor-g-5.6-sol-high", "cursor-g-5.6-luna",
  "cursor-g-5.6-pro", "cursor-g-5.6-instant", "cursor-g-5.5",
];

/* ── bundle 补丁常量(与 Python 版逐字节一致,有等价断言测试守着) ── */
const GATE_FN = "(function(c){if(!c)return c;const o={...c};" +
  "if(o.namedModelsViewConfig){o.namedModelsViewConfig={...o.namedModelsViewConfig};" +
  "delete o.namedModelsViewConfig.namedViewToRoutedModelViewButton;" +
  "delete o.namedModelsViewConfig.namedViewToRoutedModelViewToggle;" +
  "delete o.namedModelsViewConfig.namedViewToRoutedModelViewNoButton;}" +
  "if(o.routedModelViewConfig){o.routedModelViewConfig={...o.routedModelViewConfig};" +
  "delete o.routedModelViewConfig.routedModelViewToNamedViewButton;" +
  "delete o.routedModelViewConfig.routedModelViewToNamedViewToggle;" +
  "o.routedModelViewConfig.hideRoutedModelView=false;}return o;})";

const QP_MARKER = "@cx-queue-pump:v3";
const QP_ANCHOR = /(addToQueue\((\w+)\)\{if\(!this\.isValidQueueItem\(\2\)\)return;)/g;
const QP_SNIPPET =
  "/*" + QP_MARKER + "*/try{if(this._cxQP===void 0){let _cxN=0,_cxS=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){' +
  "const _u=_d.chatGenerationUUID;" +
  "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;" +
  'const _stale=_u===void 0||(_m&&typeof _m.has==="function"&&!_m.has(_u));' +
  "_cxS=_stale?_cxS+1:0;" +
  "if(_u===void 0||_cxS>=3){" +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",chatGenerationUUID:void 0,generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}' +
  "else{_cxS=0}" +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0&&++_cxN<120){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}";

// 旧版排队泵原文(逐字节),在场则原地升级 v3
const QP_OLD = [
  "/*@cx-queue-pump:v1*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'if(_d&&_d.status==="generating"&&_d.chatGenerationUUID===void 0&&(_d.generatingBubbleIds??[]).length===0){' +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId})}catch(_e){}}' +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0&&++_cxN<60){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
  "/*@cx-queue-diag:v2*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'try{this.structuredLogService.info("composer","[cx-queue-diag] tick",' +
  "{composerId:this.composerId,n:_cxN,queueLen:this.getQueueItems().length," +
  "status:(_d&&_d.status)||null,hasUUID:!!(_d&&_d.chatGenerationUUID)," +
  "bubbleIds:((_d&&_d.generatingBubbleIds)||[]).length})}catch(_e){}" +
  'if(_d&&_d.status==="generating"&&_d.chatGenerationUUID===void 0&&(_d.generatingBubbleIds??[]).length===0){' +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-diag] healed stuck generating status",{composerId:this.composerId})}catch(_e){}}' +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0&&++_cxN<120){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
];

const PATCHES = [
  {
    name: "gate", marker: "@cxteam-gate",
    rx: /(modelPickerDisplayConfiguration\?\?\w+;return )(\w+\([a-z]\))(\}resolveModelNameToCatalog)/g,
    sub: (m) => m[1] + "/*@cxteam-gate*/" + GATE_FN + "(" + m[2] + ")" + m[3],
  },
  {
    name: "localagent", marker: "@cxteam-localagent",
    rx: /(clientSupportsRoutedModelUpdate:!0\};if\()(\w+\.localMode)(\)\{try\{)/g,
    sub: (m) => m[1] + "/*@cxteam-localagent*/!0" + m[3],
  },
  {
    name: "dedicated", marker: "@cxteam-dedicated",
    rx: /(\w+\(this\.storageService,"useDedicatedLocalAgentRuntimeHost"\))(\?await this\.runLocalAgentInDedicatedExtensionHost\()/g,
    sub: (m) => "(/*@cxteam-dedicated*/!1)" + m[2],
  },
  { name: "queue-pump", marker: QP_MARKER, rx: QP_ANCHOR, sub: (m) => m[1] + QP_SNIPPET },
];

/* ── 测试钩子:打印常量供与 Python 版做逐字节等价断言 ── */
if (process.env.CX_DUMP_CONSTANTS) {
  process.stdout.write(JSON.stringify({ GATE_FN, QP_SNIPPET, QP_OLD, DEFAULT_BASE_URL, DEFAULT_MODELS }));
  process.exit(0);
}
// 测试钩子:对任意文件跑真实 PATCHES(每锚点须命中 1 次),写出到 CX_APPLY_OUT,供与 Python 版做逐字节等价断言
if (process.env.CX_APPLY_TO_FILE) {
  let t = fs.readFileSync(process.env.CX_APPLY_TO_FILE, "utf8");
  for (const pt of PATCHES) {
    const hits = countMatches(pt.rx, t);
    if (hits !== 1) { console.error("锚点 " + pt.name + " 命中=" + hits); process.exit(11); }
    t = subOnce(pt.rx, t, pt.sub);
  }
  fs.writeFileSync(process.env.CX_APPLY_OUT || (process.env.CX_APPLY_TO_FILE + ".jsout"), t);
  process.exit(0);
}

/* ── 工具函数 ── */
function cursorVersion() {
  try { return JSON.parse(fs.readFileSync(path.join(RES, "package.json"), "utf8")).version || "unknown"; }
  catch (e) { return "unknown"; }
}

function cursorRunning() {
  // 本脚本自己就跑在 Cursor 二进制上,必须排除自身 pid
  if (process.platform === "win32") {
    const r = spawnSync("tasklist", ["/FI", "IMAGENAME eq Cursor.exe", "/FO", "CSV", "/NH"], { encoding: "utf8" });
    if (r.status !== 0 || !r.stdout) return false;
    return r.stdout.split(/\r?\n/).some((line) => {
      const m = line.match(/^"Cursor\.exe","(\d+)"/i);
      return m && Number(m[1]) !== process.pid;
    });
  }
  const pat = process.platform === "darwin" ? "Cursor.app/Contents/MacOS/Cursor" : path.join(path.dirname(EXEC), "cursor");
  const r = spawnSync("pgrep", ["-f", pat], { encoding: "utf8" });
  if (r.status !== 0 || !r.stdout) return false;
  return r.stdout.split(/\s+/).filter(Boolean).some((p) => Number(p) !== process.pid);
}

function syntaxCheck(text, tag) {
  const p = path.join(os.tmpdir(), "cxteam_check_" + tag + ".js");
  fs.writeFileSync(p, text);
  const r = spawnSync(EXEC, ["--check", p], { encoding: "utf8", env: { ...process.env, ELECTRON_RUN_AS_NODE: "1" } });
  fs.unlinkSync(p);
  if (r.status !== 0) { console.log("   !! 语法校验失败(%s):%s", tag, String(r.stderr).slice(0, 300)); return false; }
  return true;
}

function countMatches(rx, text) { rx.lastIndex = 0; let n = 0; while (rx.exec(text)) n++; rx.lastIndex = 0; return n; }
function subOnce(rx, text, fn) {
  rx.lastIndex = 0;
  const m = rx.exec(text); rx.lastIndex = 0;
  if (!m) return text;
  return text.slice(0, m.index) + fn(m) + text.slice(m.index + m[0].length);
}

function planBundle(rel) {
  const p = path.join(RES, rel);
  const src = fs.readFileSync(p, "utf8");
  let out = src; const applied = [];
  for (const pt of PATCHES) {
    if (out.includes(pt.marker)) { console.log("   SKIP %s(已打过)", pt.name.padEnd(11)); continue; }
    if (pt.name === "queue-pump") {
      const old = QP_OLD.find((s) => out.includes(s));
      if (old) {
        if (out.split(old).length - 1 !== 1) { console.log("   !! queue-pump 旧版命中!=1 → 拒绝"); process.exit(2); }
        out = out.replace(old, QP_SNIPPET); applied.push("queue-pump(升级)"); continue;
      }
    }
    const hits = countMatches(pt.rx, out);
    if (hits !== 1) {
      console.log("   !! %s 锚点命中=%d != 1 → 拒绝动手(版本不匹配或已被 CursorX 改写)", pt.name.padEnd(11), hits);
      process.exit(2);
    }
    out = subOnce(pt.rx, out, pt.sub);
    applied.push(pt.name);
  }
  if (out === src) { console.log("   %s: 全部已打过,跳过", path.basename(rel)); return null; }
  console.log("   %s: 将打 [%s]", path.basename(rel), applied.join(", "));
  return { p, out };
}

/* ── SQLite:优先 node:sqlite,备胎 Cursor 自带 @vscode/sqlite3 ── */
function openDb() {
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(STATE_DB);
    return {
      get: (k) => { const r = db.prepare("SELECT value FROM ItemTable WHERE key=?").get(k); return r ? r.value : null; },
      set: (k, v) => db.prepare("UPDATE ItemTable SET value=? WHERE key=?").run(v, k),
      close: () => db.close(),
    };
  } catch (e) { /* 落到备胎 */ }
  const sqlite3 = require(path.join(RES, "node_modules", "@vscode", "sqlite3"));
  const db = new sqlite3.Database(STATE_DB);
  const call = (fn, sql, args) => new Promise((res, rej) => db[fn](sql, args, function (err, row) { err ? rej(err) : res(row); }));
  return {
    get: (k) => call("get", "SELECT value FROM ItemTable WHERE key=?", [k]).then((r) => (r ? r.value : null)),
    set: (k, v) => call("run", "UPDATE ItemTable SET value=? WHERE key=?", [v, k]),
    close: () => db.close(),
  };
}

async function mergeConfig(args, dry) {
  const db = openDb();
  const raw = await db.get(APP_USER_KEY);
  if (!raw) { console.log("   !! applicationUser blob 不存在(Cursor 没初始化过?)"); db.close(); process.exit(3); }
  const d = JSON.parse(raw);
  const before = { baseUrl: d.openAIBaseUrl, useKey: d.useOpenAIKey };
  d.openAIBaseUrl = args.baseUrl;
  d.useOpenAIKey = true;
  const ai = (d.aiSettings = d.aiSettings || {});
  const dedup = (existing) => {
    const seen = new Set(), outl = [];
    for (const x of [...(existing || []), ...args.models]) if (typeof x === "string" && !seen.has(x)) { seen.add(x); outl.push(x); }
    return outl;
  };
  const uamBefore = [...(ai.userAddedModels || [])];
  ai.userAddedModels = dedup(ai.userAddedModels);
  ai.modelOverrideEnabled = dedup(ai.modelOverrideEnabled);
  const added = args.models.filter((m) => !uamBefore.includes(m));
  console.log("   config diff: baseUrl %j -> %j ; useOpenAIKey %j -> true ; +models %s",
    before.baseUrl, args.baseUrl, before.useKey, added.length ? added.join(",") : "(none, all present)");
  if (!dry) await db.set(APP_USER_KEY, JSON.stringify(d));
  db.close();
  return raw; // 旧 blob 用于备份
}

function setUpdateNone(dry) {
  let s = {};
  if (fs.existsSync(SETTINGS_JSON)) {
    try { s = JSON.parse(fs.readFileSync(SETTINGS_JSON, "utf8") || "{}"); }
    catch (e) { console.log('   !! settings.json 不是纯 JSON(含注释?)→ 跳过自动写,请手动加 "update.mode":"none"'); return; }
  }
  const old = s["update.mode"];
  if (old === "none") { console.log("   update.mode 已是 none"); return; }
  console.log("   update.mode %j -> none", old === undefined ? null : old);
  if (!dry) { s["update.mode"] = "none"; fs.writeFileSync(SETTINGS_JSON, JSON.stringify(s, null, 2)); }
}

function ts() { const d = new Date(), z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${z(d.getMonth() + 1)}${z(d.getDate())}-${z(d.getHours())}${z(d.getMinutes())}${z(d.getSeconds())}`; }

function doBackup(ver, plans, oldBlob) {
  const bdir = path.join(BACKUP_ROOT, `${ver}-${ts()}`);
  fs.mkdirSync(bdir, { recursive: true });
  for (const { p } of plans) fs.copyFileSync(p, path.join(bdir, path.basename(p)));
  fs.writeFileSync(path.join(bdir, "applicationUser.blob.json"), oldBlob);
  if (fs.existsSync(SETTINGS_JSON)) fs.copyFileSync(SETTINGS_JSON, path.join(bdir, "settings.json"));
  console.log("   备份 ->", bdir);
}

async function revert() {
  const baks = fs.existsSync(BACKUP_ROOT) ? fs.readdirSync(BACKUP_ROOT).sort() : [];
  if (!baks.length) { console.log("!! 无备份可回滚"); process.exit(1); }
  // 优先选「含 bundle 的最新备份」(跳过 -cfgonly:那种只存了配置,回滚它会漏掉 bundle);都没有再退回最新
  const hasBundle = (d) => BUNDLES.some((rel) => fs.existsSync(path.join(BACKUP_ROOT, d, path.basename(rel))));
  const pick = [...baks].reverse().find(hasBundle) || baks[baks.length - 1];
  const b = path.join(BACKUP_ROOT, pick);
  console.log("从备份回滚:", b);
  for (const rel of BUNDLES) {
    const src = path.join(b, path.basename(rel));
    if (fs.existsSync(src)) { fs.copyFileSync(src, path.join(RES, rel)); console.log("  restored bundle:", path.basename(rel)); }
  }
  const blob = path.join(b, "applicationUser.blob.json");
  if (fs.existsSync(blob)) {
    const db = openDb();
    await db.set(APP_USER_KEY, fs.readFileSync(blob, "utf8"));
    db.close(); console.log("  restored applicationUser blob");
  }
  const sj = path.join(b, "settings.json");
  if (fs.existsSync(sj)) { fs.copyFileSync(sj, SETTINGS_JSON); console.log("  restored settings.json"); }
  console.log("回滚完成,重启 Cursor 生效。");
}

/* ── main ── */
async function main() {
  const argv = process.argv.slice(2);
  const has = (f) => argv.includes(f);
  const opt = (f, dflt) => { const i = argv.indexOf(f); return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt; };
  const args = {
    apply: has("--apply"), revert: has("--revert"), forceVersion: has("--force-version"),
    baseUrl: opt("--base-url", DEFAULT_BASE_URL),
    models: opt("--models", DEFAULT_MODELS.join(",")).split(",").map((s) => s.trim()).filter(Boolean),
  };

  if (!fs.existsSync(RES)) { console.log("!! 找不到 Cursor 资源目录:", RES); process.exit(1); }
  const ver = cursorVersion();
  console.log("Cursor version:", ver, "| platform:", process.platform, "| runtime:", process.version);

  if (args.revert) return revert();

  if (!ver.startsWith(SUPPORTED_MAJOR_MINOR + ".") && !args.forceVersion) {
    console.log("!! 只支持 Cursor %s.x;当前 %s。加 --force-version 自担风险,或等适配。", SUPPORTED_MAJOR_MINOR, ver);
    process.exit(2);
  }
  if (!process.env.CX_SKIP_RUNNING_CHECK && cursorRunning()) {
    console.log("!! Cursor 正在运行 —— 请先完全退出(mac ⌘Q / Windows 右键托盘图标退出)再跑。");
    process.exit(2);
  }

  console.log("--- 1) bundle 补丁计划 ---");
  const plans = [];
  for (const rel of BUNDLES) { const r = planBundle(rel); if (r) plans.push(r); }
  console.log("--- 2) 语法校验补后 bundle ---");
  for (const { p, out } of plans) {
    if (!syntaxCheck(out, path.basename(p).split(".")[1])) { console.log("   !! 语法校验不过,终止,未落任何盘。"); process.exit(4); }
    console.log("   syntax OK:", path.basename(p));
  }
  console.log("--- 3) BYOK 配置差异 ---");
  const oldBlob = await mergeConfig(args, !args.apply);
  console.log("--- 4) update.mode ---");
  setUpdateNone(!args.apply);

  if (!args.apply) { console.log("\n[dry-run] 以上全部通过。加 --apply 执行(会先备份到 %s)。", BACKUP_ROOT); return; }

  console.log("--- 落盘 ---");
  if (plans.length) {
    doBackup(ver, plans, oldBlob);
    for (const { p, out } of plans) { fs.writeFileSync(p, out); console.log("   patched:", path.basename(p)); }
  } else {
    const bdir = path.join(BACKUP_ROOT, `${ver}-${ts()}-cfgonly`);
    fs.mkdirSync(bdir, { recursive: true });
    fs.writeFileSync(path.join(bdir, "applicationUser.blob.json"), oldBlob);
    console.log("   备份(仅配置)->", bdir);
  }

  console.log("\n✅ 完成。还差一步(只此一步,key 是系统加密存储的,交给 Cursor 自己):");
  console.log("   1. 启动 Cursor → Settings → Models → OpenAI API Key,粘贴你的 key,点 Verify。");
  console.log("      (base-url、模型列表、开关都已预填好,你只需粘 key。)");
  console.log("   2. 就绪。菜单里选 cursor-g-5.6-sol 等即可用。");
  console.log("   回滚整包:启动器加 --revert");
}

main().catch((e) => { console.error("!! 未预期错误:", e && e.message ? e.message : e); process.exit(9); });
