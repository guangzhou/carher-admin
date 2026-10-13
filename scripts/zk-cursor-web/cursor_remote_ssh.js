/* cursor_remote_ssh.js —— 让「Cursor 连远程服务器」的窗口里也能用我们自己的模型。
 *
 * 用户面症状：本地窗口好好的，一开远程窗口（左下角连到那台 Linux），
 * 模型全部报连不上 / Connection error，菜单里名字还在、一点就红。
 *
 * ── 机制（2026-09-21 实测定的，不是推演）──────────────────────────────────
 * 装完本机包之后，Cursor 的 BYOK 地址是 **http://127.0.0.1:8788/v1** ——
 * 那个 127.0.0.1 指的是**你自己这台 Mac** 上的小代理。
 * 可是在远程窗口里，请求是从**远端那台 Linux**发出去的，那边的 127.0.0.1
 * 是它自己，8788 上什么都没有 ⇒ 必然连不上。
 *   实测：远端直接打 127.0.0.1:8788 → 000 / connection refused（阴性对照）
 *         ssh 加上 -R 8788:127.0.0.1:8788 之后再打 → 401（阳性对照，401=打到
 *         本机小代理了，只是没带 Key）
 *
 * 解法不是"在远端也装一套"：那台开发机通常是**多人共用**的（我们这台实测 5 个
 * 普通用户在线），在上面起一个不要密码的 8788 = 把你的 Key 额度送给同机所有人。
 * 所以走**反向隧道**：ssh 连过去的时候顺带把远端的 8788 映射回你本机的 8788。
 * 这样两边的 http://127.0.0.1:8788/v1 都成立 ⇒ **Cursor 里一个设置都不用改**，
 * Key 也始终只待在你自己机器上。
 *
 * 本脚本干的就是往 ~/.ssh/config 里加这一行：
 *     RemoteForward 8788 127.0.0.1:8788
 *
 * ── 动了什么 / 备份在哪 / 怎么回滚 ────────────────────────────────────────
 *   动：只动 ~/.ssh/config，只**加行**，不删不改你已有的任何配置。
 *       加的每一行都带 `# cr-g-remote-ssh` 标记。
 *   备份：写之前整份复制到 ~/.ssh/config.bak-<时间戳>（复制失败就中止，
 *         不会带着"没有退路"去改你的 ssh 配置）。
 *   回滚：`--remove`（只删带标记的行），或直接还原那份备份。
 *
 * 用法：
 *   cursor_remote_ssh.sh                      # 体检 + 列出可选主机（不写任何东西）
 *   cursor_remote_ssh.sh --host me@1.2.3.4    # 预演：显示要往 ssh 配置里加什么
 *   cursor_remote_ssh.sh --host me@1.2.3.4 --apply    # 真写 + 验收
 *   cursor_remote_ssh.sh --host me@1.2.3.4 --remove --apply   # 撤掉
 */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const MARK = "# cr-g-remote-ssh";
const SSH_DIR = path.join(os.homedir(), ".ssh");
const SSH_CONFIG = process.env.CRG_SSH_CONFIG || path.join(SSH_DIR, "config"); // 测试钩子

/* state.vscdb 的位置跟 cursor_team_setup.js 一致。这里**只读不写**。 */
function appRoot() {
  const d = path.dirname(process.execPath);
  if (process.platform === "darwin") return path.join(d, "..", "Resources", "app");
  return path.join(d, "resources", "app");
}
function userDir() {
  if (process.env.CURSOR_USER_DIR) return process.env.CURSOR_USER_DIR;
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Application Support", "Cursor", "User");
  if (process.platform === "win32") return path.join(process.env.APPDATA || "", "Cursor", "User");
  return path.join(os.homedir(), ".config", "Cursor", "User");
}
const STATE_DB = path.join(userDir(), "globalStorage", "state.vscdb");
const APP_USER_KEY = "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl" +
  ".persistentStorage.applicationUser";

