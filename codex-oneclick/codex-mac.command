#!/usr/bin/env bash
# CarHer Codex 一键配置 (macOS / Linux)
# 用法：Finder 里双击本文件，或终端里执行 `bash codex-mac.command`
# 由 build.sh 生成，别直接改；源在 codex-oneclick/src/
# 没有 codex 命令行会自动装（npmmirror）；Desktop App 需用户自己装（文档里有安装包）
set -euo pipefail

BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; NC=$'\033[0m'
say()  { printf '%s%s%s\n' "$BLUE" "$1" "$NC"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$1" "$NC"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$1" "$NC"; }
die()  { printf '%s%s%s\n' "$RED" "$1" "$NC" >&2; exit 1; }

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

# 固定配置（用户无需感知）
BASE_URL="https://chat.auto-link.com.cn/model/v1"
LEGACY_BASE_URL="https://cc.auto-link.com.cn/pro/v1"   # 10-29 前的旧地址,共存期只在新地址不通时兜底
PROVIDER="carher_prod"
MODEL="gpt-5.6-terra"

echo
say "==================================================="
say "   CarHer Codex 一键配置 (macOS / Linux)"
say "==================================================="
echo

# --- 0. 没有 codex 命令行 / 太旧 ⇒ 自动装最新版 ---
# 和 Claude Code 一键脚本同一套做法：Node 装到 ~/.carher-node、npm 全局装到 ~/.carher-npm-global，
# 都走 npmmirror（不要 sudo、不碰系统、不碰用户原来的 Node/codex）。
# 装失败不阻断：配置照样写，Desktop App 不依赖命令行。
CODEX_MIN="0.144.0"
NPM_REGISTRY="https://registry.npmmirror.com"
NODE_MIRROR="https://cdn.npmmirror.com/binaries/node"
NODE_MAJOR_MIN=18      # @openai/codex 要求 node>=16；自己装时装 22 LTS
NODE_INSTALL_MAJOR=22
NODE_HOME="$HOME/.carher-node"
NPM_PREFIX="$HOME/.carher-npm-global"
[ -x "$NODE_HOME/bin/node" ] && export PATH="$NODE_HOME/bin:$PATH"
[ -d "$NPM_PREFIX/bin" ] && export PATH="$NPM_PREFIX/bin:$PATH"

ver_ge() {  # ver_ge A B ⇒ A >= B(纯数字点分)
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" = "$2" ]
}
node_major() { node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }
cur_codex_ver() { codex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1; }

ensure_node() {
  if command -v node >/dev/null 2>&1 && [ "$(node_major)" -ge "$NODE_MAJOR_MIN" ]; then return 0; fi
  local os arch ext ver pkg tmp want got
  case "$(uname -s)" in Darwin) os=darwin; ext=tar.gz ;; Linux) os=linux; ext=tar.xz ;; *) return 1 ;; esac
  case "$(uname -m)" in x86_64|amd64) arch=x64 ;; arm64|aarch64) arch=arm64 ;; *) return 1 ;; esac
  if [ "$os" = darwin ] && [ "$arch" = x64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ]; then arch=arm64; fi
  say "→ 装一个 Node.js 到 $NODE_HOME(不需要管理员密码，不影响你原来的)"
  # 先整份拿下来再挑(管道里 head 提前关会让 curl 被 SIGPIPE,pipefail 下整条假失败;10-26 Ubuntu 实测)
  local idx; idx="$(curl -fsSL --max-time 30 "$NODE_MIRROR/index.json")" || return 1
  ver="$(printf '%s' "$idx" \
    | grep -oE '"version":"v'"$NODE_INSTALL_MAJOR"'\.[0-9]+\.[0-9]+"[^}]*"lts":"[A-Za-z]+"' \
    | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | sed -n 1p)"
  [ -n "$ver" ] || return 1
  pkg="node-$ver-$os-$arch"; tmp="$(mktemp -d)"
  curl -fSL --max-time 600 --progress-bar -o "$tmp/$pkg.$ext" "$NODE_MIRROR/$ver/$pkg.$ext" || { rm -rf "$tmp"; return 1; }
  # 校验:用官方 SHASUMS256.txt 比对,下坏的/被篡改的包不装
  want="$(curl -fsSL --max-time 30 "$NODE_MIRROR/$ver/SHASUMS256.txt" | awk -v f="$pkg.$ext" '$2==f{print $1}')"
  if command -v shasum >/dev/null 2>&1; then got="$(shasum -a 256 "$tmp/$pkg.$ext" | awk '{print $1}')"
  else got="$(sha256sum "$tmp/$pkg.$ext" | awk '{print $1}')"; fi
  if [ -z "$want" ] || [ "$want" != "$got" ]; then rm -rf "$tmp"; warn "⚠ Node.js 安装包校验失败，已停止"; return 1; fi
  rm -rf "$NODE_HOME"; mkdir -p "$NODE_HOME"
  tar -xf "$tmp/$pkg.$ext" -C "$NODE_HOME" --strip-components=1 || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  export PATH="$NODE_HOME/bin:$PATH"
  [ "$(node_major)" -ge "$NODE_MAJOR_MIN" ]
}

# 以后新开的终端也能直接敲 codex：只追加一个带标记的块,重跑先剥旧块,不碰用户别的内容
add_path_block() {
  local rc="$1" tmp; [ -e "$rc" ] || touch "$rc"
  tmp="$(mktemp)"
  awk '/# >>> carher-codex >>>/{s=1;next} /# <<< carher-codex <<</{s=0;next} !s' "$rc" > "$tmp"
  {
    cat "$tmp"
    echo '# >>> carher-codex >>>'
    [ -x "$NODE_HOME/bin/node" ] && echo "export PATH=\"$NODE_HOME/bin:\$PATH\""
    echo "export PATH=\"$NPM_PREFIX/bin:\$PATH\""
    echo '# <<< carher-codex <<<'
  } > "$rc"
  rm -f "$tmp"
}

CUR="$(cur_codex_ver || true)"
if [ -n "$CUR" ] && ver_ge "$CUR" "$CODEX_MIN"; then
  ok "✔ 检测到 codex 命令行 $CUR"
