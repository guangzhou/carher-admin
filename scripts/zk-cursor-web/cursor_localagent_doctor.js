// cursor_localagent_doctor.js —— 只读诊断:同事装完报
//   「Local Agent Not Configured / Configure your API key and base URL to use the local agent」
// 时,回答**到底缺哪一样**(apiKey 还是 baseUrl),以及下一步该做什么。Cursor 不用退。
//
// 用法(必须用 Cursor 自己的二进制跑,否则读不到 state.vscdb 的 node:sqlite):
//   ELECTRON_RUN_AS_NODE=1 /Applications/Cursor.app/Contents/MacOS/Cursor cursor_localagent_doctor.js
//   ⛔ 漏 ELECTRON_RUN_AS_NODE=1 会弹一个 Cursor 窗口然后永远挂住(不是报错)。
//
// 判据从哪来:不是猜的,是照抄 Cursor 3.20 bundle 里 `localAgentProviderConfig.js`
// 那段解析(函数 Jso/Qso/A6d)。它对 apiKey 和 baseUrl 各有一串**按序**的候选源,
// 任何一样在全部候选里都取不到非空字符串,就抛出那个弹窗。本脚本只检查其中
// 落在本机存储里的那两条(storage.openAIKey / storage.openAIBaseUrl)+ 那道闸,
// 其余候选(modelDetails.* / env)由运行时决定,脚本量不到,会如实标成"量不到"。
//
// ⚠️ 本脚本**不打印 Key 明文**:只报在不在、长度、sha256 前 16 位。
// ⚠️ 它只读,不写任何一行。
const path = require("path"), fs = require("fs"), crypto = require("crypto");

const HOME = process.env.HOME || require("os").homedir();
const USER_DIR = process.env.CX_USER_DIR ||
  (process.platform === "darwin"
    ? path.join(HOME, "Library/Application Support/Cursor/User")
    : process.platform === "win32"
    ? path.join(process.env.APPDATA || "", "Cursor/User")
    : path.join(HOME, ".config/Cursor/User"));
const DB = process.env.CX_DB || path.join(USER_DIR, "globalStorage", "state.vscdb");
const APP_USER_KEY = "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl" +
  ".persistentStorage.applicationUser";
const OPENAI_KEY_SECRET = "secret://cursorAuth/openAIKey";

// 空白串按 Cursor 的算法**等于没有**(它先 trim 再判 undefined),所以这里同样 trim。
const nonEmpty = (v) => (typeof v === "string" && v.trim() !== "");

function die(msg, next) {
  console.error("FAIL: " + msg);
  if (next) console.error("  下一步: " + next);
  process.exit(2);
}

if (!fs.existsSync(DB)) die("找不到 state.vscdb:" + DB, "确认 Cursor 至少启动过一次(全新机器要先开一次 Cursor 再装)。");

let DatabaseSync;
try { ({ DatabaseSync } = require("node:sqlite")); }
catch (e) { die("这个运行时没有 node:sqlite(" + (e && e.message) + ")",
  "必须用 Cursor 自带的二进制跑:ELECTRON_RUN_AS_NODE=1 /Applications/Cursor.app/Contents/MacOS/Cursor " + path.basename(__filename)); }

const db = new DatabaseSync(DB, { readOnly: true });
const get = (k) => { const r = db.prepare("SELECT value FROM ItemTable WHERE key=?").get(k); return r ? r.value : null; };

const rawBlob = get(APP_USER_KEY);
const rawKey = get(OPENAI_KEY_SECRET);
db.close();

if (!rawBlob) die("applicationUser blob 不存在 —— Cursor 从没初始化过这台机器的配置。",
  "先启动一次 Cursor、登录到能看见 Settings,再 ⌘Q 退出,然后重跑安装器。");

let blob;
try { blob = JSON.parse(rawBlob); } catch (e) { die("applicationUser blob 解析不了:" + e.message); }

const baseUrl = blob.openAIBaseUrl;
const useKey = blob.useOpenAIKey;
const ai = blob.aiSettings || {};
const nModels = (ai.userAddedModels || []).length;
const nOverride = (ai.modelOverrideEnabled || []).length;
const composer = (ai.modelConfig && ai.modelConfig.composer && ai.modelConfig.composer.modelName) || null;

