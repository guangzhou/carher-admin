#!/usr/bin/env node
/* CarHer 生成视频工具(Node,零依赖)—— 由三个一键配置(Codex / Claude Code / Cursor)自动安装,用户无需关心。

   用法:
     node carher_video.js "一句话描述" [输出路径或目录] [--portrait]   提交并等最多 90 秒
     node carher_video.js --wait [任务号]                              接着等(不写任务号 = 最近一个)
     --portrait / --竖屏  竖屏 720x1280(默认横屏 1280x720;上游只支持这两种)
     --retry              同一个描述刚失败过,确认要再扣一次额度重试

   为什么分「提交」和「接着等」(11-01 在三个 App 里实测出来的):
     一条视频要 1.5~3 分钟,而 agent 的命令工具不擅长等长命令 ——
     Codex App 10 秒就先交回半截输出,模型以为「输出丢了」就自己重跑(多扣额度);
     App 关掉会话时子进程被一起带走(额度照扣、片子没了);Claude Code 默认 2 分钟掐命令。
     ⇒ 真正干活的是一个**脱离会话的后台进程**(会话关了也继续,出完自己落盘);
       前台命令最多等 90 秒就返回,没好就打印一条 --wait 命令让 agent 接着等。
     ⇒ 同一个描述 + 同一个输出位置**不会重复提交**:还在生成就接上去等,刚出完就直接报结果,
       刚失败过要加 --retry 才会再提交。agent 再怎么重跑也只扣一份额度。

   key / 网关地址的找法与 carher_image.js 完全一致(先找到的为准),不接受命令行传 key:
     1. CARHER_IMAGE_KEY / CARHER_IMAGE_BASE 环境变量(给测试用)
     2. ~/.codex/auth.json + ~/.codex/config.toml        (Codex 一键)
     3. ~/.claude/settings.json 的 env.ANTHROPIC_API_KEY  (Claude Code 一键;网关地址加 /v1)
     4. ~/.carher/image.json {"key","base"}              (Cursor 一键写的)

   流程 = OpenAI 视频三段式:POST /videos → 每 10 秒 GET /videos/{id} 直到 completed → GET /videos/{id}/content 存 mp4。
   🔴 同一把 key 同时提交两个视频,网关目前只能取回其中一个(另一个查进度 404,额度照扣)
      ⇒ 本机用一个锁把视频排队:同一时间只跑一个,后来的等前一个出完再提交。 */
"use strict";
const fs = require("fs"), os = require("os"), path = require("path"), crypto = require("crypto");

const MODEL = process.env.CARHER_VIDEO_MODEL || "gemini-video";
const HOME = os.homedir();
const POLL_MS = Number(process.env.CARHER_VIDEO_POLL_MS) || 10000;     // env 只给离线测试用
const WAIT_MS = Number(process.env.CARHER_VIDEO_WAIT_MS) || 90000;     // 前台每次最多等多久(< Claude Code 默认 2 分钟)
const MAX_WAIT_MS = 15 * 60 * 1000;     // 上游最慢的号约 10 分钟才回超时,留足余量
const LOCK_STALE_MS = 20 * 60 * 1000;   // 锁的心跳比这还老 = 持锁进程死了没清,直接接管
const RECENT_MS = 30 * 60 * 1000;       // 这么久以内的同一任务:不重复提交
const STATE_DIR = process.env.CARHER_VIDEO_LOCK_DIR || path.join(HOME, ".carher");
const JOBS = path.join(STATE_DIR, "video-jobs");

