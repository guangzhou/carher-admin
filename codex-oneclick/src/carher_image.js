#!/usr/bin/env node
/* CarHer 画图工具(Node 版)—— 由三个一键配置(Codex / Claude Code / Cursor)自动安装,用户无需关心。

   为什么有 Node 版:Python 版(carher_image.py)要求电脑有 Python,很多 Windows 同事没有 ⇒ 画图整段不装。
   三个一键脚本本来就会装 Node(codex / claude 命令行要),所以统一用这一份;逻辑与 .py 一致。

   用法:  node carher_image.js "画面描述" [输出路径或目录]
   key / 网关地址按顺序找(先找到的为准),不接受命令行传 key:
     1. CARHER_IMAGE_KEY / CARHER_IMAGE_BASE 环境变量(给测试用)
     2. ~/.codex/auth.json + ~/.codex/config.toml        (Codex 一键)
     3. ~/.claude/settings.json 的 env.ANTHROPIC_API_KEY  (Claude Code 一键;网关地址加 /v1)
     4. ~/.carher/image.json {"key","base"}              (Cursor 一键写的)
   画图模型按 /models 现挑:先 muse 的两个名字(`gemini-image` 是 `muse-image` 的对外名,同一套图)、
   都没开才退到 gpt-image-2 等别家;`CARHER_IMAGE_MODEL` 可覆盖。
   muse 那条路返回相对路径 /v1/media/<32hex>.webp(文件在 muse2api 上),
   198 nginx 开了只读出口 https://<网关域名>/muse-media/<32hex>.<ext> ⇒ 改写过去下载。
   输出文件后缀以实际图片格式为准(webp 就存 .webp)。 */
"use strict";
const fs = require("fs"), os = require("os"), path = require("path");

// 11-02:别写死模型名。画图仍然走 muse —— 变的只是**客户端该写哪个名字**:
//   `gemini-image`(对外公开名)在网关里 alias 到后端组 **`muse-image`**(key/info 的
//   aliases: {"gemini-image": "muse-image"})⇒ 两个名字出的是同一套 muse 图,不是换模型。
// 现量 1743 把活 key:能直接写 `muse-image` 的只剩 3 把,`gpt-image-2` 1142 把 ⇒
// 写死 `muse-image` 的人全部 403(用户 11-02 本机实测:「你的 key 还没有开通画图模型 muse-image」)。
// ⇒ 按 /models(已按这把 key 的白名单过滤)挑:**先找 muse 的两个名字**,都没有才退到别家生图。
const MODEL_ENV = process.env.CARHER_IMAGE_MODEL || "";
// 前两个都是 muse(gemini-image = muse-image 的对外名);后面几个是**别家**的生图,只在 muse 两个名字都没开时兜底
const MODEL_PREF = ["gemini-image", "muse-image", "gpt-image-2", "grok-imagine-image-quality",
                    "grok-imagine-image-2.0", "grok-imagine", "gemini-3.1-flash-image"];
async function pickModel(key, base) {
  if (MODEL_ENV) return MODEL_ENV;
  let ids = [];
  try {
    const r = await fetch(base + "/models", { headers: { Authorization: "Bearer " + key }, signal: AbortSignal.timeout(30000) });
    if (r.ok) ids = ((await r.json()).data || []).map((m) => m && m.id).filter(Boolean);
  } catch (e) { /* 拉不到就按偏好第一个试,错误文案照旧 */ }
  for (const m of MODEL_PREF) if (ids.includes(m)) return m;
  const other = ids.find((i) => /image|imagine/.test(i) && !/edit/.test(i));
  if (other) return other;
  if (ids.length) die("你的 key 没有开通任何画图模型,请联系运维开通(比如 gemini-image)。");
  return MODEL_PREF[0];
}
const SIZE = "1024x1024";
const HOME = os.homedir();

function readJson(p) { try { return JSON.parse(fs.readFileSync(p, "utf8").replace(/^﻿/, "")); } catch (e) { return null; } }
function die(msg) { console.error(msg); process.exit(1); }