// Key 只看"这行 secret 在不在、像不像 OSCrypt 密文",不解密:解密要走钥匙串,
// 而这里要回答的问题("Cursor 认为你配了 Key 吗")不需要明文。
let keyState = "absent", keyBytes = 0, keySha = null;
if (rawKey != null && String(rawKey) !== "") {
  try {
    const o = JSON.parse(rawKey);
    if (o && o.type === "Buffer" && Array.isArray(o.data) && o.data.length > 0) {
      keyState = "present"; keyBytes = o.data.length;
      keySha = crypto.createHash("sha256").update(Buffer.from(o.data)).digest("hex").slice(0, 16);
    } else keyState = "malformed";
  } catch (e) { keyState = "malformed"; }
}

console.log("state.vscdb : " + DB);
console.log("");
console.log("Cursor 判「local agent 配好了没有」要同时拿到 apiKey 和 baseUrl,缺一个就弹那个框。");
console.log("");

// ── apiKey 一侧 ──────────────────────────────────────────────────────────────
// Cursor 侧:storage.openAIKey 这条候选**被 useOpenAIKey 闸住** ——
// bundle 里那句原话是 "Stored OpenAI key exists but useOpenAIKey is false"。
// 所以「库里有 Key」和「Cursor 会用这个 Key」是两件事,必须分开报。
const keyUsable = keyState === "present" && useKey === true;
console.log("① apiKey");
console.log("   storage.openAIKey     : " + keyState +
  (keyState === "present" ? "(" + keyBytes + " 字节密文, sha256_16=" + keySha + ")" : ""));
console.log("   storage.useOpenAIKey  : " + JSON.stringify(useKey) + "   ← 这道闸为 false 时,上面那条 Key 会被**忽略**");
console.log("   → 本机存储这一侧:" + (keyUsable ? "OK" : "取不到"));

// ── baseUrl 一侧 ─────────────────────────────────────────────────────────────
console.log("② baseUrl");
console.log("   storage.openAIBaseUrl : " + JSON.stringify(baseUrl));
console.log("   → 本机存储这一侧:" + (nonEmpty(baseUrl) ? "OK" : "取不到"));

console.log("");
console.log("附:模型清单 userAddedModels=" + nModels + " / modelOverrideEnabled=" + nOverride +
  " / composer 选中=" + JSON.stringify(composer));
console.log("");

const missing = [];
if (!keyUsable) missing.push("apiKey");
if (!nonEmpty(baseUrl)) missing.push("baseUrl");

if (missing.length === 0) {
  console.log("VERDICT: 本机存储这两样都齐 —— 这个弹窗**不该**由存储缺失引起。");
  console.log("  ⚠️ 脚本量不到的候选还有:modelDetails.apiKey / modelDetails.openaiApiBaseUrl(挂在具体模型上)");
  console.log("     和 env CURSOR_LOCAL_AGENT_API_KEY / CURSOR_LOCAL_AGENT_BASE_URL。");
  console.log("  下一步只有一条:在弹窗里点 **Open logs**,找这一行 ——");
  console.log("     [local-agent][config] path=… result=… missing=[…] apiKeySource=… baseUrlSource=…");
  console.log("     它由 Cursor 自己打,直接写明缺的是哪一样、从哪个源取的,别再猜。");
  process.exit(0);
}

console.log("VERDICT: 缺 [" + missing.join(", ") + "]");
if (!keyUsable) {
  if (keyState === "absent") {
    console.log("  · 库里根本没有 Key —— 装的时候在「请粘贴你的 API Key 后回车」那一步直接敲了回车,");
    console.log("    安装器会照样打完补丁、写完地址和菜单并打印 ✅ 完成,只是没有 Key。");
    console.log("    修:Cursor → Settings → Models → OpenAI API Key 粘一次点 Verify;");
    console.log("       或退出 Cursor 后重跑 INSTALL,在那一步真的把 Key 粘进去。");
  } else if (keyState === "malformed") {
    console.log("  · secret 行在,但不是 Cursor 认得的密文结构 —— 手填一次覆盖掉它。");
  } else {
    console.log("  · Key 在库里,但 useOpenAIKey=" + JSON.stringify(useKey) + " 把它闸掉了。");
    console.log("    修:Settings → Models 里把 OpenAI API Key 那个开关打开(或重跑一次安装器)。");
  }
}
if (!nonEmpty(baseUrl)) {
  console.log("  · BYOK 地址是空的 —— 安装器的第 3 步没落盘(多半是当时 Cursor 还开着)。");
  console.log("    修:完全退出 Cursor(⌘Q),重跑一次 INSTALL / UPGRADE。");
}
process.exit(1);