let haveLock = false;
function readJson(p) { try { return JSON.parse(fs.readFileSync(p, "utf8").replace(/^﻿/, "")); } catch (e) { return null; } }
function die(msg) { releaseLock(); console.error(msg); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function credentials() {
  if (process.env.CARHER_IMAGE_KEY && process.env.CARHER_IMAGE_BASE)
    return [process.env.CARHER_IMAGE_KEY.trim(), process.env.CARHER_IMAGE_BASE.trim().replace(/\/+$/, "")];
  const ch = process.env.CODEX_HOME || path.join(HOME, ".codex");
  const auth = readJson(path.join(ch, "auth.json"));
  if (auth && auth.OPENAI_API_KEY) {
    let base = "";
    try { const m = /^\s*base_url\s*=\s*"([^"]+)"/m.exec(fs.readFileSync(path.join(ch, "config.toml"), "utf8")); if (m) base = m[1]; } catch (e) {}
    if (base) return [String(auth.OPENAI_API_KEY).trim(), base.replace(/\/+$/, "")];
  }
  // 桌面 App 起的 Claude Code 可能带着别的 CLAUDE_CONFIG_DIR ⇒ 两处都找
  for (const dir of [process.env.CLAUDE_CONFIG_DIR, path.join(HOME, ".claude")]) {
    if (!dir) continue;
    const s = readJson(path.join(dir, "settings.json"));
    const env = (s && s.env) || {};
    if (env.ANTHROPIC_API_KEY && env.ANTHROPIC_BASE_URL)
      return [String(env.ANTHROPIC_API_KEY).trim(), String(env.ANTHROPIC_BASE_URL).replace(/\/+$/, "") + "/v1"];
  }
  const c = readJson(path.join(HOME, ".carher", "image.json"));
  if (c && c.key && c.base) return [String(c.key).trim(), String(c.base).replace(/\/+$/, "")];
  return null;
}

// ── 任务记录:~/.carher/video-jobs/<任务号>.json(原子写:先写临时文件再改名)──
function jobPath(id) { return path.join(JOBS, id + ".json"); }
function readJob(id) { return readJson(jobPath(id)); }
function writeJob(job) {
  job.updated = Date.now();
  const p = jobPath(job.id), tmp = p + "." + process.pid + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(job, null, 2));
  fs.renameSync(tmp, p);
}
function listJobs() {
  let names = [];
  try { names = fs.readdirSync(JOBS).filter((n) => /\.json$/.test(n)); } catch (e) {}
  return names.map((n) => readJson(path.join(JOBS, n))).filter((j) => j && j.id).sort((a, b) => b.created - a.created);
}
function cleanupOld() {                          // 7 天前的记录删掉(只删记录,不碰视频文件)
  for (const j of listJobs()) if (Date.now() - j.created > 7 * 864e5) try { fs.rmSync(jobPath(j.id), { force: true }); } catch (e) {}
}
function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; }
}
function finished(j) { return j.state === "done" || j.state === "failed"; }
// 后台进程死了却没写结局(被强杀 / 关机)⇒ 当失败处理,免得前台永远等
function settle(j) {
  if (j && !finished(j) && !pidAlive(j.pid) && Date.now() - j.updated > 15000) {
    j.state = "failed"; j.msg = "后台生成进程意外退出了(电脑睡眠/关机或被强制结束)。" + (j.videoId ? "任务 id:" + j.videoId : "");
    try { writeJob(j); } catch (e) {}
  }
  return j;
}

// ── 本机排队锁(目录锁:mkdir 是原子的,Mac/Windows 都一样)──
const LOCK = path.join(STATE_DIR, "video.lock");
const OWNER = path.join(LOCK, "owner"), BREAK = LOCK + ".break";
function ownerPid() { try { return parseInt(fs.readFileSync(OWNER, "utf8"), 10) || 0; } catch (e) { return 0; } }
function releaseLock() {
  if (!haveLock) return;
  haveLock = false;
  if (ownerPid() !== process.pid) return;         // 锁已被别人接管(我们拖太久被判死)⇒ 别删别人的锁
  try { fs.rmSync(LOCK, { recursive: true, force: true }); } catch (e) {}
}
// 心跳:持锁期间刷新 owner 的 mtime,「锁老」按最后一次心跳算,不按建锁时间
function touchLock() { if (haveLock && ownerPid() === process.pid) try { fs.writeFileSync(OWNER, String(process.pid)); } catch (e) {} }
function lockState() {                            // "gone" | "alive" | "stale"
  let age;
  try { age = Date.now() - fs.statSync(LOCK).mtimeMs; } catch (e) { return "gone"; }
  try { age = Date.now() - fs.statSync(OWNER).mtimeMs; } catch (e) {}
  const pid = ownerPid();
  let alive = age < 3000;                         // 刚建、还没写 owner:当作活着
  if (pid) alive = pidAlive(pid);
  return alive && age <= LOCK_STALE_MS ? "alive" : "stale";   // pid 被复用时靠心跳超时兜底
}
// 两个排队进程同时发现死锁:各自「删 + mkdir」会把对方刚建的新锁删掉 ⇒ 两个一起提交。
// 用第二把短锁把「判死 + 删」串起来,拿到短锁后重新判一次。
function breakStale() {
  try { fs.mkdirSync(BREAK); } catch (e) {
    if (e.code !== "EEXIST") throw e;
    try { if (Date.now() - fs.statSync(BREAK).mtimeMs > 10000) fs.rmSync(BREAK, { recursive: true, force: true }); } catch (e2) {}
    return;
  }
  try { if (lockState() === "stale") fs.rmSync(LOCK, { recursive: true, force: true }); }
  finally { try { fs.rmdirSync(BREAK); } catch (e) {} }
}
async function acquireLock(job) {
  const t0 = Date.now();
  for (;;) {
    try { fs.mkdirSync(LOCK); haveLock = true; fs.writeFileSync(OWNER, String(process.pid)); return; }
    catch (e) { if (e.code !== "EEXIST") throw e; }
    const st = lockState();
    if (st === "gone") continue;
    if (st === "stale") { breakStale(); await sleep(200); continue; }
    if (job.state !== "queued") { job.state = "queued"; writeJob(job); } else if (Date.now() - job.updated > 10000) writeJob(job);
    if (Date.now() - t0 > LOCK_STALE_MS) throw new Error("等前一个视频等了 20 分钟还没出完,请稍后再试。");
    await sleep(3000);
  }
}

