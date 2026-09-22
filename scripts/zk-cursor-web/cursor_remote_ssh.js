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
function probeRemote(host, port, withTunnel) {
  const base = sshBase();
  if (withTunnel) base.push("-R", port + ":127.0.0.1:" + port);
  const remote =
    "if command -v curl >/dev/null 2>&1; then " +
    "curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:" + port + "/v1/models; " +
    "else (exec 3<>/dev/tcp/127.0.0.1/" + port + ") >/dev/null 2>&1 && printf open || printf 000; fi";
  const r = sh("ssh", base.concat([host, remote]), 45000);
  const out = (r.stdout || "").trim();
  return { code: out || "000", err: (r.stderr || "").trim(), rc: r.status };
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

async function main() {
  const argv = process.argv.slice(2);
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
    console.log("   → 这是**公网直连**,不经过本机小代理。");
    console.log("   ✅ 远程窗口天生就能用,不需要配隧道,本脚本到此为止(一个字都没改)。");
    console.log("      (要是远程窗口里还是连不上,那是另一回事:先确认那台机能上外网。)");
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

  if (!args.host) {
    console.log("③ 还没指定要连哪台。你 ~/.ssh/config 里现有的主机:");
    const names = [];
    blocks.forEach((x) => x.names.forEach((n) => { if (!n.includes("*") && !n.includes("?")) names.push(n); }));
    if (names.length) names.forEach((n) => console.log("     " + n));
    else console.log("     (一个都没有 —— 说明你还没配过 ssh 免密,先找管理员配好再来)");
    console.log("\n   重跑并带上主机名,例如:  --host " + (names[0] || "me@1.2.3.4"));
    console.log("   (Cursor 左下角「Connect to Host」下拉**也只列这个文件里的机器**,");
    console.log("    所以这里为空 = Cursor 里也选不到它。)");
    return 0;
  }
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
  console.log("④ 阴性对照 · 远端直接打 127.0.0.1:" + port + " → " + before.code);
  if (before.code !== "000" && !args.remove) {
    console.log("   ✅ 已经通了 —— 说明隧道**已经配好**(或者你已有别的通道)。");
    console.log("      不需要改任何东西。本脚本到此为止。");
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
  console.log("\n⑦ 阳性验收 · 远端再打 127.0.0.1:" + port + " → " + after.code);
  if (after.code === "000") {
    console.log("   ❌ 还是不通。配置写对了但没起作用,最可能是那台服务器禁了端口转发。");
    console.log("      ssh 的原始输出:");
    (after.err || "(没有输出)").split("\n").slice(0, 6).forEach((l) => l && console.log("        " + l));
    console.log("      回滚:重跑并加 --remove --apply,或还原上面那份备份。");
    return 1;
  }
  console.log("   ✅ 通了(401 = 打到你本机的小代理了,只是探针没带 Key —— 这正是我们要的)");
  console.log("\n━━━ 接下来在 Cursor 里 ━━━");
  console.log("  1) 完全退出 Cursor(Cmd+Q),重新打开");
  console.log("  2) 左下角连到 " + alias + ",在那个窗口里随便发一条消息");
  console.log("  Cursor 里**一个设置都不用改** —— 地址还是 http://127.0.0.1:" + port + "/v1,");
  console.log("  只是现在远端的这个地址会被转回你本机。");
  console.log("\n提示:第二个窗口连同一台机时,ssh 可能提示");
  console.log("  \"remote port forwarding failed for listen port " + port + "\"");
  console.log("  —— 这是因为第一个窗口已经占着了,**无害,别去修**,模型照样能用。");
  return 0;
}

main().then((c) => process.exit(c)).catch((e) => { console.error("!! " + (e && e.stack || e)); process.exit(4); });