function openDbRead() {
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(STATE_DB, { readOnly: true });
    return { get: (k) => { const r = db.prepare("SELECT value FROM ItemTable WHERE key=?").get(k); return r ? r.value : null; },
             close: () => db.close() };
  } catch (e) { /* 落备胎 */ }
  const sqlite3 = require(path.join(appRoot(), "node_modules", "@vscode", "sqlite3"));
  const db = new sqlite3.Database(STATE_DB);
  const call = (sql, a) => new Promise((res, rej) => db.get(sql, a, (e, r) => (e ? rej(e) : res(r))));
  return { get: (k) => call("SELECT value FROM ItemTable WHERE key=?", [k]).then((r) => (r ? r.value : null)),
           close: () => db.close() };
}

/* 本机 BYOK 地址 —— 判"这台机器到底需不需要隧道"的**唯一**尺子。
   ⛔ 不许默认"大家都是 127.0.0.1:8788"：Windows 和关了小代理的 Mac 上
      地址是公网直连，那种机器**天生就没这个毛病**，跑这个脚本纯属白改 ssh 配置。 */
async function readBaseUrl() {
  if (!fs.existsSync(STATE_DB)) return { err: "找不到 Cursor 的配置库(" + STATE_DB + ")" };
  let raw;
  try { const db = openDbRead(); raw = await db.get(APP_USER_KEY); db.close(); }
  catch (e) { return { err: "读配置库失败:" + e.message }; }
  if (!raw) return { err: "配置库里没有 applicationUser(Cursor 还没初始化过?)" };
  let d; try { d = JSON.parse(raw); } catch (e) { return { err: "配置 blob 不是合法 JSON" }; }
  return { url: d.openAIBaseUrl || "", useKey: !!d.useOpenAIKey };
}

function sh(cmd, args, timeoutMs) {
  return spawnSync(cmd, args, { encoding: "utf8", timeout: timeoutMs || 30000 });
}

/* 所有 ssh 调用共用的参数。
   ⚠️ ControlPath=none:ssh 会复用已有连接,复用的那条**不带**新加的 RemoteForward,
      验收会读出假红(配置明明写对了)。所以每次都强制开新连接。
   ⚠️ CRG_SSH_CONFIG 这个测试钩子必须**连探针一起**改道:只改"写哪个文件"而让 ssh
      照旧读 ~/.ssh/config,那"写入分支"就永远只能靠读代码发布 —— 而那恰恰是同事
      用得最多的一条路。 */
function sshBase() {
  const a = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlPath=none"];
  if (process.env.CRG_SSH_CONFIG) a.push("-F", process.env.CRG_SSH_CONFIG);
  return a;
}

/* 探针：在目标机上看 127.0.0.1:<port> 有没有东西应答。
   返回 HTTP 状态码字符串；"000" = 根本连不上。
   ⚠️ 远端不一定有 curl（精简镜像常见），所以有 /dev/tcp 的备胎；
      备胎只能证明"端口通不通"，证不出 HTTP，因此它只回 "open"/"000"。 */
/* 🔴 2026-10-13(188 实测):**只看 HTTP 码会把"别人占着这个端口"读成"隧道已经通了"**。
   188 上 8788 被一个跑了 98 天的 zerokey-codex bridge 占着:带 RemoteForward 连进去,
   ssh 只打一行 `remote port forwarding failed`(连接照建),远端打 127.0.0.1:8788 回 **200**
   —— 但那是 codex bridge 的模型列表,不是我们的小代理。原来这个函数看到非 000 就判「已经通了」,
   于是**正好在坏的那台上告诉用户不用改**,而 Cursor 的请求全打到了别人的服务上。
   ⇒ 判据改成**认人不认码**:打 /healthz,只有回包里带小代理自己的字段(`delta_url`)才算是我们。
   返回 code 之外再给 who:"ours"(我们的小代理)/"other"(端口有人但不是我们)/"none"(没人)。 */