async function api(key, url, opts = {}) {
  let last;
  for (let i = 0; i < 3; i++) {                  // 只重试网络抖动;HTTP 错误码直接交给调用方判断
    try {
      return await fetch(url, { ...opts, signal: AbortSignal.timeout(opts.timeout || 60000),
        headers: { Authorization: "Bearer " + key, ...(opts.headers || {}) } });
    } catch (e) { last = e; await sleep(3000); }
  }
  throw last;
}

function explain(status, detail) {
  if (status === 403) return "你的 key 还没有开通视频模型 " + MODEL + ",请联系运维开通后重试。";
  if (status === 401) return "key 已失效,请找运维重新颁发。";
  if (status === 404 && !/model/i.test(detail))
    return "任务查不到了(HTTP 404,常见原因:同一把 key 另有视频在同时生成),请稍后单独重试一次。原文:" + detail.slice(0, 200);
  if (/quota_exceeded|capacity|already being generated/i.test(detail))
    return "视频服务这会儿忙(上游额度/排队),过几分钟再试。原文:" + detail.slice(0, 200);
  if (/not ready before the deadline/i.test(detail))
    return "上游这次没在时限内出片(偶发),过几分钟再试一次。原文:" + detail.slice(0, 200);
  return "生成失败" + (status ? "(HTTP " + status + ")" : "") + ":" + detail.slice(0, 300);
}

