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

═══ 做的事(全幂等+自动备份+失败即拒) ═══
  1. 软版本闸:按 Cursor 3.16.x 设计,别的版本只告警不拒;真正安全阀=bundle 锚点必须
     exactly-1(结构变了认不出 → 自动拒绝,绝不改坏)。
  2. Cursor GUI 必须已退出(排除本进程自身;外部写 state.vscdb 有内存覆盖竞态)。
  3. 两条 workbench bundle(desktop+glass)打 3 处解锁补丁 + 排队泵 v3:
     锚点=稳定语义地标+通用捕获,全文件恰好命中 1 次才动手;补完整 bundle 过
     `--check` 语法校验才落盘。旧版排队泵(v1/v2diag)在场则原地升级 v3。
  4. BYOK 配置并进 state.vscdb applicationUser blob(node:sqlite,读旧去重合并):
     base-url、useOpenAIKey、6 个 cursor-g 模型,并把默认选中模型设为 cursor-g-5.6-sol。
  5. #2 写 Key:mac 用 in-process keytar 读钥匙串主密码 + OSCrypt(AES-128-CBC)加密写
     secret://cursorAuth/openAIKey。写前两道自检(能解开现有 Key + 加密回环)才落盘,
     否则回退手填(防假绿 401)。win=DPAPI 未实测→回退手填;linux=best-effort。
  6. 默认允许 Cursor 自动升级(升级后失效跑 --repair 一键重打补丁);--pin-update 才锁不升级。

用法(不带参数=dry-run 只检查不写):
  --apply            执行(末尾会提示粘一次 Key;可用 --key <k> 无人值守)
  --repair           只重打 bundle 补丁(Cursor 升级后失效用;不碰配置/Key)
  --revert           从最近备份回滚(bundle+blob+settings+Key secret)
  --key <k>          直接给 Key(CI/无人值守;否则 TTY 下交互粘)
  --pin-update       写 "update.mode":"none" 锁定不升级(默认不写)
  --base-url <url> / --models a,b,c / --force-version
回滚备份在 ~/.cursor-team-setup-backup(与 Python 版同格式,互相可 revert)。
⚠️ 装过 CursorX 的机器:3 解锁锚点命中 0 → 自动拒绝(防重复打),先还原 pristine bundle。
*/
"use strict";
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");
const readline = require("readline");
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
const DEFAULT_MODEL = "cursor-g-5.6-sol"; // 装完直接选中它,用户不用在菜单里挑
const OPENAI_KEY_SECRET = "secret://cursorAuth/openAIKey"; // Cursor 存 BYOK Key 的 secret 行(OSCrypt 密文)

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

const QP_MARKER = "@cx-queue-pump:v3.4";
const QP_ANCHOR = /(addToQueue\((\w+)\)\{if\(!this\.isValidQueueItem\(\2\)\)return;)/g;
// v3.4 修 v3.3 误杀(折叠/漏答真凶):官方派发起跑窗口(2-3s)status=generating 但 uuid
// 未登记,旧"无 uuid 立刻 heal"把起跑轮误判僵尸→heal→派发下一条→掐死起跑轮→两问挤一轮。
// 修:①官方在飞标记 inFlightDispatchItemIds 非空=起跑中,绝不判僵尸;②一律 3 tick 确认。
// 其余同 v3.3:队列非空就守(无寿命上限),派发全走官方 tryDispatchNextQueueItem()。
const QP_SNIPPET =
  "/*" + QP_MARKER + "*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){' +
  "const _u=_d.chatGenerationUUID;" +
  "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;" +
  "const _fl=this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0;" +
  'const _stale=!_fl&&(_u===void 0||(_m&&typeof _m.has==="function"&&!_m.has(_u)));' +
  "_cxS=_stale?_cxS+1:0;" +
  "if(_cxS>=3){" +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",chatGenerationUUID:void 0,generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}' +
  "else{_cxS=0}" +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}";