function probeRemote(host, port, withTunnel) {
  const base = sshBase();
  if (withTunnel) base.push("-R", port + ":127.0.0.1:" + port);
  const remote =
    "if command -v curl >/dev/null 2>&1; then " +
    "c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:" + port + "/v1/models); " +
    "h=$(curl -s --max-time 6 http://127.0.0.1:" + port + "/healthz 2>/dev/null | head -c 400); " +
    "case \"$h\" in *delta_url*) w=ours ;; *) [ \"$c\" = 000 ] && w=none || w=other ;; esac; " +
    "printf '%s %s' \"$c\" \"$w\"; " +
    "else (exec 3<>/dev/tcp/127.0.0.1/" + port + ") >/dev/null 2>&1 && printf 'open unknown' || printf '000 none'; fi";
  const r = sh("ssh", base.concat([host, remote]), 45000);
  const out = (r.stdout || "").trim().split(/\s+/);
  const err = (r.stderr || "").trim();
  return { code: out[0] || "000", who: out[1] || "none", err, rc: r.status,
           fwdFailed: /remote port forwarding failed/i.test(err) };
}

/* 本机小代理活着没 —— 第 0 步阳性对照。
   它要是没起来，远端隧道通了也照样连不上，症状一模一样 ⇒ 不先量这一步的话，
   后面会把"小代理没开"错判成"隧道没配好"，然后白改一遍 ssh 配置。 */
function probeLocal(port) {
  const r = sh("curl", ["-s", "-o", os.platform() === "win32" ? "NUL" : "/dev/null",
                        "-w", "%{http_code}", "--max-time", "8",
                        "http://127.0.0.1:" + port + "/v1/models"], 15000);
  return (r.stdout || "").trim() || "000";
}

/* ── ~/.ssh/config 解析。只按行做，**不重排、不格式化**别人的配置 ────────────
   返回每个 Host 块的 [起始行, 结束行) 和它声明的别名。 */
function parseHosts(lines) {
  const blocks = [];
  for (let i = 0; i < lines.length; i++) {
    const m = /^\s*Host\s+(.+?)\s*$/i.exec(lines[i]);
    if (!m) continue;
    if (blocks.length) blocks[blocks.length - 1].end = i;
    blocks.push({ start: i, end: lines.length, names: m[1].split(/\s+/) });
  }
  return blocks;
}
function indentOf(lines, blk) {
  for (let i = blk.start + 1; i < blk.end; i++) {
    const m = /^(\s+)\S/.exec(lines[i]);
    if (m) return m[1];              // 跟随这个块已有的缩进,别在人家文件里混两种风格
  }
  return "    ";
}

function usage() {
  console.log([
    "用法:",
    "  cursor_remote_ssh.sh                          体检:看这台机需不需要隧道 + 列出可选主机",
    "  cursor_remote_ssh.sh --host me@1.2.3.4        预演(默认):只显示要加什么,不落盘",
    "  cursor_remote_ssh.sh --host me@1.2.3.4 --apply        真写 + 验收",
    "  cursor_remote_ssh.sh --host me@1.2.3.4 --remove --apply   撤掉(只删带标记的行)",
    "可选: --port N  (默认从 Cursor 的 BYOK 地址里读,不写死)",
  ].join("\n"));
}

function listHostsHint() {
  const lines = fs.existsSync(SSH_CONFIG) ? fs.readFileSync(SSH_CONFIG, "utf8").split("\n") : [];
  const names = [];
  parseHosts(lines).forEach((x) => x.names.forEach((n) => { if (!n.includes("*") && !n.includes("?")) names.push(n); }));
  console.log("\n③ 还没指定要连哪台。你 ~/.ssh/config 里现有的主机:");
  if (names.length) names.forEach((n) => console.log("     " + n));
  else console.log("     (一个都没有 —— 也可以直接填 用户名@IP)");
  console.log("   (Cursor 左下角「Connect to Host」下拉**也只列这个文件里的机器**。)");
}

/* ⑧ 服务器上的 Cursor 组件补丁(远程窗口里 agent 扩展跑在服务器上,本机补丁管不到)。
   服务器上要先有和本机**同 commit** 的 cursor-server(Cursor 第一次连上去时自己装)。 */