else
  if [ -n "$CUR" ]; then warn "⚠ codex $CUR 太旧(需要 ≥ $CODEX_MIN),自动升级 ..."
  else say "→ 没有 codex 命令行，自动安装(约 1~3 分钟)..."; fi
  if ensure_node; then
    mkdir -p "$NPM_PREFIX"
    export PATH="$NPM_PREFIX/bin:$PATH"
    if npm_config_prefix="$NPM_PREFIX" npm install -g @openai/codex --registry="$NPM_REGISTRY" \
         --no-fund --no-audit --update-notifier=false --loglevel=error; then
      hash -r 2>/dev/null || true
      for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
        # .bash_profile 只在它已存在时改(macOS 上新建它会让 .bashrc 不再被读)
        [ "$rc" = "$HOME/.bash_profile" ] && [ ! -e "$rc" ] && continue
        add_path_block "$rc"
      done
      CUR="$(cur_codex_ver || true)"
      [ -n "$CUR" ] && ok "✔ codex 命令行 $CUR 已装好" || warn "⚠ 装完了但找不到 codex 命令"
    else
      warn "⚠ 自动安装 codex 命令行失败（配置照样会写好，Desktop App 不受影响；命令行可稍后重跑本脚本）"
    fi
  else
    warn "⚠ 准备 Node.js 失败，跳过安装 codex 命令行（配置照样会写好，Desktop App 不受影响）"
  fi
fi
echo

# --- 1. 读 key ---
say "粘贴你的 API key（sk- 开头，运维发给你的那个），然后回车："
printf "key: "
read -r APIKEY || true
APIKEY="$(printf '%s' "${APIKEY:-}" | tr -d '[:space:]')"
[ -n "$APIKEY" ] || die "✗ 没有输入 key，已取消。"
case "$APIKEY" in
  sk-*) : ;;
  *) warn "⚠ key 不是 sk- 开头，仍然继续（如果是运维给的其它格式忽略此提醒）。" ;;
esac
echo

# --- 1b. 默认模型按这把 key 选 ---
# 🔴 10-16 同事实机:key 只开了 deepseek-v4.1-flash,脚本写死 gpt-5.6-terra ⇒ 每一轮都 403。
#    网关 /models 已按 key 白名单过滤 ⇒ 默认模型在里面就用它,不在就用列表里第一个能聊天的。
#    拿不到列表(网络问题)就保持原默认,不阻断安装。
IDS="$(curl -fsS --max-time 20 -H "Authorization: Bearer $APIKEY" "$BASE_URL/models" 2>/dev/null \
  | grep -oE '"id" *: *"[^"]+"' | sed -E 's/.*"([^"]+)"$/\1/' || true)"
# 10-29 换了网关域名(旧地址还能用一段时间):新地址这会儿拉不到、旧地址拉得到 ⇒ 先用旧地址,
#   每天的模型同步 / 「刷模型」会在新地址通了以后自动换过去。
if [ -z "$IDS" ]; then
  OLD_IDS="$(curl -fsS --max-time 20 -H "Authorization: Bearer $APIKEY" "$LEGACY_BASE_URL/models" 2>/dev/null \
    | grep -oE '"id" *: *"[^"]+"' | sed -E 's/.*"([^"]+)"$/\1/' || true)"
  if [ -n "$OLD_IDS" ]; then
    warn "↳ 新网关地址暂时不通,这次先用旧地址;以后会自动换到新地址。"
    BASE_URL="$LEGACY_BASE_URL"; IDS="$OLD_IDS"
  fi
fi
if [ -n "$IDS" ]; then
  if ! printf '%s\n' "$IDS" | grep -qx "$MODEL"; then
    ALT="$(printf '%s\n' "$IDS" | grep -vE 'image|imagine|^codex-auto-review$' | head -n1)"
    if [ -n "$ALT" ]; then
      warn "↳ 你的 key 没开通 $MODEL,默认模型改用 $ALT"
      MODEL="$ALT"
    fi
  fi
fi

# --- 2. 备份已有配置 ---
mkdir -p "$CODEX_HOME"
TS="$(date +%Y%m%d-%H%M%S)"
BKDIR="$CODEX_HOME/backup-carher-$TS"
BACKED=0
for f in config.toml auth.json chatgpt.config.toml; do
  if [ -f "$CODEX_HOME/$f" ]; then
    mkdir -p "$BKDIR"; cp "$CODEX_HOME/$f" "$BKDIR/$f"; BACKED=1
  fi
done
[ "$BACKED" = "1" ] && warn "↳ 已备份原有配置到：$BKDIR"

# --- 3. 写 config.toml ---
# 顶层 model_provider 让裸 `codex` 和 Desktop App 都默认走 CarHer 网关（WS 增量同时生效）。
# 不用 env_key：GUI App 读不到 shell 环境变量，写了 env_key 而变量缺失会硬报错。
# 不用 [profiles.x]：codex ≥0.147 视为 legacy，与 --profile 组合会报错；改用 chatgpt.config.toml。
cat > "$CODEX_HOME/config.toml" <<EOF
# Generated by CarHer Codex 一键配置 — $TS
model = "$MODEL"
model_reasoning_effort = "medium"
model_provider = "$PROVIDER"
openai_base_url = "$BASE_URL"

# 画图工具要联网：codex exec 默认是只读沙箱，且 workspace-write 沙箱默认断网，
# 两个都不放开的话工具报 nodename nor servname provided。
# ⚠️ sandbox_mode 是顶层裸键，必须写在任何 [table] 之前，否则会被吃进那个表。
sandbox_mode = "workspace-write"

[model_providers.$PROVIDER]
name = "CarHer"
base_url = "$BASE_URL"
wire_api = "responses"
supports_websockets = true
experimental_bearer_token = "$APIKEY"

[sandbox_workspace_write]
network_access = true
EOF
chmod 600 "$CODEX_HOME/config.toml" 2>/dev/null || true
ok "✔ 已写 $CODEX_HOME/config.toml"

# --- 4. 写 chatgpt.config.toml（兼容老命令 codex --profile chatgpt） ---
cat > "$CODEX_HOME/chatgpt.config.toml" <<EOF
model = "$MODEL"
model_provider = "$PROVIDER"
EOF
ok "✔ 已写 $CODEX_HOME/chatgpt.config.toml"

# --- 5. 写 auth.json（apikey 模式，Desktop App 必需） ---
cat > "$CODEX_HOME/auth.json" <<EOF
{
  "auth_mode": "apikey",
  "OPENAI_API_KEY": "$APIKEY"
}
EOF
chmod 600 "$CODEX_HOME/auth.json" 2>/dev/null || true
ok "✔ 已写 $CODEX_HOME/auth.json"