// 旧版排队泵原文(逐字节),在场则原地升级 v3.4
const QP_OLD = [
  "/*@cx-queue-pump:v3.3*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{" +
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
  "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
  "/*@cx-queue-pump:v3*/try{if(this._cxQP===void 0){let _cxN=0,_cxS=0;const _cxT=()=>{" +
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
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
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

/* ── SQLite:优先 node:sqlite,备胎 Cursor 自带 @vscode/sqlite3 ──
   set 用 UPSERT(INSERT OR REPLACE)不用 UPDATE:secret://cursorAuth/openAIKey 这行在
   从没填过 key 的新用户机器上不存在,UPDATE 会 0 行静默成功=假绿(printed 写了其实没写)。 */
function openDb() {
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(STATE_DB);
    return {
      get: (k) => { const r = db.prepare("SELECT value FROM ItemTable WHERE key=?").get(k); return r ? r.value : null; },
      set: (k, v) => db.prepare("INSERT INTO ItemTable(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(k, v),
      close: () => db.close(),
    };
  } catch (e) { /* 落到备胎 */ }
  const sqlite3 = require(path.join(RES, "node_modules", "@vscode", "sqlite3"));
  const db = new sqlite3.Database(STATE_DB);
  const call = (fn, sql, args) => new Promise((res, rej) => db[fn](sql, args, function (err, row) { err ? rej(err) : res(row); }));
  return {
    get: (k) => call("get", "SELECT value FROM ItemTable WHERE key=?", [k]).then((r) => (r ? r.value : null)),
    set: (k, v) => call("run", "INSERT INTO ItemTable(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [k, v]),
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
  // #3 默认模型:装完直接选中 cursor-g-5.6-sol,用户不用在菜单里挑。
  // 保守——仅当当前选中的不是任一 cursor-g 时才设,不覆盖用户自己已选的 cursor-g。
  const mc = (ai.modelConfig = ai.modelConfig || {});
  const curName = mc.composer && mc.composer.modelName;
  const alreadyCursorG = typeof curName === "string" && curName.startsWith("cursor-g");
  let defModelSet = "(skip, 已是 " + (curName || "?") + ")";
  if (!alreadyCursorG) {
    for (const feat of ["composer", "cmd-k"]) {
      mc[feat] = { ...(mc[feat] || {}), modelName: DEFAULT_MODEL, selectedModels: [{ modelId: DEFAULT_MODEL, parameters: [] }] };
    }
    defModelSet = DEFAULT_MODEL;
  }
  console.log("   config diff: baseUrl %j -> %j ; useOpenAIKey %j -> true ; +models %s ; defaultModel -> %s",
    before.baseUrl, args.baseUrl, before.useKey, added.length ? added.join(",") : "(none, all present)", defModelSet);
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

/* ── #2 写 OpenAI Key 进 Cursor 的加密 secret(mac 实测;win/linux best-effort,自检回退) ──
   Key 存 secret://cursorAuth/openAIKey,值=Electron safeStorage 的 OSCrypt 密文序列化成
   {"type":"Buffer","data":[...]}。mac/linux:主密码在系统钥匙串,keytar 读(本进程=Cursor
   已签名二进制,in-process 读不弹框);密文=v10+AES-128-CBC,iv=16空格,
   key=PBKDF2("saltysalt",mac 1003/linux 1,16,sha1)。
   诚实边界:Windows 的 OSCrypt 是 DPAPI,方案不同且本机无法验证 → 直接回退手填,不冒险写坏。 */
async function deriveOsCryptKey(RES) {
  if (process.platform === "win32") return null;
  let keytar;
  try { keytar = require(path.join(RES, "node_modules", "keytar")); }
  catch (e) { return null; }
  let pw = null;
  try { pw = await keytar.getPassword("Cursor Safe Storage", "Cursor"); } catch (e) { pw = null; }
  if (!pw) return null;
  const iters = process.platform === "darwin" ? 1003 : 1;
  return crypto.pbkdf2Sync(pw, "saltysalt", iters, 16, "sha1");
}
function oscryptEncrypt(key, plaintext) {
  const iv = Buffer.alloc(16, 0x20);
  const c = crypto.createCipheriv("aes-128-cbc", key, iv);
  return Buffer.concat([Buffer.from("v10"), c.update(Buffer.from(plaintext, "utf8")), c.final()]);
}
function oscryptDecrypt(key, enc) {
  const iv = Buffer.alloc(16, 0x20);
  const d = crypto.createDecipheriv("aes-128-cbc", key, iv);
  return Buffer.concat([d.update(enc.slice(3)), d.final()]).toString("utf8");
}
// 返回 {ok, reason?, confirmed?}。两道自检:①能解开 Cursor 现有 Key(证明方案与 Cursor 完全一致)
// ②加密回环相等。任一不过 → 不写,回退手填(绝不假绿)。
async function writeOpenAIKey(rawKey, RES, dry) {
  if (process.platform === "win32")
    return { ok: false, reason: "Windows 暂不支持自动写(OSCrypt=DPAPI,未实测),请在 Cursor 里手动粘一次" };
  const key = await deriveOsCryptKey(RES);
  if (!key) return { ok: false, reason: "读不到钥匙串主密码(keytar 不可用?)" };
  const db = openDb();
  let schemeOk = null; // true=解开现有Key(已确认) / false=解不开(方案不符) / null=无现存Key
  try {
    const cur = await db.get(OPENAI_KEY_SECRET);
    if (cur) {
      const enc = Buffer.from(JSON.parse(cur).data);
      try { oscryptDecrypt(key, enc); schemeOk = true; } catch (e) { schemeOk = false; }
    }
  } catch (e) { /* ignore */ }
  if (schemeOk === false) { db.close(); return { ok: false, reason: "现有 Key 解密失败,方案与本机不符,不冒险覆盖" }; }
  if (schemeOk === null && process.platform !== "darwin") { db.close(); return { ok: false, reason: "非 mac 且无现存 Key 可校验方案,回退手填" }; }
  const enc = oscryptEncrypt(key, rawKey);
  if (oscryptDecrypt(key, enc) !== rawKey) { db.close(); return { ok: false, reason: "加密回环自检不过" }; }
  if (!dry) await db.set(OPENAI_KEY_SECRET, JSON.stringify({ type: "Buffer", data: [...enc] }));
  db.close();
  return { ok: true, confirmed: schemeOk === true };
}
function promptLine(q) {
  return new Promise((res) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(q, (a) => { rl.close(); res((a || "").trim()); });
  });
}

function ts() { const d = new Date(), z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${z(d.getMonth() + 1)}${z(d.getDate())}-${z(d.getHours())}${z(d.getMinutes())}${z(d.getSeconds())}`; }

async function doBackup(ver, plans, oldBlob) {
  const bdir = path.join(BACKUP_ROOT, `${ver}-${ts()}`);
  fs.mkdirSync(bdir, { recursive: true });
  for (const { p } of plans) fs.copyFileSync(p, path.join(bdir, path.basename(p)));
  if (oldBlob != null) fs.writeFileSync(path.join(bdir, "applicationUser.blob.json"), oldBlob);
  if (fs.existsSync(SETTINGS_JSON)) fs.copyFileSync(SETTINGS_JSON, path.join(bdir, "settings.json"));
  // #2 备份现有 Key secret(若有),便于 --revert 还原
  try { const db = openDb(); const k = await db.get(OPENAI_KEY_SECRET); db.close();
    if (k != null) fs.writeFileSync(path.join(bdir, "openAIKey.secret.json"), k); } catch (e) { /* ignore */ }
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
  const ks = path.join(b, "openAIKey.secret.json");
  if (fs.existsSync(ks)) {
    const db = openDb();
    await db.set(OPENAI_KEY_SECRET, fs.readFileSync(ks, "utf8"));
    db.close(); console.log("  restored openAIKey secret");
  }
  console.log("回滚完成,重启 Cursor 生效。");
}

/* ── main ── */
async function main() {
  const argv = process.argv.slice(2);
  const has = (f) => argv.includes(f);
  const opt = (f, dflt) => { const i = argv.indexOf(f); return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt; };
  const args = {
    apply: has("--apply"), revert: has("--revert"), repair: has("--repair"),
    forceVersion: has("--force-version"), pinUpdate: has("--pin-update"),
    key: opt("--key", ""),
    baseUrl: opt("--base-url", DEFAULT_BASE_URL),
    models: opt("--models", DEFAULT_MODELS.join(",")).split(",").map((s) => s.trim()).filter(Boolean),
  };

  if (!fs.existsSync(RES)) { console.log("!! 找不到 Cursor 资源目录:", RES); process.exit(1); }
  const ver = cursorVersion();
  console.log("Cursor version:", ver, "| platform:", process.platform, "| runtime:", process.version);

  if (args.revert) return revert();

  // #4 软版本闸:非 3.16.x 只告警不拒绝——真正的安全阀是 planBundle 里"锚点必须 exactly-1"。
  // 这样 Cursor 升级后 --repair 也能尝试;bundle 变到锚点认不出 → planBundle 自动拒绝(不会改坏)。
  if (!ver.startsWith(SUPPORTED_MAJOR_MINOR + ".") && !args.forceVersion) {
    console.log("⚠️  本安装器按 Cursor %s.x 设计,当前 %s。将继续尝试;若 bundle 结构变了认不出锚点会自动拒绝(不会改坏)。",
      SUPPORTED_MAJOR_MINOR, ver);
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

  // #4 修复模式:只重打 bundle(配置/Key 在库里,升级不动它们),不碰 config/update.mode/Key。
  if (args.repair) {
    if (!plans.length) { console.log("\n✅ bundle 补丁都在,无需修复。"); return; }
    console.log("--- 修复:重打 bundle ---");
    await doBackup(ver, plans, null);
    for (const { p, out } of plans) { fs.writeFileSync(p, out); console.log("   patched:", path.basename(p)); }
    console.log("\n✅ 修复完成,重启 Cursor 即可继续用 cursor-g。");
    return;
  }

  console.log("--- 3) BYOK 配置差异 ---");
  const oldBlob = await mergeConfig(args, !args.apply);
  console.log("--- 4) update.mode ---");
  if (args.pinUpdate) setUpdateNone(!args.apply);
  else console.log("   跳过(允许 Cursor 自动升级;升级后 cursor-g 没了就双击 REPAIR / 跑 --repair)。加 --pin-update 可锁定不升级。");

  if (!args.apply) { console.log("\n[dry-run] 以上全部通过。加 --apply 执行(会先备份到 %s)。", BACKUP_ROOT); return; }

  console.log("--- 落盘 ---");
  if (plans.length) {
    await doBackup(ver, plans, oldBlob);
    for (const { p, out } of plans) { fs.writeFileSync(p, out); console.log("   patched:", path.basename(p)); }
  } else {
    const bdir = path.join(BACKUP_ROOT, `${ver}-${ts()}-cfgonly`);
    fs.mkdirSync(bdir, { recursive: true });
    fs.writeFileSync(path.join(bdir, "applicationUser.blob.json"), oldBlob);
    try { const db = openDb(); const k = await db.get(OPENAI_KEY_SECRET); db.close();
      if (k != null) fs.writeFileSync(path.join(bdir, "openAIKey.secret.json"), k); } catch (e) { /* ignore */ }
    console.log("   备份(仅配置)->", bdir);
  }

  // #2 写 Key:命令行给了 --key 用它,否则 TTY 下提示粘一次;空/非 TTY → 回退手填。
  console.log("--- 5) 写入 API Key ---");
  let rawKey = args.key;
  if (!rawKey && process.stdin.isTTY) {
    rawKey = await promptLine("   请粘贴你的 API Key 后回车(直接回车=稍后自己在 Cursor 里填): ");
  }
  let keyDone = false;
  if (rawKey) {
    const r = await writeOpenAIKey(rawKey, RES, false);
    if (r.ok) { keyDone = true; console.log("   ✅ Key 已写入" + (r.confirmed ? "(方案已用现有 Key 校验一致)" : "")); }
    else console.log("   ⚠️  自动写 Key 跳过:%s —— 请稍后在 Cursor 里手动粘一次。", r.reason);
  } else {
    console.log("   (没输入 Key,跳过——稍后在 Cursor 里粘一次即可)");
  }

  console.log("\n✅ 完成。" + (keyDone
    ? "启动 Cursor,模型菜单默认就是 cursor-g-5.6-sol,直接用。"
    : "还差一步:启动 Cursor → Settings → Models → OpenAI API Key,粘贴你的 key 点 Verify。"));
  console.log("   回滚整包:启动器加 --revert;Cursor 升级后失效:双击 REPAIR 或跑 --repair。");
}

main().catch((e) => { console.error("!! 未预期错误:", e && e.message ? e.message : e); process.exit(9); });