function credentials() {
  if (process.env.CARHER_IMAGE_KEY && process.env.CARHER_IMAGE_BASE)
    return [process.env.CARHER_IMAGE_KEY.trim(), process.env.CARHER_IMAGE_BASE.trim().replace(/\/+$/, "")];
  // Codex
  const ch = process.env.CODEX_HOME || path.join(HOME, ".codex");
  const auth = readJson(path.join(ch, "auth.json"));
  if (auth && auth.OPENAI_API_KEY) {
    let base = "";
    try { const m = /^\s*base_url\s*=\s*"([^"]+)"/m.exec(fs.readFileSync(path.join(ch, "config.toml"), "utf8")); if (m) base = m[1]; } catch (e) {}
    if (base) return [String(auth.OPENAI_API_KEY).trim(), base.replace(/\/+$/, "")];
  }
  // Claude Code(网关地址不带 /v1)
  const s = readJson(path.join(process.env.CLAUDE_CONFIG_DIR || path.join(HOME, ".claude"), "settings.json"));
  const env = (s && s.env) || {};
  if (env.ANTHROPIC_API_KEY && env.ANTHROPIC_BASE_URL)
    return [String(env.ANTHROPIC_API_KEY).trim(), String(env.ANTHROPIC_BASE_URL).replace(/\/+$/, "") + "/v1"];
  // Cursor
  const c = readJson(path.join(HOME, ".carher", "image.json"));
  if (c && c.key && c.base) return [String(c.key).trim(), String(c.base).replace(/\/+$/, "")];
  die("找不到 key。请重新运行一次一键配置脚本。");
}

function mediaUrl(url, base) {
  if (/^https?:\/\//.test(url)) return url;
  const m = /^\/v1\/media\/([0-9a-f]{32}\.(?:webp|png|jpe?g))$/.exec(url);
  if (!m) die("生成失败:看不懂返回的图片地址 " + url.slice(0, 120));
  return /^(https?:\/\/[^/]+)/.exec(base)[1] + "/muse-media/" + m[1];
}

async function download(url) {
  let last;
  for (let i = 0; i < 3; i++) {   // 文件刚生成时偶有一两秒还没落盘
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(120000) });
      if (r.ok) return Buffer.from(await r.arrayBuffer());
      last = new Error("HTTP " + r.status);
    } catch (e) { last = e; }
    await new Promise((res) => setTimeout(res, 2000));
  }
  die("图片已生成但下载失败:" + (last && last.message));
}

function fixExt(p, d) {
  let ext = "";
  if (d.slice(0, 4).toString("latin1") === "RIFF" && d.slice(8, 12).toString("latin1") === "WEBP") ext = ".webp";
  else if (d.slice(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) ext = ".png";
  else if (d[0] === 0xff && d[1] === 0xd8 && d[2] === 0xff) ext = ".jpg";
  if (!ext) return p;
  const cur = path.extname(p).toLowerCase();
  if (cur === ext || (ext === ".jpg" && cur === ".jpeg")) return p;
  return p.slice(0, p.length - path.extname(p).length) + ext;
}

async function generate(prompt, out) {
  const [key, base] = credentials();
  const MODEL = await pickModel(key, base);
  const t0 = Date.now();
  console.log("正在生成,约需 30 秒…");
  let r;
  try {
    r = await fetch(base + "/images/generations", {
      method: "POST", signal: AbortSignal.timeout(300000),
      headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" },
      body: JSON.stringify({ model: MODEL, prompt, size: SIZE, n: 1 }),
    });
  } catch (e) { die("生成失败:" + e.message); }
  if (!r.ok) {
    const detail = (await r.text()).slice(0, 300);
    if (r.status === 403) die("你的 key 还没有开通画图模型 " + MODEL + "(这是按你的 key 能用的列表挑的)。请联系运维开通画图模型。");
    if (r.status === 401) die("key 已失效,请找运维重新颁发。");
    die("生成失败(HTTP " + r.status + "):" + detail);
  }
  const item = ((await r.json()).data || [{}])[0] || {};
  let data;
  if (item.b64_json) data = Buffer.from(item.b64_json, "base64");
  else if (item.url) data = await download(mediaUrl(item.url, base));
  else die("生成失败:返回里没有图片数据。");
  out = fixExt(out, data);
  fs.writeFileSync(out, data);
  console.log("已生成:" + out + "(" + (data.length / 1048576).toFixed(1) + " MB,用时 " + Math.round((Date.now() - t0) / 1000) + " 秒)");
}

const args = process.argv.slice(2).filter((a) => a.trim());
if (!args.length) die('用法:node ' + path.basename(process.argv[1]) + ' "画面描述" [输出路径.png]');
const pad = (n) => String(n).padStart(2, "0"), d = new Date();
const name = "image-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + ".png";
let out = args[1] || process.cwd();
// 给的是目录(含 . / 结尾带斜杠)⇒ 在目录里自动起名(10-13:传「.」曾拼成隐藏文件 ..webp)
let isDir = false; try { isDir = fs.statSync(out).isDirectory(); } catch (e) {}
if (isDir || /[\/\\]$/.test(out)) { fs.mkdirSync(out, { recursive: true }); out = path.join(out, name); }
generate(args[0], out).catch((e) => die("生成失败:" + e.message));