# --- 6. 装生图能力 ---
# 内置画图工具在本环境未注册，Codex 会退化成画 SVG。装一个零依赖小工具 + 一条常驻授权，
# 让 Codex 需要画图时直接调它。
# 🔴 10-16:原来只有 Python 版,没 Python 的同事整段跳过 ⇒ 画不了图。现在优先 Node 版(第 0 步已保证有 Node),
#    没有 Node 才退回 Python 版。工具都内嵌在本脚本里（build.sh 打包时填入），用户只需要这一个文件。
cat > "$CODEX_HOME/carher_image.js" <<'CARHER_PY_EOF'
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
CARHER_PY_EOF
cat > "$CODEX_HOME/carher_image.py" <<'CARHER_PY_EOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CarHer 生图工具 — 由「Codex 一键配置」自动安装，用户无需关心。

只用 Python 标准库，不需要 pip 装任何东西。
key / base_url 从同目录 auth.json + config.toml 读取，不接受命令行传 key。

用法：
  python3 ~/.codex/carher_image.py "画面描述" [输出路径]

2026-10-13 起统一用 muse(用户点名「画图都用 muse 那个工具」)——11-02 起客户端写的是它的对外名 `gemini-image`,
后端还是组 `muse-image`,同一套图;两个名字都没开才退到别家生图。
muse-image 返回的是**相对路径** `/v1/media/<32hex>.webp`(文件在 muse2api 上,经网关拿不到),
198 nginx 加了只读出口 `https://<网关域名>/muse-media/<32hex>.<ext>` ⇒ 这里把相对路径改写过去下载。
输出文件后缀以**实际图片格式**为准(webp 就存 .webp),不把 webp 字节写进 .png 名字里骗看图软件。
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
# 11-02:别写死模型名。画图仍走 muse,变的只是客户端该写哪个名字:`gemini-image` 是后端组
# `muse-image` 的**对外公开名**(alias),同一套图;而能直接写 muse-image 的 key 只剩 3 把 ⇒ 写死就 403。
# 先挑 muse 的两个名字,都没开才退到别家生图。详见 carher_image.js 同处注释。
MODEL_ENV = os.environ.get("CARHER_IMAGE_MODEL", "")
MODEL_PREF = ["gemini-image", "muse-image", "gpt-image-2", "grok-imagine-image-quality",
              "grok-imagine-image-2.0", "grok-imagine", "gemini-3.1-flash-image"]


def pick_model(key, base):
    if MODEL_ENV:
        return MODEL_ENV
    ids = []
    try:
        req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ids = [m.get("id") for m in (json.loads(resp.read()).get("data") or []) if m.get("id")]
    except Exception:
        pass
    for m in MODEL_PREF:
        if m in ids:
            return m
    other = next((i for i in ids if ("image" in i or "imagine" in i) and "edit" not in i), None)
    if other:
        return other
    if ids:
        sys.exit("你的 key 没有开通任何画图模型，请联系运维开通（比如 gemini-image）。")
    return MODEL_PREF[0]
DEFAULT_SIZE = "1024x1024"


def read_credentials():
    """key 取自 auth.json，base_url 取自 config.toml —— 与一键脚本写入的位置一致。"""
    # 🔴 先读 auth.json(一键脚本写的那把),环境变量只作兜底:用户 shell 里常有一把**别的**
    #    OPENAI_API_KEY(10-13 本机实测:env 里那把没开 muse-image ⇒ 每次 403),
    #    Codex 本身用的是 auth.json 那把,画图要和它一致。
    key = ""
    try:
        with open(os.path.join(CODEX_HOME, "auth.json"), encoding="utf-8") as f:
            key = (json.load(f).get("OPENAI_API_KEY") or "").strip()
    except Exception:
        pass
    if not key:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("找不到 key。请重新运行一次「Codex 一键配置」脚本。")

    base = ""
    if True:
        try:
            with open(os.path.join(CODEX_HOME, "config.toml"), encoding="utf-8") as f:
                m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', f.read(), re.M)
            if m:
                base = m.group(1).strip()
        except Exception:
            pass
    if not base:
        base = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base:
        sys.exit("找不到网关地址。请重新运行一次「Codex 一键配置」脚本。")
    return key, base.rstrip("/")


def media_url(url, base):
    """相对路径 /v1/media/<hex>.<ext> → https://<网关根>/muse-media/<hex>.<ext>;绝对 URL 原样。"""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    m = re.match(r"^/v1/media/([0-9a-f]{32}\.(?:webp|png|jpe?g))$", url)
    if not m:
        sys.exit("生成失败：看不懂返回的图片地址 %s" % url[:120])
    root = re.match(r"^(https?://[^/]+)", base).group(1)
    return root + "/muse-media/" + m.group(1)


def download(url):
    last = None
    for _ in range(3):   # 文件刚生成时偶有一两秒还没落盘
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2)
    sys.exit("图片已生成但下载失败：%s: %s" % (type(last).__name__, last))


def fix_ext(path, data):
    """按文件头认格式,后缀不对就改成对的(webp 存成 .png 会让不少看图软件打不开)。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    else:
        return path
    root, cur = os.path.splitext(path)
    return path if cur.lower() in ((ext, ".jpeg") if ext == ".jpg" else (ext,)) else root + ext


def generate(prompt, out_path):
    key, base = read_credentials()
    model = pick_model(key, base)
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "size": DEFAULT_SIZE,
        "n": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/images/generations",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )

    started = time.time()
    print("正在生成，约需 30 秒…", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 403:
            sys.exit("你的 key 还没有开通画图模型 %s（这是按你的 key 能用的列表挑的）。请联系运维开通画图模型。" % model)
        if e.code == 401:
            sys.exit("key 已失效，请找运维重新颁发。")
        sys.exit("生成失败（HTTP %s）：%s" % (e.code, detail[:300]))
    except Exception as e:
        sys.exit("生成失败：%s: %s" % (type(e).__name__, e))

    item = (payload.get("data") or [{}])[0]
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        data = download(media_url(item["url"], base))
    else:
        sys.exit("生成失败：返回里没有图片数据。")

    out_path = fix_ext(out_path, data)
    with open(out_path, "wb") as f:
        f.write(data)
    print("已生成：%s（%.1f MB，用时 %.0f 秒）"
          % (out_path, len(data) / 1048576.0, time.time() - started))
    return out_path


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if not args:
        sys.exit('用法：python3 %s "画面描述" [输出路径.png]' % sys.argv[0])
    prompt = args[0]
    name = "image-%s.png" % time.strftime("%Y%m%d-%H%M%S")
    out = args[1] if len(args) > 1 else os.getcwd()
    # 🔴 10-13 本机回归:Codex 传了「.」当输出路径 ⇒ 原来拼成 `..webp`(隐藏文件,用户找不到)。
    #    给的是目录(含 . / 结尾带斜杠)⇒ 在目录里自动起名;没有后缀的普通名字照常补。
    if os.path.isdir(out) or out.endswith(("/", "\\")):
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, name)
    generate(prompt, out)   # 后缀按实际格式自动改(muse-image 出的是 .webp)


if __name__ == "__main__":
    main()
CARHER_PY_EOF
# 11-01:一句话生成视频(gemini-video),同一套 key 查找;只有 Node 版
cat > "$CODEX_HOME/carher_video.js" <<'CARHER_PY_EOF'
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
CARHER_PY_EOF
cat > "$CODEX_HOME/carher_models_sync.py" <<'CARHER_PY_EOF'
#!/usr/bin/env python3
"""把这把 key 在 CarHer 网关上能用的全部模型同步进 Codex 的模型选择器。

