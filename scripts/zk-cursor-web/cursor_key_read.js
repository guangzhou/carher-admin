// cursor_key_read.js —— 只读:解开本机 Cursor 的 BYOK Key(secret://cursorAuth/openAIKey)。Cursor 不用退。
//
// 用法(必须用 Cursor 自己的二进制跑,见下面「为什么」):
//   ELECTRON_RUN_AS_NODE=1 /Applications/Cursor.app/Contents/MacOS/Cursor cursor_key_read.js          # 默认打掩码
//   ELECTRON_RUN_AS_NODE=1 /Applications/Cursor.app/Contents/MacOS/Cursor cursor_key_read.js --reveal # 打明文
//
// ⚠️ 这是诊断工具,**不进交付 zip**(package_team_setup.sh 不拷它)。它读的是凭据:
//    默认只打前后各 4 位 + 长度 + sha256 前 16 位,足够回答"两边是不是同一个 Key";
//    要明文才 `--reveal`,且别把输出贴进 issue / 聊天记录 / 工单。
//
// 为什么必须用 Cursor 的二进制:
//   Key 存在 state.vscdb 的 ItemTable 里,值 = Electron safeStorage 的 OSCrypt 密文
//   序列化成 {"type":"Buffer","data":[...]},"v10" 前缀 + AES-128-CBC,iv = 16 个 0x20,
//   key = PBKDF2(钥匙串主密码, "saltysalt", mac 1003 / linux 1, 16, sha1)。
//   主密码在 macOS 钥匙串 `Cursor Safe Storage` / `Cursor`,那条 item 的 ACL **只认
//   Cursor 签名的二进制**。所以:
//     - in-process require Cursor 自带的 keytar → 不弹框,直接拿到。
//     - `/usr/bin/security find-generic-password` → **弹授权框**。在无人值守 / agent shell 里
//       没人点那个框,表现是**命令永远挂住、零输出、最后被超时杀掉**(不是报错、不是拒绝),
//       极容易被读成"钥匙串里没这条 item"或"脚本死循环"。判据:`security` 超过几秒没吐东西
//       就是在等框,别加 timeout 重试,换这条路。
//     - 漏 `ELECTRON_RUN_AS_NODE=1` → 不是跑 node,而是**弹一个 Cursor 窗口然后永远挂住**。
//
// 与 cursor_team_setup.js 的关系:那边 deriveOsCryptKey/oscryptDecrypt 是同一套算法
// (写 Key 前的"能解开现有 Key"自检就是这条路)。这里只读、不写、不碰 bundle。
const path = require("path"), crypto = require("crypto"), fs = require("fs");

const REVEAL = process.argv.includes("--reveal");
const RES = process.env.CX_RES || "/Applications/Cursor.app/Contents/Resources/app";
const DB = process.env.CX_DB || path.join(
  process.env.HOME, "Library/Application Support/Cursor/User/globalStorage/state.vscdb");
const SECRET_KEY = "secret://cursorAuth/openAIKey";

// 每个失败都给**不同的**退出码和一句"下一步",因为这四种失败的处置完全不一样。
function die(code, msg, next) {
  console.error("FAIL(" + code + "): " + msg);
  if (next) console.error("  下一步: " + next);
  process.exit(code);
}