// ── 后台进程:排队 → 提交一次 → 轮询 → 下载落盘,每一步写进任务记录 ──
async function worker(id) {
  const job = readJob(id);
  if (!job) process.exit(2);
  job.pid = process.pid; writeJob(job);
  const fail = (msg) => { releaseLock(); job.state = "failed"; job.msg = msg; writeJob(job); process.exit(1); };
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(sig, () => fail("后台生成进程被结束了。" + (job.videoId ? "任务 id:" + job.videoId : "")));
  process.on("exit", releaseLock);
  try {
    const cred = credentials();
    if (!cred) return fail("找不到 key。请重新运行一次一键配置脚本。");
    const [key, base] = cred;
    await acquireLock(job);
    job.state = "submitting"; writeJob(job);
    let r;
    try {
      // 只提交一次,不重试:生成类请求重试 = 多出一份、多扣一份额度
      r = await fetch(base + "/videos", {
        method: "POST", signal: AbortSignal.timeout(120000),
        headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" },
        body: JSON.stringify({ model: MODEL, prompt: job.prompt, size: job.portrait ? "720x1280" : "1280x720" }),
      });
    } catch (e) { return fail("提交失败:" + e.message); }
    if (!r.ok) return fail(explain(r.status, await r.text()));
    let st = await r.json();
    if (!st || !st.id) return fail("提交失败:返回里没有任务 id。");
    job.videoId = st.id; job.state = "running"; job.started = Date.now(); job.progress = st.progress || 0; writeJob(job);

    while (st.status !== "completed") {
      if (st.status === "failed" || st.status === "cancelled") {
        const e = st.error || {};
        return fail(explain(0, String(e.message || e.code || st.status)));
      }
      if (Date.now() - job.started > MAX_WAIT_MS) return fail("等了 15 分钟还没出来,放弃。任务 id:" + job.videoId);
      await sleep(POLL_MS);
      touchLock();
      const q = await api(key, base + "/videos/" + encodeURIComponent(job.videoId));
      if (!q.ok) return fail(explain(q.status, await q.text()));
      st = await q.json();
      if (st.progress != null && st.progress !== job.progress) { job.progress = st.progress; writeJob(job); }
      else if (Date.now() - job.updated > 30000) writeJob(job);
    }
    job.state = "downloading"; job.progress = 100; writeJob(job);
    let data, last;
    for (let i = 0; i < 3 && !data; i++) {        // 刚完成时文件偶尔还没就绪
      touchLock();
      try {
        const c = await api(key, base + "/videos/" + encodeURIComponent(job.videoId) + "/content", { timeout: 300000 });
        if (c.ok) { data = Buffer.from(await c.arrayBuffer()); break; }
        last = new Error("HTTP " + c.status + " " + (await c.text()).slice(0, 200));
      } catch (e) { last = e; }
      await sleep(3000);
    }
    if (!data) return fail("视频已生成但下载失败:" + (last && last.message));
    if (data.length < 1024 || data.slice(4, 8).toString("latin1") !== "ftyp")
      return fail("下载到的不是视频文件(" + data.length + " 字节):" + data.slice(0, 120).toString("utf8"));
    fs.mkdirSync(path.dirname(job.out), { recursive: true });
    fs.writeFileSync(job.out, data);
    releaseLock();
    job.state = "done"; job.bytes = data.length; job.seconds = Math.round((Date.now() - job.created) / 1000); writeJob(job);
    process.exit(0);
  } catch (e) { fail("生成失败:" + (e && e.message || e)); }
}

// ── 前台:打印进度,最多等 WAIT_MS;好了报结果,没好告诉 agent 怎么接着等 ──
function waitCmd(id) {
  const self = '"' + process.execPath + '" "' + __filename + '"';
  if (process.env.ELECTRON_RUN_AS_NODE) {          // Cursor:用它自带的 node 跑,要带上这个开关
    return process.platform === "win32"
      ? 'cmd /c "set ELECTRON_RUN_AS_NODE=1&& ' + self + ' --wait ' + id + '"'
      : "ELECTRON_RUN_AS_NODE=1 " + self + " --wait " + id;
  }
  return self + " --wait " + id;
}
function describe(j) {
  if (j.state === "queued") return "排队中(前面还有一个视频在生成,同一把 key 一次只能出一个)";
  if (j.state === "running") return "生成中 " + (j.progress || 0) + "%";
  if (j.state === "downloading") return "已生成,正在下载";
  return "准备提交";
}
async function follow(id) {
  const t0 = Date.now();
  let shown = "";
  for (;;) {
    const j = settle(readJob(id));
    if (!j) die("找不到任务 " + id + "(记录可能已过期)。");
    if (j.state === "done") {
      console.log("已生成:" + j.out + "(" + ((j.bytes || 0) / 1048576).toFixed(1) + " MB,用时 " + (j.seconds ?? "?") + " 秒)");
      process.exit(0);
    }
    if (j.state === "failed") die("生成失败:" + j.msg);
    const s = describe(j);
    if (s !== shown) { console.log("  " + Math.round((Date.now() - j.created) / 1000) + " 秒:" + s); shown = s; }
    if (Date.now() - t0 >= WAIT_MS) {
      console.log("");
      console.log("⏳ 视频还没好(" + s + "),它在后台继续生成,关掉对话也不影响。");
      console.log("   不要重新提交(会多扣一次额度)。过一会儿运行下面这条接着等:");
      console.log("   " + waitCmd(id));
      process.exit(0);
    }
    await sleep(2000);
  }
}

function videoName(id) {
  const pad = (n) => String(n).padStart(2, "0"), d = new Date();
  return "video-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + "-" + id.slice(-4) + ".mp4";
}