只用 Python 标准库。key / base_url 从 CODEX_HOME 下的 auth.json + config.toml 读，
与一键脚本写入的位置一致。可反复运行（定时任务每天跑一次）。

流程：
  1. GET {base_url}/models —— 网关已按 key 的白名单过滤，列出来的就是能用的
  2. 模板取本机 codex 自带的目录（codex debug models --bundled），
     字段格式自动跟随用户的 codex 版本，避免 schema 漂移导致 codex 起不来
  3. 生成新目录 → 用 codex 自己解析一遍 → 通过才原子替换
  4. config.toml 里没有 model_catalog_json 就补一行

任何一步失败都不动现有文件：目录写坏了 codex 会直接启动失败。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
CONFIG = CODEX_HOME / "config.toml"
DEFAULT_CATALOG = CODEX_HOME / "carher-models.json"
# 非 OpenAI 模型套 gpt-5.5 的元数据：它是普通函数调用。5.6/6 系的 tool_mode=code_mode_only +
# use_responses_lite 是 OpenAI 专属工具形状，套给 grok 实测拿不到 shell，一个文件都建不出来。
TEMPLATE_SLUG = "gpt-5.5"
# 不进选择器：生图模型不能对话；审查专用模型 codex 自己会调
SKIP = re.compile(r"image|imagine|video|music|^codex-auto-review$")  # 出图/视频/音乐生成名不能对话
PREFERRED = "gpt-5.6-terra"  # 与一键脚本的默认模型一致
# 10-29 换对外域名:旧 /pro/v1 → chat.auto-link.com.cn/model/v1(同一个网关)。共存期新旧都算我们的;
# 是旧的 ⇒ 先用这把 key 打新地址 /models,拿到列表才改写 config.toml(只改值等于旧地址的那几行)。
NEW_BASE = os.environ.get("CARHER_MIGRATE_TO") or "https://chat.auto-link.com.cn/model/v1"  # 环境变量只给测试用
LEGACY_BASE = re.compile(r"^https://cc\.auto-link\.com\.cn/pro/v1/?$")


LOG_FILE = CODEX_HOME / "carher-models-sync.log"


def log(msg: str) -> None:
    line = f"[carher-models] {msg}"
    if sys.stdout is not None:  # pythonw（Windows 定时任务）没有 stdout
        print(line, flush=True)
    try:  # 定时任务里没人看终端，自己写日志，不依赖任务的输出重定向
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def read_credentials() -> tuple[str, str]:
    key = ""
    try:
        key = json.loads((CODEX_HOME / "auth.json").read_text(encoding="utf-8")).get("OPENAI_API_KEY", "")
    except (OSError, ValueError):
        pass
    m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', CONFIG.read_text(encoding="utf-8"), re.M)
    if not key or not m:
        raise RuntimeError("auth.json 里没有 key 或 config.toml 里没有 base_url，先跑一键配置脚本")
    return key, m.group(1).rstrip("/")


def fetch_model_ids(key: str, base_url: str) -> list[str]:
    req = urllib.request.Request(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp).get("data", [])
    return sorted({x["id"] for x in data if isinstance(x, dict) and x.get("id")}, key=str.casefold)


def fetch_input_limits(key: str, base_url: str) -> dict[str, int]:
    """网关 /model/info 里每个模型的输入上限（多条部署取最小）。拿不到就返回空，不影响同步。

    模板的 context_window 是 272000；claude-haiku 实际只有 200000，不压下来的话
    codex 会在超过 200k 后才想起压缩，请求先被上游拒掉。
    """
    req = urllib.request.Request(f"{base_url}/model/info", headers={"Authorization": f"Bearer {key}"})
    limits: dict[str, int] = {}
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.load(resp).get("data", [])
    except Exception as exc:
        log(f"读不到模型上下文上限，沿用默认值：{exc}")
        return limits
    for row in rows:
        name = row.get("model_name") if isinstance(row, dict) else None
        cap = (row.get("model_info") or {}).get("max_input_tokens") if name else None
        if isinstance(cap, int) and cap > 0:
            limits[name] = min(cap, limits.get(name, cap))
    return limits