(async () => {
  if (process.platform === "win32")
    die(2, "Windows 的 OSCrypt 是 DPAPI,本脚本没实测过",
      "在 Cursor 设置里自己看,别拿这个脚本的沉默当结论");
  if (!fs.existsSync(RES)) die(2, "找不到 Cursor Resources/app: " + RES, "CX_RES=<路径> 指过去");
  if (!fs.existsSync(DB)) die(2, "找不到 state.vscdb: " + DB, "CX_DB=<路径> 指过去");

  let keytar;
  try { keytar = require(path.join(RES, "node_modules", "keytar")); }
  catch (e) {
    die(3, "require 不到 Cursor 自带 keytar: " + e.message,
      "确认是用 `ELECTRON_RUN_AS_NODE=1 <Cursor 可执行文件> " + path.basename(__filename) +
      "` 跑的 —— 用系统 node 跑时 keytar 的 .node 二进制 ABI 对不上");
  }
  let pw = null;
  try { pw = await keytar.getPassword("Cursor Safe Storage", "Cursor"); }
  catch (e) { die(3, "读钥匙串抛异常: " + e.message, "别退回 /usr/bin/security —— 它会弹框然后挂住"); }
  if (!pw) die(3, "钥匙串里读不到 `Cursor Safe Storage`/`Cursor` 主密码(返回 null,不是异常)",
    "这台机器的 Cursor 可能从没启动过;先开一次 Cursor 再来");

  const iters = process.platform === "darwin" ? 1003 : 1;
  const key = crypto.pbkdf2Sync(pw, "saltysalt", iters, 16, "sha1");

  // 读库:优先 node:sqlite(Electron 自带),退 Cursor 自带的 sqlite3 —— 与安装器 openDb 同样的双备胎。
  let row = null;
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(DB, { readOnly: true });
    row = db.prepare("select value from ItemTable where key = ?").get(SECRET_KEY);
    db.close();
  } catch (e) {
    let sqlite3 = null;
    for (const m of ["sqlite3", "@vscode/sqlite3"]) {
      try { sqlite3 = require(path.join(RES, "node_modules", m)); break; } catch (e2) { /* 下一个 */ }
    }
    if (!sqlite3) die(4, "node:sqlite 和 Cursor 自带 sqlite3 都用不了: " + e.message, "换一个 Cursor 版本试");
    const db = new sqlite3.Database(DB, sqlite3.OPEN_READONLY);
    row = await new Promise((res, rej) =>
      db.get("select value from ItemTable where key=?", [SECRET_KEY], (er, r) => er ? rej(er) : res(r)));
    db.close();
  }
  // "没有这一行"和"有但解不开"是两件完全不同的事,分开报:前者=他从没配过 Key(升级完会 401),
  // 后者=密文方案与本机不符(别去改 Key,先查是不是换过机器 / 迁移过 Library)。
  if (!row || row.value == null)
    die(5, "库里没有 " + SECRET_KEY + " 这一行 —— 这台机器从没配过 BYOK Key",
      "跑 INSTALL 填一次(或在 Cursor 设置里粘);别把这个当成\"读不到\"");

  let enc;
  try { enc = Buffer.from(JSON.parse(String(row.value)).data); }
  catch (e) { die(6, "那一行不是 {\"type\":\"Buffer\",\"data\":[...]} 形状: " + e.message, "先看原值再动手"); }
  if (enc.slice(0, 3).toString() !== "v10")
    die(6, "密文没有 v10 前缀(拿到 " + JSON.stringify(enc.slice(0, 3).toString()) + ")",
      "不是 OSCrypt 密文,方案不同,别硬解");

  let pt;
  try {
    const d = crypto.createDecipheriv("aes-128-cbc", key, Buffer.alloc(16, 0x20));
    pt = Buffer.concat([d.update(enc.slice(3)), d.final()]).toString("utf8");
  } catch (e) {
    die(6, "解密失败(主密码拿到了,但解不开这段密文): " + e.message,
      "方案与本机不符 —— 换过机器 / 迁移过 Library 会这样。**不要**覆盖写新 Key");
  }

  const sha = crypto.createHash("sha256").update(pt).digest("hex").slice(0, 16);
  const mask = pt.length <= 12 ? "*".repeat(pt.length)
    : pt.slice(0, 4) + "*".repeat(pt.length - 8) + pt.slice(-4);
  console.log("db        = " + DB);
  console.log("row       = " + SECRET_KEY + " (" + String(row.value).length + " B 密文 JSON)");
  console.log("len       = " + pt.length);
  console.log("sha256_16 = " + sha);
  console.log("value     = " + (REVEAL ? pt : mask + "   (--reveal 才打明文)"));
})().catch((e) => die(9, "没预料到的异常: " + (e && e.stack || e), "把这段贴给我"));
