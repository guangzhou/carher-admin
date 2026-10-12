/* cursor_remote_patch.js —— 在**服务器**上给 Cursor 远程组件打补丁(件C ctxwin + 件D noloop)。
 *
 * 为什么要这个:Remote-SSH 窗口里,`cursor-agent-exec` / `cursor-agent-host` 这两个扩展
 * **跑在服务器上**(package.json 没声明 extensionKind ⇒ 默认 workspace;服务器的 cursor-server
 * 里自带同名一份)。本机安装器只补了 Mac 上那份 ⇒ 远程窗口里:
 *   - 件C 不生效:服务端若回「k 单位」的 maxTokens(500 = 500k),阈值变成 450 token,
 *     **用了 1000 token 就压缩**(原版远端 3.24.12 实测)。
 *   - 件D 不生效:Cursor 若对这个账号打开 agent_host_local_loop,不带凭据的那一轮会被判
 *     managed-local,打到 Cursor 自家推理服务 ⇒ permission_denied。
 *
 * 怎么用(本机跑,通过 ssh 推到服务器上执行;服务器不用装任何东西,用 cursor-server 自带的 node):
 *   node cursor_remote_patch.js --host me@1.2.3.4            # 预演:只报每个文件打不打得上
 *   node cursor_remote_patch.js --host me@1.2.3.4 --apply    # 真打(先备份,语法过了才落盘)
 *   node cursor_remote_patch.js --host me@1.2.3.4 --revert --apply   # 从备份还原
 *
 * 动了什么 / 备份 / 回滚:
 *   只改服务器上 `~/.cursor-server/bin/linux-x64/<与本机同一个 commit>/extensions/` 下 4 个文件。
 *   改前整份复制成同目录 `<文件>.carher-orig`(已存在就不覆盖 —— 那份才是原厂)。
 *   `--revert` 用 .carher-orig 还原。每次 Cursor 升级 commit 都会变 ⇒ 新 commit 是全新目录,
 *   需要重跑一次本脚本(老 commit 目录不再被用到)。
 *
 * 补丁代码**不复制**:从同目录的 cursor_team_setup.js 原文切出 ctxwin / noloop 两个家族的
 * 常量+函数,原样送到服务器上执行 ⇒ 本机和服务器用的是同一份变换,改一处两边都跟着变。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const cp = require("child_process");

const args = { apply: false, revert: false, host: "" };
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i];
  if (a === "--apply") args.apply = true;
  else if (a === "--revert") args.revert = true;
  else if (a === "--host") args.host = process.argv[++i] || "";
}
if (!args.host) {
  console.log("用法: node cursor_remote_patch.js --host 用户名@服务器 [--apply] [--revert]");
  process.exit(2);
}

// ── 本机 Cursor 的 commit(服务器上那份必须是同一个 commit,否则 Cursor 根本不用它)──
function localCommit() {
  const cands = process.platform === "darwin"
    ? ["/Applications/Cursor.app/Contents/Resources/app/product.json"]
    : process.platform === "win32"
      ? [path.join(process.env.LOCALAPPDATA || "", "Programs", "cursor", "resources", "app", "product.json")]
      : ["/usr/share/cursor/resources/app/product.json", "/opt/Cursor/resources/app/product.json"];
  for (const p of cands) {
    try { return JSON.parse(fs.readFileSync(p, "utf8")).commit; } catch {}
  }
  return null;
}

// ── 从 cursor_team_setup.js 切出两个补丁家族 ──────────────────────────────
// 按**整段行区间**切(不是逐条语句解析:第一版自写的语句切分器把同一个常量切进来两次 ⇒
// 服务器上 `Identifier 'NOLOOP_MARKER' has already been declared`)。区间两端都用
// 源码里稳定的字面量定位,找不到就报错停 —— 安装器结构变了要求人来改,不悄悄切错。
function extractPatchFamilies() {
  const src = fs.readFileSync(path.join(__dirname, "cursor_team_setup.js"), "utf8");
  const span = (from, to, what) => {
    const i = src.indexOf(from);
    const j = i < 0 ? -1 : src.indexOf(to, i);
    if (i < 0 || j < 0) throw new Error("cursor_team_setup.js 里切不到「" + what + "」—— 安装器结构变了,本脚本要跟着改");
    return src.slice(i, j);
  };
  const id = span('\nconst ID = "', "\nconst ID1", "ID 常量");
  const ctx = span("\nconst CTXWIN_MARKER", "\n// 测试钩子:对任意 bundle 跑真实件C", "件C ctxwin 家族");
  const nl = span("\nconst NOLOOP_MARKER", "\nif (process.env.CX_NOLOOP_APPLY_TO_FILE)", "件D noloop 家族");
  // 家族里有引用本机路径/备份的辅助函数(ctxwinPlanFile、noloopBakName 等)—— 只定义不调用,无副作用;
  // 但它们引用的 path 模块要在作用域里,远端程序头部已 require。
  return [id, ctx, nl].join("\n");
}

const TARGETS = [
  { rel: "cursor-local-agent-runtime/dist/main.js", ctx: "min", nl: null },
  { rel: "cursor-agent-host/dist/main.js", ctx: "min", nl: "min" },
  { rel: "cursor-agent-exec/dist/main.js", ctx: "min", nl: null },
  { rel: "cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs", ctx: "src", nl: "src" },
];

// 在服务器上执行的那段:用 cursor-server 自带的 node 跑(服务器上不需要装 node)
function remoteProgram(families, commit, mode) {
  return `
"use strict";
const fs=require("fs"),path=require("path"),os=require("os"),cp=require("child_process");
${families}
const COMMIT=${JSON.stringify(commit)}, MODE=${JSON.stringify(mode)};
const TARGETS=${JSON.stringify(TARGETS)};
const base=path.join(os.homedir(),".cursor-server","bin","linux-x64",COMMIT,"extensions");
if(!fs.existsSync(base)){console.log("NOSERVER "+base);process.exit(3);}
const nodeBin=path.join(os.homedir(),".cursor-server","bin","linux-x64",COMMIT,"node");
let bad=0;
for(const t of TARGETS){
  const f=path.join(base,t.rel), orig=f+".carher-orig";
  if(!fs.existsSync(f)){console.log("MISSING "+t.rel);bad++;continue;}
  if(MODE==="revert"){
    if(!fs.existsSync(orig)){console.log("NOBAK   "+t.rel);continue;}
    fs.copyFileSync(orig,f); console.log("REVERT  "+t.rel); continue;
  }
  let s=fs.readFileSync(f,"utf8"), applied=[];
  const already=s.includes(CTXWIN_MARKER)||s.includes(NOLOOP_MARKER);
  if(already){console.log("DONE    "+t.rel+"(已打过)");continue;}
  if(t.ctx){const r=ctxwinApplyToText(s,t.ctx); if(r.ok){s=r.out;applied.push("ctxwin")} else {console.log("SKIP    "+t.rel+" ctxwin: "+r.why);}}
  if(t.nl){const r=noloopApplyToText(s,t.nl); if(r.ok){s=r.out;applied.push("noloop")} else {console.log("SKIP    "+t.rel+" noloop: "+r.why);}}
  if(!applied.length){bad++;continue;}
  // 语法检查:用服务器自己的 node --check;过不了一个字节都不写
  const tmp=f+".carher-tmp"+(f.endsWith(".cjs")?".cjs":".js");
  fs.writeFileSync(tmp,s);
  const chk=cp.spawnSync(nodeBin,["--check",tmp],{encoding:"utf8"});
  if(chk.status!==0){fs.unlinkSync(tmp);console.log("SYNTAX  "+t.rel+" "+(chk.stderr||"").split("\\n")[0]);bad++;continue;}
  if(MODE==="apply"){
    if(!fs.existsSync(orig)) fs.copyFileSync(f,orig);
    fs.renameSync(tmp,f);
    console.log("PATCHED "+t.rel+" ["+applied.join(",")+"]");
  } else { fs.unlinkSync(tmp); console.log("WOULD   "+t.rel+" ["+applied.join(",")+"]"); }
}
process.exit(bad?5:0);
`;
}

function main() {
  const commit = localCommit();
  if (!commit) { console.log("❌ 读不到本机 Cursor 的版本(装了 Cursor 吗?)"); return 2; }
  console.log("本机 Cursor commit: " + commit.slice(0, 12) + "…");
  console.log("服务器: " + args.host + "\n");
  const mode = args.revert ? (args.apply ? "revert" : "dry") : (args.apply ? "apply" : "dry");
  let families;
  try { families = extractPatchFamilies(); } catch (e) { console.log("❌ " + e.message); return 2; }
  const prog = remoteProgram(families, commit, mode);
  // 用服务器上 cursor-server 自带的 node 执行;-o ClearAllForwardings:这一趟不需要隧道
  const nodePath = "$HOME/.cursor-server/bin/linux-x64/" + commit + "/node";
  const r = cp.spawnSync("ssh", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ClearAllForwardings=yes",
                                 args.host, `if [ -x "${nodePath}" ]; then "${nodePath}" -; else echo NOSERVER; exit 3; fi`],
                         { input: prog, encoding: "utf8", timeout: 180000 });
  const out = (r.stdout || "").trim();
  if (/NOSERVER/.test(out)) {
    console.log("⚠️ 服务器上还没有和你本机同版本的 Cursor 组件。");
    console.log("   先用 Cursor 连一次这台服务器(左下角 → Connect to Host),等它装完,再跑本脚本。");
    return 3;
  }
  if (r.status === null || (r.stderr && !out)) {
    console.log("❌ ssh 执行失败:" + (r.stderr || r.error || "").toString().split("\n")[0]);
    return 1;
  }
  const label = { PATCHED: "✅ 已打", WOULD: "  将打", DONE: "  已打过", REVERT: "✅ 已还原", NOBAK: "  无备份",
                  SKIP: "⚠️ 跳过", SYNTAX: "❌ 语法", MISSING: "❌ 缺文件" };
  for (const line of out.split("\n")) {
    const k = line.split(/\s+/)[0]; console.log((label[k] || "  ") + "  " + line.slice(k.length).trim());
  }
  console.log("");
  if (mode === "dry") console.log("以上是预演,服务器上一个字都没改。确认后加 --apply。");
  else console.log("完成。已经开着的远程窗口要**关掉重连**才会用上(重新 Connect to Host 即可)。");
  return r.status || 0;
}

process.exit(main());