def codex_candidates() -> list[str]:
    """本机所有 codex 候选,按优先级。10-21 guozhenhua:先查到了老的 /opt/homebrew 版(没有 debug models)
    ⇒ 同步失败。⇒ 一键装的 ~/.carher-npm-global 排第一,load_templates 失败就换下一个。与 Node 版一致。"""
    out: list[str] = []

    def add(c):
        if c and Path(c).exists() and c not in out:
            out.append(c)

    for pre in (Path.home() / ".carher-npm-global" / "bin" / "codex",):
        add(str(pre))
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows npm 装的：优先真二进制，绕开 codex.cmd 这层 cmd 转义
        npm = Path(appdata) / "npm" / "node_modules" / "@openai"
        try:
            hit = next(npm.glob("codex*/**/codex.exe"), None)
        except OSError:
            hit = None
        if hit:
            add(str(hit))
    candidates = [
        shutil.which("codex"),
        "/Applications/Codex.app/Contents/Resources/codex",
        "/Applications/OpenAI Codex.app/Contents/Resources/codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:  # Windows App 常见位置
        candidates += [str(Path(local) / "Programs" / "codex" / "codex.exe")]
    for c in candidates:
        add(c)
    # Windows 只装了 Desktop App（msix）时，codex.exe 在 App 包目录里
    for root in (os.environ.get("ProgramFiles"), local):
        if not root:
            continue
        for pattern in ("WindowsApps/OpenAI.Codex*/**/codex.exe", "Programs/*odex*/**/codex.exe"):
            try:
                hit = next(Path(root).glob(pattern), None)
            except OSError:  # WindowsApps 目录可能没有列目录权限
                hit = None
            if hit:
                add(str(hit))
    return out


def run_codex(codex: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CODEX_HOME=str(CODEX_HOME))
    # codex 输出 UTF-8（含中文/符号）；不写 encoding 会按系统编码解，中文 Windows(GBK) 直接解码失败
    # 输出很大（0.166 的 --bundled 有 728KiB 且在涨）；Python 这边 capture_output 没有上限，
    # 不用像 .js 那样设 maxBuffer（那边默认 1MiB，11-03 真机踩到 ENOBUFS）。
    return subprocess.run([codex, "debug", "models", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, env=env)


def load_templates(cands: list[str], current: Path) -> tuple[str | None, list[dict]]:
    """依次试候选,第一个能吐出自带目录的就用它(也用它校验新目录)。"""
    why = []
    for c in cands:
        r = run_codex(c, "--bundled")
        if r.returncode == 0:
            try:
                return c, json.loads(r.stdout)["models"]
            except (ValueError, KeyError):
                why.append(f"{c}:输出不是 JSON")
                continue
        err = " ⏎ ".join(l.strip() for l in r.stderr.strip().splitlines())[:400]
        why.append(f"[{c} debug models --bundled] rc={r.returncode} {err}")
    if why:
        log(f"这些 codex 拿不到自带目录(版本太老?):{' | '.join(why)}")
    if current.exists():
        return None, json.loads(current.read_text(encoding="utf-8"))["models"]
    if cands:
        raise RuntimeError("本机的 codex 都太老(没有 debug models),也没有现有目录可当模板:重跑一次一键配置装新版")
    raise RuntimeError("找不到 codex，也没有现有目录可当模板")


def build_catalog(ids: list[str], templates: list[dict], limits: dict[str, int]) -> dict:
    by_slug = {m["slug"]: m for m in templates if m.get("slug")}
    base = by_slug.get(TEMPLATE_SLUG) or next(iter(by_slug.values()))
    wanted = [i for i in ids if not SKIP.search(i)]
    out: list[dict] = []
    # 官方自带的模型保留官方元数据，按官方顺序排在前面
    for slug, m in by_slug.items():
        m = copy.deepcopy(m)
        m["visibility"] = "list" if slug in wanted else "hide"
        out.append(m)
    for slug in wanted:
        if slug in by_slug:
            continue
        m = copy.deepcopy(base)
        m.update(slug=slug, display_name=slug, description=f"CarHer 网关模型：{slug}",
                 visibility="list", supported_in_api=True)
        m.update(tool_mode=None, use_responses_lite=False, multi_agent_version=None,
                 additional_speed_tiers=[], service_tiers=[])  # 模板版本变了也钉住
        # apply_patch 用普通 JSON 函数参数，不用 freeform：非 OpenAI 模型不会写裸 patch 语法。
        # 10-09 实测 freeform 下 kimi/composer/claude-sonnet-5/5.5 反复报
        # "first line of the patch must be '*** Begin Patch'"，sonnet-5.5 还会回 DONE 但文件没改；
        # 改 null 后 8 个模型改文件 0 报错、全部改对。
        m["apply_patch_tool_type"] = None
        m.pop("upgrade", None)
        m.pop("availability_nux", None)
        out.append(m)
    for priority, m in enumerate(out, start=1):
        m["priority"] = priority
        cap = limits.get(m["slug"])
        if cap:  # 只往下压，不放大
            for field in ("context_window", "max_context_window"):
                if isinstance(m.get(field), int) and m[field] > cap:
                    m[field] = cap
    return {"models": out}


def fix_default_model(text: str, shown: list[str], preferred: str, our_provider: str | None) -> dict | None:
    """config 里钉着的 model 已不在这把 key 里 ⇒ 每一轮都 403。换成 preferred(在的话)或列表第一个。
    只动顶层 `model = "…"`,且只在顶层 model_provider 是我们网关时才动。与 Node 版 fixDefaultModel 一致。"""
    first = re.search(r"^\[", text, re.M)
    head = text[: first.start()] if first else text
    m = re.search(r'^(\s*model\s*=\s*)"([^"]*)"', head, re.M)
    if not m or not shown or m.group(2) in shown:
        return None
    pv = re.search(r'^\s*model_provider\s*=\s*"([^"]+)"', head, re.M)
    if not pv or pv.group(1) != our_provider:
        return None
    nxt = preferred if preferred in shown else shown[0]
    return {"text": text[: m.start()] + m.group(1) + json.dumps(nxt) + text[m.end():], "from": m.group(2), "to": nxt}


def our_provider_name(text: str, base: str) -> str | None:
    """config.toml 里 base_url 等于这次同步所用网关的那个 [model_providers.X] 的名字 X。"""
    for m in re.finditer(r"^\[model_providers\.([^\]\s]+)\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S):
        bu = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', m.group(2), re.M)
        if bu and bu.group(1).rstrip("/") == str(base).rstrip("/"):
            return m.group(1)
    return None


def is_legacy_base(url: str | None) -> bool:
    return bool(url) and bool(LEGACY_BASE.match(str(url).strip()))


def migrate_base_urls(text: str, new_base: str) -> str | None:
    """把 config.toml 里**值等于旧网关地址**的 `base_url` / `openai_base_url` 行改成 new_base,别的一个字不动
    (用户自己配的其它 provider 地址不同,天然不会被碰)。没有要改的返回 None。与 Node 版 migrateBaseUrls 一致。"""
    n = 0

    def repl(m):
        nonlocal n
        if not is_legacy_base(m.group(3)):
            return m.group(0)
        n += 1
        return m.group(1) + m.group(2) + new_base + m.group(4)

    out = re.sub(r'^(\s*(?:openai_)?base_url\s*=\s*)(")([^"]*)(")', repl, text, flags=re.M)
    return out if n else None


def catalog_path_from_config(text: str) -> Path | None:
    m = re.search(r"""^\s*model_catalog_json\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')""", text, re.M)
    if not m:
        return None
    raw = m.group(1)
    value = json.loads(raw) if raw.startswith('"') else raw[1:-1]
    return Path(value).expanduser()


def toml_str(value: object) -> str:
    # json 字符串转义规则是 TOML 基本字符串的子集：反斜杠、引号、中文路径都安全
    return json.dumps(str(value), ensure_ascii=False)


def ensure_config_line(text: str, path: Path) -> str:
    line = f"model_catalog_json = {toml_str(path)}\n"
    first_table = re.search(r"^\[", text, re.M)  # 顶层键必须在任何 [table] 之前
    if first_table is None:
        return text.rstrip("\n") + "\n" + line
    return text[: first_table.start()] + line + "\n" + text[first_table.start():]


def main() -> int:
    key, base_url = read_credentials()
    ids = None
    if is_legacy_base(base_url):  # 10-29 换域名:新地址用这把 key 拉得到列表才改
        try:
            ids = fetch_model_ids(key, NEW_BASE) or None
        except Exception as exc:
            log(f"新网关地址暂时不通，这次照旧用老地址：{exc}")
        if ids:
            t = migrate_base_urls(CONFIG.read_text(encoding="utf-8"), NEW_BASE)
            if t is not None:
                CONFIG.write_text(t, encoding="utf-8")
            log(f"网关地址已换成新地址 {NEW_BASE}（同一个网关，换了域名）")
            base_url = NEW_BASE
    if ids is None:
        ids = fetch_model_ids(key, base_url)
    if not ids:
        raise RuntimeError("网关没返回任何模型，保持现有目录不变")

    config_text = CONFIG.read_text(encoding="utf-8")
    target = catalog_path_from_config(config_text) or DEFAULT_CATALOG
    codex, templates = load_templates(codex_candidates(), target)
    catalog = build_catalog(ids, templates, fetch_input_limits(key, base_url))
    try:
        before = [m["slug"] for m in json.loads(target.read_text(encoding="utf-8"))["models"] if m.get("visibility") == "list"]
    except (OSError, ValueError, KeyError, TypeError):
        before = []

    fd, tmp_name = tempfile.mkstemp(prefix=".carher-models-", suffix=".json", dir=str(CODEX_HOME))
    tmp = Path(tmp_name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    try:
        if codex:  # 让 codex 自己解析一遍，通不过就不替换
            # 值不加引号：-c 的值解析 TOML 失败时按原样字符串用，路径天然不是合法 TOML。
            # 这样 Windows 走 codex.cmd 时也不用经过 cmd 的引号转义。
            r = run_codex(codex, "-c", f"model_catalog_json={tmp}")
            if r.returncode != 0:
                raise RuntimeError(f"codex 解析新目录失败，未替换：{r.stderr.strip()[:300]}")
        if target.exists():
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    if catalog_path_from_config(config_text) is None:
        CONFIG.write_text(ensure_config_line(config_text, target), encoding="utf-8")
        log(f"已在 config.toml 加 model_catalog_json -> {target}")

    shown = [m["slug"] for m in catalog["models"] if m["visibility"] == "list"]
    log(f"同步完成：选择器 {len(shown)} 个模型（网关可见 {len(ids)} 个）-> {target}")
    added = [x for x in shown if x not in before]
    removed = [x for x in before if x not in shown]
    if before and added:
        log(f"新增 {len(added)}：{', '.join(added)}")
    if before and removed:
        log(f"删除 {len(removed)}：{', '.join(removed)}")
    if before and not added and not removed:
        log("和上次一样，没有变化")
    ours = our_provider_name(config_text, base_url)
    for f in (CONFIG, CODEX_HOME / "chatgpt.config.toml"):
        try:
            t = f.read_text(encoding="utf-8")
        except OSError:
            continue
        r = fix_default_model(t, shown, PREFERRED, ours)
        if r:
            f.write_text(r["text"], encoding="utf-8")
            log(f"默认模型 {r['from']} 你的 key 已不能用，改成 {r['to']}（{f.name}）")
    log("Codex App 要完全退出重开才会看到新模型")
    return 0


if __name__ == "__main__":
    # 定时任务把输出写进日志文件时，英文 Windows 的默认编码(cp1252)写不了中文，会在打印日志时崩
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # None（pythonw）或不支持
            pass
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512 * 1024:  # 每天几行，防止无限长
        LOG_FILE.write_text("", encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as exc:  # 定时任务里跑，失败只留日志、不动现有配置
        log(f"失败：{exc}")
        raise SystemExit(1)
CARHER_PY_EOF
cat > "$CODEX_HOME/carher_models_sync.js" <<'CARHER_PY_EOF'
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
// 10-29 换对外域名(同 Python 版):旧 /pro/v1 → chat.auto-link.com.cn/model/v1;新地址拉得到列表才改写
const NEW_BASE = process.env.CARHER_MIGRATE_TO || "https://chat.auto-link.com.cn/model/v1";   // 环境变量只给测试用
const LEGACY_BASE = /^https:\/\/cc\.auto-link\.com\.cn\/pro\/v1\/?$/;
const isLegacyBase = (u) => !!u && LEGACY_BASE.test(String(u).trim());

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
  const which = process.platform === "win32" ? null : cp.spawnSync("sh", ["-c", "command -v codex"], { encoding: "utf8", maxBuffer: CODEX_MAX_BUFFER }).stdout.trim();
  if (which) {
    let real = which; try { real = fs.realpathSync(which); } catch (e) {}
    add(real.endsWith(".js") ? [process.execPath, [real]] : [real, []]);
  }
  for (const c of ["/Applications/Codex.app/Contents/Resources/codex", "/Applications/OpenAI Codex.app/Contents/Resources/codex"])
    if (fs.existsSync(c)) add([c, []]);
  return out;
}

// 🔴 2026-11-03 shixiaojie(Win, codex 0.166):`spawnSync ... ENOBUFS` ⇒ 整条同步失败,
//    下拉里只剩自带的几个 GPT。真因是 **spawnSync 的默认 maxBuffer 只有 1MiB**,
//    而 `codex debug models --bundled` 的输出随版本在涨(0.153=506KiB、0.166=728KiB),
//    已经逼近 1MiB ⇒ 超一点就 ENOBUFS。给 64MiB,留一个数量级余量。
const CODEX_MAX_BUFFER = 64 * 1024 * 1024;

function runCodex(codex, args) {
  const [bin, pre] = codex;
  return cp.spawnSync(bin, [...pre, "debug", "models", ...args], {
    encoding: "utf8", timeout: 60000, maxBuffer: CODEX_MAX_BUFFER,
    env: Object.assign({}, process.env, { CODEX_HOME }), windowsHide: true,
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
    // 10-21 guozhenhua:只记第一行时日志里只剩 clap 的 usage 尾巴,看不出是哪个 codex、什么参数被拒 ⇒ 记全
    const err = String(r.stderr || r.error || "").trim().replace(/\s*\n\s*/g, " ⏎ ").slice(0, 400);
    why.push(`[${[c[0], ...c[1]].join(" ")} debug models --bundled] rc=${r.status} ${err}`);
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

/* 值等于旧网关地址的 base_url / openai_base_url 行改成 newBase,别的不动;没有要改的返回 null。与 Python 版一致。 */
function migrateBaseUrls(text, newBase) {
  let n = 0;
  const out = text.replace(/^(\s*(?:openai_)?base_url\s*=\s*)(")([^"]*)(")/gm, (all, a, q1, v, q2) => {
    if (!isLegacyBase(v)) return all;
    n++; return a + q1 + newBase + q2;
  });
  return n ? out : null;
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
  let [key, base] = readCredentials();
  let ids = null;
  if (isLegacyBase(base)) {   // 10-29 换域名:新地址用这把 key 拉得到列表才改
    try { ids = (await fetchModelIds(key, NEW_BASE)); if (!ids.length) ids = null; }
    catch (e) { log(`新网关地址暂时不通，这次照旧用老地址：${e.message}`); }
    if (ids) {
      const t = migrateBaseUrls(fs.readFileSync(CONFIG, "utf8"), NEW_BASE);
      if (t !== null) fs.writeFileSync(CONFIG, t, "utf8");
      log(`网关地址已换成新地址 ${NEW_BASE}（同一个网关，换了域名）`);
      base = NEW_BASE;
    }
  }
  if (!ids) ids = await fetchModelIds(key, base);
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

module.exports = { buildCatalog, catalogPathFromConfig, ensureConfigLine, fixDefaultModel, ourProviderName, codexCandidates, loadTemplates, migrateBaseUrls, isLegacyBase };

if (require.main === module) {
  try { if (fs.existsSync(LOG_FILE) && fs.statSync(LOG_FILE).size > 512 * 1024) fs.writeFileSync(LOG_FILE, ""); } catch (e) {}
  main().then((rc) => process.exit(rc), (e) => { log(`失败：${e.message}`); process.exit(1); });
}
CARHER_PY_EOF
chmod +x "$CODEX_HOME/carher_image.py" "$CODEX_HOME/carher_image.js" "$CODEX_HOME/carher_video.js" 2>/dev/null || true

# 探测真正能跑的 python：/usr/bin/python3 在没装开发者工具的机器上是个会弹窗的桩，
# 光看命令存在不算数，必须实际执行一次才算可用。
PYBIN=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import json,urllib.request' >/dev/null 2>&1; then
    PYBIN="$(command -v "$c")"; break
  fi
done
# Node 用绝对路径写进 AGENTS.md / 定时任务:Codex 和 launchd 的 PATH 里未必有 ~/.carher-node
NODEBIN="$(command -v node 2>/dev/null || true)"
if [ -n "$NODEBIN" ] && [ "$("$NODEBIN" -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -lt 18 ]; then NODEBIN=""; fi
if [ -n "$NODEBIN" ]; then IMG_CMD="\"$NODEBIN\" \"$CODEX_HOME/carher_image.js\""
elif [ -n "$PYBIN" ]; then IMG_CMD="\"$PYBIN\" \"$CODEX_HOME/carher_image.py\""
else IMG_CMD=""; fi

if [ -n "$IMG_CMD" ]; then
  # AGENTS.md 用标记块管理：重复运行只替换本块，不动用户自己写的内容
  AGENTS="$CODEX_HOME/AGENTS.md"
  BEGIN="<!-- CARHER-IMAGE-BEGIN -->"
  END="<!-- CARHER-IMAGE-END -->"
  if [ -f "$AGENTS" ]; then
    [ -d "$BKDIR" ] || mkdir -p "$BKDIR"; cp "$AGENTS" "$BKDIR/AGENTS.md" 2>/dev/null || true
    awk -v b="$BEGIN" -v e="$END" '
      $0==b {skip=1} skip==0 {print} $0==e {skip=0}
    ' "$AGENTS" > "$AGENTS.tmp" && mv "$AGENTS.tmp" "$AGENTS"
  fi
  cat >> "$AGENTS" <<EOF
$BEGIN
## 画图

用户要画图、生成图片、出图、做配图/插画/海报/头像时，**只用下面这个命令**（它调公司的画图模型），
**不要询问、不要用 SVG / ASCII / HTML / matplotlib 代替，也不要用内置 image_gen 或别的生图方式**：

    $IMG_CMD "画面描述" 输出路径

- 画面描述用用户的原话，可以补充风格、构图、颜色等细节。
- 约 30 秒出图。key 已配好，不要向用户索要，也不要在命令里写 key。
- 输出文件的后缀会按实际格式自动改（一般是 .webp），以命令最后打印的「已生成：…」路径为准，把这个路径告诉用户。
- 命令报错就把报错原文告诉用户，不要换别的方式硬画。
$END
EOF
  ok "✔ 已开通生图能力"
else
  warn "⚠ 没有 Node 也没有 Python，生图能力未开通（其余配置已生效）"
fi

# --- 6b. 一句话生成视频(11-01)---
# 和画图同一个路子:AGENTS.md 一个标记块 + 零依赖 Node 小工具(调 gemini-video)。
# 视频要 1~3 分钟,块里写明「耐心等、别重跑」——重跑 = 多扣一份额度。
if [ -n "$NODEBIN" ]; then
  AGENTS="$CODEX_HOME/AGENTS.md"
  VBEGIN="<!-- CARHER-VIDEO-BEGIN -->"
  VEND="<!-- CARHER-VIDEO-END -->"
  if [ -f "$AGENTS" ]; then
    awk -v b="$VBEGIN" -v e="$VEND" '
      $0==b {skip=1} skip==0 {print} $0==e {skip=0}
    ' "$AGENTS" > "$AGENTS.tmp" && mv "$AGENTS.tmp" "$AGENTS"
  fi
  cat >> "$AGENTS" <<EOF
$VBEGIN
## 生成视频

用户要生成视频、做短视频、出一段视频时，**只用下面这个命令**（它调公司的 gemini-video 真实生成 10 秒带声音的 mp4），
**不要说「没法生成视频」，不要用代码画动画 / PIL / ffmpeg / HTML 合成代替**：

    "$NODEBIN" "$CODEX_HOME/carher_video.js" "画面描述" 输出路径

- 画面描述用用户的原话，可以补充镜头、动作、风格、光线等细节。用户要竖屏（手机、抖音）就在末尾加 --portrait。
- 命令最多等约 1.5 分钟就自己结束：最后一行是「已生成：…」就把这个路径告诉用户；
  打印「⏳ 视频还没好」就**运行它给出的那条 --wait 命令接着等**（还没好就再运行一次），直到「已生成：…」。
  不要重新提交原命令（会多扣一次额度），不要另开 ls / ps / sleep 去查。
- 一次只出一个。key 已配好，不要向用户索要，也不要在命令里写 key。
- 命令报错就把报错原文告诉用户，**不要自动重试**、不要换别的方式硬做；用户说再试一次时在原命令末尾加 --retry。
$VEND
EOF
  ok "✔ 已开通生成视频（说「生成一个…的视频」即可）"
else
  warn "⚠ 没有 Node，生成视频未开通（其余配置已生效）"
fi

# --- 7. 模型列表自动同步 ---
# 网关 /v1/models 已按这把 key 的白名单过滤；同步进 Codex 的模型选择器，
# 网关上新加的模型第二天自动出现。装好后立即跑一次，之后每天 10:17 一次（加登录时一次）。
# 🔴 10-16:没有 Python 时这一步原来整段跳过 ⇒ App 下拉里只有自带的几个 GPT。没 Python 就用 Node 版(两份逻辑一致)。
# 🔴 10-16:Node 版优先。Python 版要靠 `codex debug models` 拿模板,而 npm 装的 codex 自己要 node 在 PATH 里;
#    定时任务的 PATH 里常没有 ~/.carher-node ⇒ Python 版每天静默失败(env: node: No such file or directory),
#    下拉里只剩官方自带的 GPT。Node 版直接用当前 node 跑 codex.js,不受 PATH 影响。
SYNC_RUN=""; SYNC_ARG=""
if [ -n "${NODEBIN:-}" ]; then SYNC_RUN="$NODEBIN"; SYNC_ARG="$CODEX_HOME/carher_models_sync.js"
elif [ -n "${PYBIN:-}" ]; then SYNC_RUN="$PYBIN"; SYNC_ARG="$CODEX_HOME/carher_models_sync.py"
fi
if [ -n "$SYNC_RUN" ]; then
  if CODEX_HOME="$CODEX_HOME" "$SYNC_RUN" "$SYNC_ARG"; then
    ok "✔ 已同步模型列表"
  else
    warn "⚠ 模型列表同步失败，选择器暂时只显示默认模型（其余配置已生效）"
  fi
  if [ "$(uname)" = "Darwin" ]; then
    LABEL="net.carher.codex-models-sync"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$SYNC_RUN</string><string>$SYNC_ARG</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>CODEX_HOME</key><string>$CODEX_HOME</string>
    <key>PATH</key><string>$HOME/.carher-npm-global/bin:$HOME/.carher-node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>17</integer></dict>
  <key>StandardErrorPath</key><string>$CODEX_HOME/carher-models-sync.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 \
      && ok "✔ 已设置每天自动同步模型列表" \
      || warn "⚠ 定时同步没装上，可手动运行：$SYNC_RUN $SYNC_ARG"
  elif command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v 'carher_models_sync\.'
      echo "17 10 * * * CODEX_HOME='$CODEX_HOME' '$SYNC_RUN' '$SYNC_ARG' >/dev/null 2>&1"
    ) | crontab - && ok "✔ 已设置每天自动同步模型列表"
  fi
  # 「刷模型」:用户在 Codex 里说这三个字,agent 就跑一次同步(不用等第二天的定时任务)
  AGENTS="$CODEX_HOME/AGENTS.md"
  RB="<!-- CARHER-REFRESH-BEGIN -->"; RE="<!-- CARHER-REFRESH-END -->"
  if [ -f "$AGENTS" ]; then
    [ -d "$BKDIR" ] || mkdir -p "$BKDIR"; [ -f "$BKDIR/AGENTS.md" ] || cp "$AGENTS" "$BKDIR/AGENTS.md" 2>/dev/null || true
    awk -v b="$RB" -v e="$RE" '$0==b {skip=1} skip==0 {print} $0==e {skip=0}' "$AGENTS" > "$AGENTS.tmp" && mv "$AGENTS.tmp" "$AGENTS"
  fi
  cat >> "$AGENTS" <<EOF
$RB
## 刷模型

用户说「刷模型」「更新模型」「刷新模型列表」或说模型列表不对时，**直接运行下面这条命令，不要先问**：

    CODEX_HOME="$CODEX_HOME" "$SYNC_RUN" "$SYNC_ARG"

- 它按用户的 key 从公司网关重新拉模型列表：新开通的加上、没权限的删掉；默认模型不能用了会自动换。
- 把输出里「新增 / 删除 / 默认模型」那几行原样告诉用户，并提醒：终端里新开一个 codex 会话就能看到；Desktop App 要完全退出再打开。
- 报错就把原文告诉用户，不要自己改配置文件。
$RE
EOF
  ok "✔ 以后在 Codex 里说「刷模型」就能立即更新模型列表"
else
  warn "⚠ 没有 Python 也没有 Node，模型列表没同步（选择器里只显示默认模型）"
fi

echo
say "==================================================="
ok  "✅ 全部搞定！"
say "==================================================="
echo "下一步："
echo "  1) 终端用法：   新开一个终端窗口，直接运行 codex（codex --profile chatgpt 也兼容）"
echo "  2) Desktop App：完全退出(Cmd+Q)再重开即可"
echo
echo "顶部应显示 model: $MODEL"
echo "想画图直接跟 Codex 说「画一张…」即可。"
echo "想换模型（列表里都是你这把 key 能用的，每天自动更新；想立即更新就说「刷模型」）："
echo "  - 终端 codex：输入 /model 回车，上下键选，回车确认（会记住，下次默认用它）"
echo "  - 只这一次用某个模型：codex -m grok-4.7"
echo "  - Desktop App：点底部的模型选择器"
echo