async function submit(prompt, outArg, portrait, retry) {
  if (!credentials()) die("找不到 key。请重新运行一次一键配置脚本。");
  try { await fetch(credentials()[1] + "/models", { method: "HEAD", signal: AbortSignal.timeout(15000) }); }
  catch (e) {
    const c = (e && e.cause && e.cause.code) || "";
    if (/ENOTFOUND|EAI_AGAIN|EPERM|ECONNREFUSED/.test(c)) die("连不上网关(" + c + "),多半是命令沙箱禁止联网。请让这条命令在沙箱外运行;还没提交,不扣额度。");
  }
  let out = path.resolve(outArg || process.cwd());
  let isDir = false; try { isDir = fs.statSync(out).isDirectory(); } catch (e) {}
  if (!isDir && /[\/\\]$/.test(outArg || "")) isDir = true;
  if (!isDir && !/\.mp4$/i.test(out)) out += ".mp4";
  // 提交前就把目录建好:等 2 分钟、扣完额度才发现父目录不存在 = 白扣一份
  try { fs.mkdirSync(isDir ? out : path.dirname(out), { recursive: true }); } catch (e) { die("输出路径不可用:" + e.message); }
  // Cursor / Codex 的命令沙箱默认不让写 ~/.carher、不让联网 ⇒ 在提交之前就说清楚(此时还没扣额度)
  try { fs.mkdirSync(JOBS, { recursive: true }); fs.accessSync(JOBS, fs.constants.W_OK); }
  catch (e) {
    if (/EPERM|EACCES|EROFS/.test(e.code || "")) die("被命令沙箱拦住了(写不了 " + JOBS + ")。请让这条命令在沙箱外运行(需要联网);还没提交,不扣额度。");
    throw e;
  }
  cleanupOld();

  // 防重复提交:同一描述 + 同一输出位置 + 同一画幅,30 分钟内的任务直接接上
  const sig = crypto.createHash("sha1").update([prompt, isDir ? out + path.sep : out, portrait ? "p" : "l"].join("\n")).digest("hex").slice(0, 16);
  const prev = listJobs().find((j) => j.sig === sig && Date.now() - j.created < RECENT_MS);
  if (prev) {
    settle(prev);
    if (!finished(prev)) { console.log("这个视频已经在生成了(任务 " + prev.id + "),接着等,不会重复提交。"); return follow(prev.id); }
    if (prev.state === "done" && fs.existsSync(prev.out) && !retry) {
      console.log("这个视频刚生成过,直接用上次的结果(要重新生成一条请加 --retry)。");
      return follow(prev.id);
    }
    if (prev.state === "failed" && !retry)
      die("这个视频刚失败过:" + prev.msg + "\n确认要再试一次(会再扣一次额度)请在命令末尾加 --retry。");
  }

  const id = Date.now().toString(36) + crypto.randomBytes(3).toString("hex");
  const job = { id, sig, prompt, portrait, out: isDir ? path.join(out, videoName(id)) : out, state: "starting",
                created: Date.now(), progress: 0, model: MODEL };
  writeJob(job);
  const { spawn } = require("child_process");
  // detached:自成一个进程组/会话 ⇒ agent 的命令被掐、对话被关、App 退出都不会带走它
  const child = spawn(process.execPath, [__filename, "--worker", id], { detached: true, stdio: "ignore", windowsHide: true, env: process.env });
  child.unref();   // 不在这里回写记录:后台进程可能已经写了新状态,前台再写会把它盖回去(只由后台写 pid)
  console.log("已提交(任务 " + id + "),一条约 1.5~3 分钟,下面每隔一会儿报一次进度…");
  return follow(id);
}

const raw = process.argv.slice(2);
if (raw[0] === "--worker") { worker(raw[1]); }
else if (raw[0] === "--wait") {
  const id = raw[1] || (listJobs()[0] || {}).id;
  if (!id) die("没有正在生成的视频。");
  follow(id).catch((e) => die("出错了:" + e.message));
} else {
  const FLAGS = ["--portrait", "--竖屏", "--retry"];
  const portrait = raw.some((a) => a === "--portrait" || a === "--竖屏");
  const retry = raw.includes("--retry");
  const args = raw.filter((a) => a.trim() && !FLAGS.includes(a));
  if (!args.length) die('用法:node ' + path.basename(process.argv[1]) + ' "一句话描述" [输出路径.mp4] [--portrait]\n      node ' + path.basename(process.argv[1]) + ' --wait [任务号]');
  submit(args[0], args[1], portrait, retry).catch((e) => die("生成失败:" + e.message));
}