function serverPatch(host, okTail) {
  console.log("\n⑧ 服务器上的 Cursor 组件补丁");
  const rp = sh(process.execPath, [path.join(__dirname, "cursor_remote_patch.js"), "--host", host, "--apply"], 240000);
  const rpOut = (rp.stdout || "") + (rp.stderr || "");
  if (/还没有和你本机同版本/.test(rpOut)) {
    console.log("   ⏳ 服务器上还没有和你本机同版本的 Cursor 组件(第一次连这台机,或刚升级过 Cursor)。");
    console.log("      先在 Cursor 里连一次这台服务器,等左下角显示已连接,**再双击一次本脚本**,它会补上。");
    return;
  }
  if (rp.status === 0) {
    // 每个文件一行,行首「✅ 已打」或「已打过」,行尾又带「(已打过)」⇒ 按行数,不按命中次数
    const n = rpOut.split("\n").filter((l) => /^\s*(✅ 已打|已打过)\s/.test(l)).length;
    console.log("   ✅ 已补好(" + n + " 个文件)" + (okTail || ""));
    return;
  }
  console.log("   ⚠️ 没补成(不影响模型能不能用,只影响远程窗口里的上下文压缩):");
  rpOut.split("\n").filter((l) => /❌|⚠️/.test(l)).slice(0, 4).forEach((l) => console.log("      " + l.trim()));
}

/* --wizard:双击版的「列主机 → 问哪台 → 预演 → 问 yes → 执行」在 JS 里做。
   🔴 2026-10-13 同事 Windows 实机:REMOTE-SSH-Windows.cmd 原来把这套问答写在 .cmd 里(中文 echo +
   set /p + chcp 65001 + 只有 LF 换行),cmd 按字节偏移逐行续读批处理 ⇒ 行首被吃,满屏
   「…不是内部或外部命令」,连 --apply 那行都被切碎。.cmd 只留纯 ASCII + CRLF、无 chcp,
   中文和交互全在这里(Node 输出 UTF-8)。Mac 启动器也走这里,两边一套逻辑。 */
// 一个进程只开一个 readline、按行排队取:每问一次就新开/关一个 interface 的话,
// stdin 里已经读进缓冲的后几行(管道/粘贴一次输入多行)会随 close 一起丢掉 ⇒ 第二问读到空(实测)。
let _rl = null; const _lines = []; const _waiters = [];
function ask(q) {
  if (!_rl) {
    _rl = require("readline").createInterface({ input: process.stdin, terminal: false });
    _rl.on("line", (l) => { const w = _waiters.shift(); if (w) w(l); else _lines.push(l); });
    _rl.on("close", () => { while (_waiters.length) _waiters.shift()(""); });
  }
  process.stdout.write(q);
  return new Promise((res) => {
    const done = (a) => res((a || "").trim());
    if (_lines.length) done(_lines.shift()); else _waiters.push(done);
  });
}
async function wizard(rest) {
  const self = (extra) => spawnSync(process.execPath, [__filename, ...rest, ...extra], { stdio: ["ignore", "inherit", "inherit"] }).status;
  console.log("让「Cursor 连远程服务器」的窗口里也能用 cursor-g 的模型。\n");
  console.log("它做两件事:①(Mac 走本机小代理时)把远端的 127.0.0.1:8788 转回你本机;");
  console.log("②补好服务器上的 Cursor 组件(远程窗口里有一部分组件跑在服务器上)。Cursor 里一个设置都不用改。\n");
  const rc0 = self([]);
  if (rc0 !== 0) return rc0;   // 小代理没起来等:上面已经说清楚了
  const host = (await ask("\n要配哪台?(照着上面的名字填,或 用户名@IP;直接回车=退出): ")).replace(/"/g, "");
  if (!host) { console.log("已退出,什么都没动。"); return 0; }
  console.log("\n===== 先预演一遍,只显示要改什么,不落盘 =====");
  const rc1 = self(["--host", host]);
  if (rc1 !== 0) return rc1;   // ④ 端口被别人占着 / ssh 不通:已打印原因和解法,不再往下问
  const ans = await ask("\n确认执行?输入 yes 回车(其它任何输入=取消): ");
  if (ans.toLowerCase() !== "yes") { console.log("已取消,什么都没动。"); return 0; }
  return self(["--host", host, "--apply"]);
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv.includes("--wizard")) {
    const rc = await wizard(argv.filter((a) => a !== "--wizard"));
    if (_rl) _rl.close();
    return rc;
  }
  const args = { apply: false, remove: false, host: "", port: 0 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--apply") args.apply = true;
    else if (a === "--remove") args.remove = true;
    else if (a === "--host") args.host = argv[++i] || "";
    else if (a === "--port") args.port = parseInt(argv[++i] || "0", 10);
    else if (a === "-h" || a === "--help") { usage(); return 0; }
    else { console.log("!! 不认识的参数:" + a); usage(); return 2; }
  }

  console.log("═══ Cursor 远程窗口 · 模型直通检查 ═══\n");

  // ── 1) 这台机器到底需不需要做这件事 ────────────────────────────────────
  const b = await readBaseUrl();
  if (b.err) { console.log("❌ " + b.err); return 3; }
  console.log("① 你的 Cursor 模型地址:" + (b.url || "(空)"));
  const m = /^https?:\/\/(127\.0\.0\.1|localhost)(?::(\d+))?/i.exec(b.url || "");
  if (!m) {
    // 公网直连不需要隧道,但服务器上的 Cursor 组件**照样要补**(与隧道无关)。
    // 🔴 10-13 前这里直接 return ⇒ 走公网直连的 Windows 同事从来没补到服务器那份。
    console.log("   → 这是**公网直连**,不经过本机小代理 ⇒ **不需要隧道**,ssh 配置一个字都不改。");
    console.log("      (要是远程窗口里模型连不上,那是另一回事:先确认那台服务器能上外网。)");
    console.log("   但服务器上的 Cursor 组件**照样要补**(远程窗口里有一部分组件跑在服务器上)。");
    if (!args.host) { listHostsHint(); return 0; }
    console.log("\n③ 目标主机:" + args.host);
    if (!args.apply) { console.log("\n以上是**预演**:只会补服务器上的 Cursor 组件(先备份),不改 ssh 配置。"); return 0; }
    serverPatch(args.host, "。已开着的远程窗口关掉重连即可。");
    console.log("\n  **每次升级 Cursor 后**,连一次服务器、再双击一次本脚本(服务器要补的是新版本那份)。");
    return 0;
  }
  const port = args.port || parseInt(m[2] || "8788", 10);
  console.log("   → 指向**本机**小代理,端口 " + port);
  console.log("   ⚠️ 远程窗口里的 127.0.0.1 是**远端那台机**,所以必须做隧道。\n");

  // ── 2) 第 0 步:本机小代理自己活着没 ──────────────────────────────────
  const lc = probeLocal(port);
  console.log("② 本机小代理 127.0.0.1:" + port + " → HTTP " + lc);
  if (lc === "000") {
    console.log("   ❌ 本机小代理没起来。**先修这个**:隧道配好了也照样连不上,症状一模一样。");
    console.log("      双击 REPAIR-Mac.command(或 INSTALL)把它拉起来,再回来跑本脚本。");
    return 3;
  }
  console.log("   ✅ 活着(401 = 活着但没带 Key,正常)\n");

  // ── 3) 目标主机 ───────────────────────────────────────────────────────
  let lines = [];
  let exists = fs.existsSync(SSH_CONFIG);
  if (exists) lines = fs.readFileSync(SSH_CONFIG, "utf8").split("\n");
  const blocks = parseHosts(lines);

  if (!args.host) { listHostsHint(); return 0; }
  const host = args.host;
  const alias = host.includes("@") ? host.split("@")[1] : host;
  console.log("③ 目标主机:" + host);

  const ping = sh("ssh", sshBase().concat([host, "echo ok"]), 25000);
  if ((ping.stdout || "").trim() !== "ok") {
    console.log("   ❌ ssh 连不上。原样贴出来(先修 ssh 免密,那是另一件事):");
    (ping.stderr || "(没有输出)").split("\n").forEach((l) => l && console.log("      " + l));
    return 3;
  }
  console.log("   ✅ ssh 通\n");

  // ── 4) 阴性对照:现在远端打得通吗 ──────────────────────────────────────
  const before = probeRemote(host, port, false);
  console.log("④ 阴性对照 · 远端直接打 127.0.0.1:" + port + " → " + before.code + "(" + before.who + ")");
  if (before.who === "other" && !args.remove) {
    // 端口被远端别的服务占了 ⇒ 我们的反向隧道**建不起来**,而 Cursor 会把请求打给那个服务。
    console.log("   ❌ 远端的 " + port + " 端口已经被**别的程序**占着(不是我们的小代理)。");
    console.log("      这时 ssh 只会打一行 `remote port forwarding failed` 然后照常连上,");
    console.log("      Cursor 的模型请求全部打到那个程序上 ⇒ 模型报错,而且看不出原因。");
    // ⚠️ 不能简单"换个远端端口":Cursor 里的模型地址是 127.0.0.1:8788,在远程窗口里请求从
    //    远端发出 ⇒ 远端必须**恰好**在 8788 上有我们的小代理,换成 18788 Cursor 也不会去找它。
    //    所以能走的只有两条:让出这台机器的 8788,或者这台机器上直接用公网地址(不走小代理)。
    console.log("      解法(二选一):");
    console.log("        a) 请这台服务器的管理员把占着 " + port + " 的程序换个端口(它可以换,我们不行)");
    console.log("        b) 这台机器上不走本机小代理:Cursor → Settings → Models,把 OpenAI Base URL");
    console.log("           临时改成  https://cc.auto-link.com.cn/pro/v1  (远程窗口天生就能用,只是没有增量传输)");
    console.log("      占用者:  在服务器上跑  ss -ltnp | grep :" + port + "  能看到是谁");
    return 4;
  }
  if (before.who === "ours" && !args.remove) {
    console.log("   ✅ 已经通了,而且确认是**我们的小代理**(/healthz 认得出) —— 隧道已经配好,ssh 配置不用改。");
    // 隧道已配好的人也要补服务器组件 —— 他们恰恰是「升级 Cursor 后远程窗口出问题」的那批老用户。
    if (args.apply) serverPatch(host, "。已开着的远程窗口关掉重连即可。");
    return 0;
  }
  if (!args.remove) console.log("   (000 = 远端那边确实什么都没有,符合预期)\n");
  else console.log("");

  // ── 5) 算出要改什么 ───────────────────────────────────────────────────
  const fwd = "RemoteForward " + port + " 127.0.0.1:" + port;
  const hit = blocks.filter((x) => x.names.includes(alias) || x.names.includes(host));
  let out = lines.slice();
  let desc = [];

  if (args.remove) {
    const keep = out.filter((l) => !(l.includes(MARK) && l.includes("RemoteForward")));
    const gone = out.length - keep.length;
    if (!gone) { console.log("⑤ ~/.ssh/config 里没有本脚本加过的行 —— 没什么可撤的。"); return 0; }
    out = keep;
    desc.push("删掉 " + gone + " 行带 `" + MARK + "` 标记的 RemoteForward");
    console.log("⑤ 要做的改动:\n     " + desc.join("\n     "));
  } else if (hit.length) {
    const blk = hit[0];
    const already = out.slice(blk.start, blk.end).some((l) => /^\s*RemoteForward\s/i.test(l) && l.includes(" " + port + " "));
    if (already) {
      console.log("⑤ Host " + alias + " 里已经有 RemoteForward " + port + " 了,配置不用改。");
      console.log("   但第 ④ 步量出来远端还是 000 —— 说明那台服务器**禁掉了端口转发**。");
      console.log("   下一步:让那台机的管理员把 sshd_config 里的 AllowTcpForwarding 打开(默认就是开的)。");
      return 1;
    }
    const ind = indentOf(out, blk);
    out.splice(blk.start + 1, 0, ind + fwd + "  " + MARK);
    desc.push("在已有的 `Host " + alias + "` 块里加一行:" + fwd);
    console.log("⑤ 要做的改动:\n     " + desc.join("\n     "));
  } else {
    const user = host.includes("@") ? host.split("@")[0] : os.userInfo().username;
    const add = ["", MARK + " —— 远程窗口里也能用我们的模型(加的,原有配置没动)",
      "Host " + alias, "    HostName " + alias, "    User " + user,
      "    " + fwd + "  " + MARK];
    out = out.concat(add);
    desc.push("新增一个 `Host " + alias + "` 块(含 " + fwd + ")");
    console.log("⑤ 要做的改动:\n     " + desc.join("\n     "));
    console.log("   ⚠️ 你 ssh 配置里原本没有 " + alias + " —— 顺带补上,");
    console.log("      因为 Cursor 的「Connect to Host」下拉只列这个文件里的机器。");
  }

  if (!args.apply) {
    console.log("\n以上是**预演**,一个字都没写。确认后重跑并加 --apply。");
    return 0;
  }

  // ── 6) 写入(先备份) ──────────────────────────────────────────────────
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "");
  if (exists) {
    const bak = SSH_CONFIG + ".bak-" + stamp;
    try { fs.copyFileSync(SSH_CONFIG, bak); }
    catch (e) { console.log("\n❌ 备份写不下去(" + e.message + ") —— 中止,不会在没有退路的情况下改你的 ssh 配置。"); return 3; }
    console.log("\n⑥ 已备份:" + bak);
  } else {
    fs.mkdirSync(SSH_DIR, { recursive: true, mode: 0o700 });
    console.log("\n⑥ ~/.ssh/config 原本不存在,新建(不需要备份)");
  }
  fs.writeFileSync(SSH_CONFIG, out.join("\n"), { mode: 0o600 });
  console.log("   已写入 " + SSH_CONFIG);

  if (args.remove) {
    console.log("\n✅ 撤掉了。已经连着的远程窗口要关掉重开才会生效。");
    return 0;
  }

  // ── 7) 阳性验收:同一把尺子再量一次 ───────────────────────────────────
  const after = probeRemote(host, port, false);
  console.log("\n⑦ 阳性验收 · 远端再打 127.0.0.1:" + port + " → " + after.code + "(" + after.who + ")");
  if (after.who === "other" || after.fwdFailed) {
    // 配置写对了,但隧道没建起来:远端端口被别人占了(ssh 只打一行 warning,连接照建)
    console.log("   ❌ 隧道没建起来:远端 " + port + " 端口被别的程序占着,请求会打到它那里去。");
    console.log("      见上面第④步的两条解法。回滚:重跑并加 --remove --apply。");
    return 4;
  }
  if (after.code === "000") {
    console.log("   ❌ 还是不通。配置写对了但没起作用,最可能是那台服务器禁了端口转发。");
    console.log("      ssh 的原始输出:");
    (after.err || "(没有输出)").split("\n").slice(0, 6).forEach((l) => l && console.log("        " + l));
    console.log("      回滚:重跑并加 --remove --apply,或还原上面那份备份。");
    return 1;
  }
  console.log("   ✅ 通了(401 = 打到你本机的小代理了,只是探针没带 Key —— 这正是我们要的)");
  // ── 8) 服务器上的 Cursor 组件也补一下(远程窗口里 agent 扩展跑在服务器上)────────
  // 本机安装器只补了 Mac 那份;远程窗口用的是服务器 cursor-server 里自带的那份 ⇒ 不补的话
  // 远程窗口里会「每轮都压缩上下文」/ 偶发打到 Cursor 自家推理报 permission_denied。
  // 服务器上要先有和本机**同 commit** 的 cursor-server(Cursor 第一次连上去时自己装)。
  serverPatch(host);

  console.log("\n━━━ 接下来在 Cursor 里 ━━━");
  console.log("  1) 完全退出 Cursor(Cmd+Q),重新打开");
  console.log("  2) 左下角连到 " + alias + ",在那个窗口里随便发一条消息");
  console.log("  Cursor 里**一个设置都不用改** —— 地址还是 http://127.0.0.1:" + port + "/v1,");
  console.log("  只是现在远端的这个地址会被转回你本机。");
  console.log("\n提示:第二个窗口连同一台机时,ssh 可能提示");
  console.log("  \"remote port forwarding failed for listen port " + port + "\"");
  console.log("  —— 这是因为第一个窗口已经占着了,**无害,别去修**,模型照样能用。");
  console.log("  **每次升级 Cursor 后**,连一次服务器、再双击一次本脚本(服务器要补的是新版本那份)。");
  return 0;
}

main().then((c) => process.exit(c)).catch((e) => { console.error("!! " + (e && e.stack || e)); process.exit(4); });
