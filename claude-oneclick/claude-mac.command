#!/usr/bin/env bash
# CarHer Claude Code 一键配置 (macOS / Linux)
# 用法：Finder 里双击本文件，或终端里执行 `bash claude-mac.command`
# 由 build.sh 生成，别直接改；源在 claude-oneclick/src/
#
# 做四件事，用户只需要粘一次 key：
#   1. 没有 Node.js ≥22 就装一个（Claude Code 硬要求）—— 装到 ~/.carher-node，不碰系统、不要 sudo
#   2. 没有 claude / 版本太旧就装最新版（走 npmmirror：官方 downloads.claude.ai 在国内不通）
#   3. 读 key
#   4. 把网关配置**合并**进 ~/.claude/settings.json（保留用户原有设置），删模型列表缓存
set -euo pipefail

BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; NC=$'\033[0m'
say()  { printf '%s%s%s\n' "$BLUE" "$1" "$NC"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$1" "$NC"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$1" "$NC"; }
die()  { printf '%s%s%s\n' "$RED" "$1" "$NC" >&2; exit 1; }

# ── 固定配置（用户无需感知）────────────────────────────────────────────
BASE_URL="https://cc.auto-link.com.cn/pro"
NPM_REGISTRY="https://registry.npmmirror.com"
NODE_MIRROR="https://cdn.npmmirror.com/binaries/node"
NODE_MAJOR_MIN=22
# 2.1.257 之前的版本打 5.x 模型会被上游以 claude_code_version_too_old 拒(400)
CLAUDE_MIN="2.1.257"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
NODE_HOME="$HOME/.carher-node"

echo
say "==================================================="
say "   CarHer Claude Code 一键配置 (macOS / Linux)"
say "==================================================="
echo

# ── 平台 ─────────────────────────────────────────────────────────────
case "$(uname -s)" in
  Darwin) OS=darwin ;;
  Linux)  OS=linux ;;
  *) die "✗ 不支持的系统：$(uname -s)。Windows 请用 claude-windows.bat。" ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  ARCH=x64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) die "✗ 不支持的 CPU 架构：$(uname -m)" ;;
esac
# Rosetta 下跑的 x64 shell 其实是 Apple 芯片 ⇒ 装 arm64 版(否则 Node/claude 全走转译)
if [ "$OS" = darwin ] && [ "$ARCH" = x64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ]; then
  ARCH=arm64
fi

ver_ge() {  # ver_ge A B ⇒ A >= B(纯数字点分)
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" = "$2" ]
}

# ── 1. Node.js ≥22 ───────────────────────────────────────────────────
# 先把我们自己装过的那份放进 PATH,重跑脚本时能认出来
[ -x "$NODE_HOME/bin/node" ] && export PATH="$NODE_HOME/bin:$PATH"

node_major() { node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }

if command -v node >/dev/null 2>&1 && [ "$(node_major)" -ge "$NODE_MAJOR_MIN" ]; then
  ok "✔ Node.js $(node --version)"
else
  if command -v node >/dev/null 2>&1; then
    warn "⚠ 现有 Node.js $(node --version) 太旧(需要 ≥ v$NODE_MAJOR_MIN),装一个新的到 $NODE_HOME(不影响你原来的)"
  else
    say "→ 没有 Node.js,装一个到 $NODE_HOME(不需要管理员密码)"
  fi
  # 10-26:先整份拿下来再挑。原来 `curl | grep | head -n1` 在 set -o pipefail 下,head 拿到第一行就关管道 ⇒
  #   curl 写剩下的内容时 SIGPIPE/exit 23 ⇒ 整条判失败,报「取不到版本列表(网络?)」—— 其实版本号已经拿到了。
  #   mac 上 index.json 下得快、curl 常在 head 关管道前写完所以碰巧不炸;Ubuntu 22.04 实测每次必炸。
  NODE_IDX="$(curl -fsSL --max-time 30 "$NODE_MIRROR/index.json")" \
    || die "✗ 取不到 Node.js 版本列表(网络?):$NODE_MIRROR"
  NODE_VER="$(printf '%s' "$NODE_IDX" \
    | grep -oE '"version":"v'"$NODE_MAJOR_MIN"'\.[0-9]+\.[0-9]+"[^}]*"lts":"[A-Za-z]+"' \
    | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | sed -n 1p)"
  [ -n "$NODE_VER" ] || die "✗ 没找到 Node.js v$NODE_MAJOR_MIN 的 LTS 版本"
  if [ "$OS" = darwin ]; then EXT=tar.gz; else EXT=tar.xz; fi
  PKG="node-$NODE_VER-$OS-$ARCH"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  say "  下载 $PKG ..."
  curl -fSL --max-time 600 --progress-bar -o "$TMP/$PKG.$EXT" "$NODE_MIRROR/$NODE_VER/$PKG.$EXT" \
    || die "✗ 下载 Node.js 失败"
  # 校验:用官方 SHASUMS256.txt 比对,下坏的/被篡改的包不装
  WANT="$(curl -fsSL --max-time 30 "$NODE_MIRROR/$NODE_VER/SHASUMS256.txt" | awk -v f="$PKG.$EXT" '$2==f{print $1}')"
  if command -v shasum >/dev/null 2>&1; then GOT="$(shasum -a 256 "$TMP/$PKG.$EXT" | awk '{print $1}')"
  else GOT="$(sha256sum "$TMP/$PKG.$EXT" | awk '{print $1}')"; fi
  [ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || die "✗ Node.js 安装包校验失败(期望 ${WANT:-?},实际 $GOT),已停止"
  rm -rf "$NODE_HOME"; mkdir -p "$NODE_HOME"
  tar -xf "$TMP/$PKG.$EXT" -C "$NODE_HOME" --strip-components=1 || die "✗ 解压 Node.js 失败"
  export PATH="$NODE_HOME/bin:$PATH"
  [ "$(node_major)" -ge "$NODE_MAJOR_MIN" ] || die "✗ Node.js 装完仍不可用"
  ok "✔ Node.js $(node --version) 已装到 $NODE_HOME"
fi

# npm 全局装到用户目录(不要 sudo、不写 /usr/local)
NPM_PREFIX="$HOME/.carher-npm-global"
mkdir -p "$NPM_PREFIX"
export npm_config_prefix="$NPM_PREFIX"
export PATH="$NPM_PREFIX/bin:$PATH"

# ── 2. Claude Code ───────────────────────────────────────────────────
cur_claude_ver() { claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1; }
CUR="$(cur_claude_ver || true)"
if [ -n "$CUR" ] && ver_ge "$CUR" "$CLAUDE_MIN"; then
  ok "✔ Claude Code $CUR"
else
  if [ -n "$CUR" ]; then warn "⚠ Claude Code $CUR 太旧(需要 ≥ $CLAUDE_MIN),升级中 ..."
  else say "→ 安装 Claude Code(约 1~3 分钟)..."; fi
  npm install -g @anthropic-ai/claude-code --registry="$NPM_REGISTRY" --no-fund --no-audit --loglevel=error \
    || die "✗ 安装 Claude Code 失败(把上面的报错截图发给运维)"
  hash -r 2>/dev/null || true
  CUR="$(cur_claude_ver || true)"
  [ -n "$CUR" ] || die "✗ 装完了但找不到 claude 命令"
  ok "✔ Claude Code $CUR"
fi
CLAUDE_BIN="$(command -v claude)"

# ── 让以后新开的终端也能直接敲 claude ───────────────────────────────
# 只追加一个带标记的块,重跑先剥旧块,不碰用户别的内容
add_path_block() {
  local rc="$1"; [ -e "$rc" ] || touch "$rc"
  local tmp; tmp="$(mktemp)"
  awk '/# >>> carher-claude >>>/{s=1;next} /# <<< carher-claude <<</{s=0;next} !s' "$rc" > "$tmp"
  {
    cat "$tmp"
    echo '# >>> carher-claude >>>'
    [ -x "$NODE_HOME/bin/node" ] && echo "export PATH=\"$NODE_HOME/bin:\$PATH\""
    echo "export PATH=\"$NPM_PREFIX/bin:\$PATH\""
    echo '# <<< carher-claude <<<'
  } > "$rc"
  rm -f "$tmp"
}
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
  # .bash_profile 只在它已存在时改(macOS 上新建它会让 .bashrc 不再被读)
  [ "$rc" = "$HOME/.bash_profile" ] && [ ! -e "$rc" ] && continue
  add_path_block "$rc"
done
echo

# ── 3. 读 key ────────────────────────────────────────────────────────
say "粘贴你的 key(sk- 开头,和 Cursor / Codex 用的是同一把),然后回车:"
printf "key: "
read -r APIKEY || true
# 剥掉所有空白 + 零宽字符(从飞书表格复制最容易带进来,带进去请求根本发不出去)。
# 🔴 用 node 做,不用 sed:macOS 的 BSD sed 不认 `[\x8b-\x8f]` 这种字节区间,直接报
#    `RE error: invalid character range` 并输出**空** ⇒ 连干净的 key 也被吃成空字符串。
#    这是台架当场抓到的(第一版就是 sed),上面已保证有 node,用它最稳。key 走环境变量进去。
APIKEY="$(CARHER_RAW="${APIKEY:-}" node -e 'process.stdout.write(process.env.CARHER_RAW.replace(/[\s\u00A0\u200B-\u200F\u2028-\u202F\uFEFF]/g,""))')"
[ -n "$APIKEY" ] || die "✗ 没有输入 key,已取消。"
case "$APIKEY" in sk-*) : ;; *) warn "⚠ key 不是 sk- 开头,仍然继续。" ;; esac
echo

# 先验 key 能用,再写配置(写了个坏 key 进去用户只会看到 401 不知道为什么)
say "→ 检查 key ..."
CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -H "x-api-key: $APIKEY" "$BASE_URL/v1/models" || echo 000)"
case "$CODE" in
  200) ok "✔ key 可用" ;;
  401|403) die "✗ key 无效(HTTP $CODE)。去 key 表里重新「全选复制」那一格再试。" ;;
  *) warn "⚠ 暂时连不上网关(HTTP $CODE),配置照样写好,网络恢复后就能用。" ;;
esac
echo

# ── 4. 写配置:合并进 settings.json,不覆盖用户原有设置 ──────────────
mkdir -p "$CLAUDE_HOME"
SETTINGS="$CLAUDE_HOME/settings.json"
if [ -f "$SETTINGS" ]; then
  BK="$SETTINGS.bak-carher-$(date +%Y%m%d-%H%M%S)"
  cp "$SETTINGS" "$BK"; warn "↳ 已备份原有配置到:$BK"
fi
# 用 Node 合并 JSON(上面已保证有 Node,不依赖 python/jq)。key 走环境变量,不进命令行参数。
# 网关模型清单(给 modelPicker 用);拿不到就跳过这一项,不影响别的。
# 「刷模型」用的是同一份逻辑(carher_models_refresh.js),这里先落盘,合并时直接 require 它。
mkdir -p "$CLAUDE_HOME/carher"
cat > "$CLAUDE_HOME/carher/carher_models_refresh.js" <<'CARHER_JS_EOF'
#!/usr/bin/env node
/* 「刷模型」—— 按这把 key 重写 Claude Code 的模型菜单(2026-10-19)。

   Claude Code 自己会做网关模型发现(CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1),但非 claude 的
   网关模型要靠 settings.json 里的 modelPicker(behavesAs)才不报「isn't recognized」。那张表以前只在
   安装时写一次 ⇒ 网关删掉的模型一直留在 /model 里(点了 403),新开通的进不来。
   本脚本:读 settings.json 里的 key/地址 → GET /v1/models → 重写 modelPicker(旧的删、新的加)
   → 删模型缓存 → 当前 model 已没权限就清掉(回到默认)→ haiku 默认指到 key 里真有的那个。安装器第 4 步和「刷模型」共用这一份。

   只用 Node 自带模块(Node ≥ 18)。用法:
     node carher_models_refresh.js                 # 拉网关并写入
     CARHER_MODELS="a\nb" node carher_models_refresh.js   # 不出网,用给定列表(安装器/测试用)
   拉不到列表就不动 settings.json(绝不拿空列表把菜单清空)。改前备份到 settings.json.bak-carher-refresh。 */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");

const CLAUDE_HOME = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
const SETTINGS = process.env.CARHER_SETTINGS || path.join(CLAUDE_HOME, "settings.json");
const BEHAVES_AS = "claude-sonnet-5-5";
const CLAUDE_FAMILY = ["fable", "opus", "sonnet", "haiku"];
const familyRank = (i) => { const k = CLAUDE_FAMILY.findIndex((f) => i.startsWith(`claude-${f}-`)); return k < 0 ? CLAUDE_FAMILY.length : k; };

/* /model 菜单 = 这把 key 能聊天的全部模型,每个只出现一次(10-25)。
   以前只写非 claude 的、再叠上 Claude Code 自带 + 网关发现 ⇒ 同一个模型出现两次、出图模型和 key 没有的 Fable 也在菜单里。
   现在 replaceBuiltInOptions:true ⇒ 菜单只剩「Default + 这里的行」(官方 settings-reference#modelpicker)。
   claude-*:不写 label,Claude Code 自己显示成 Opus 5.5 这类名字;同家族按版本新→旧。
   非 claude:behavesAs 指一个它认识的模型 ⇒ 不再告警「isn't recognized」。出图/视频/音乐、审查专用不进。 */
function pickerRows(ids) {
  const seen = new Set();
  const chat = ids.filter((i) => typeof i === "string" && i && !seen.has(i) && seen.add(i))
    .filter((i) => !/image|imagine|video|music/.test(i) && i !== "codex-auto-review");
  const claude = chat.filter((i) => /^claude-/.test(i))
    .sort((a, b) => familyRank(a) - familyRank(b) || b.localeCompare(a, "en", { numeric: true }));
  return [
    ...claude.map((i) => ({ model: i })),
    ...chat.filter((i) => !/^claude-/.test(i)).map((i) => ({ model: i, label: i, behavesAs: BEHAVES_AS })),
  ];
}

/* 把网关列表并进 settings 对象。返回 {d, added, removed, modelReset}。纯函数,测试直接调。 */
function applyModels(d, ids) {
  const before = ((d.modelPicker && d.modelPicker.options) || []).map((o) => o && o.model).filter(Boolean);
  const rows = pickerRows(ids);
  const after = rows.map((r) => r.model);
  if (rows.length) d.modelPicker = { ...(d.modelPicker || {}), options: rows, replaceBuiltInOptions: true };
  else delete d.modelPicker;
  // 当前钉着的 model 已不在网关里 ⇒ 每次新开窗口都报错。清掉 = 回到 Claude Code 默认模型。
  // 只认「网关名」:上次菜单里的,或 claude-* 全名(`[1m]` 后缀先剥)。opus / sonnet 这类别名不动。
  let modelReset = null;
  if (typeof d.model === "string" && d.model) {
    const bare = d.model.replace(/\[[^\]]*\]$/, "");
    if ((before.includes(bare) || /^claude-/.test(bare)) && !ids.includes(bare)) { modelReset = d.model; delete d.model; }
  }
  /* 10-19 sunqiang/linsen:Claude Code 2.1.30x 的 haiku 默认是 claude-haiku-5-5(后台起标题、子任务用),
     网关没有这个模型 ⇒ 后台请求一直 403。⇒ 指到这把 key 里真有的 haiku(按版本新→旧挑);
     一个都没有就删掉这项(让 Claude Code 自己决定)。用户自己设的、且仍可用的不动。
     10-25 网关上了 claude-haiku-5-5:之前写进去的老 haiku(4-5)仍可用但不是最新 ⇒ 也升到最新;
     用户指到非 haiku 的(比如 sonnet)仍不动。 */
  d.env = Object.assign({}, d.env || {});
  // 10-25:每次开会话的自动刷新顺带补上这项 ⇒ 装过带 hook 版本的同事以后不用重跑安装也能拿到(用户自己写过不动)
  if (!d.env.ENABLE_TOOL_SEARCH) d.env.ENABLE_TOOL_SEARCH = "true";
  const haikus = ids.filter((i) => /^claude-haiku-\d/.test(i))
    .sort((a, b) => b.localeCompare(a, "en", { numeric: true }));
  const curH = d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  let haikuSet = null;
  const staleHaiku = curH && /^claude-haiku-\d/.test(curH) && haikus.length && curH !== haikus[0];
  if (!(curH && ids.includes(curH)) || staleHaiku) {
    if (haikus.length) { d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL = haikus[0]; haikuSet = haikus[0]; }
    else delete d.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  }
  return {
    d,
    haikuSet,
    added: after.filter((m) => !before.includes(m)),
    removed: before.filter((m) => !after.includes(m)),
    modelReset,
  };
}

/* 桌面 App(Code 标签页)的模型菜单(10-27 用户:「刷模型在 App 里也搞定啊」)。
   App 不读 settings.json 的 modelPicker,它自己在**启动时**去网关拉一次 /v1/models,结果只存在内存里。
   App 配置项 `inferenceModels` 一旦写了,App 就不再自己拉、菜单 = 这份列表(本机实测日志:
   「Model discovery: skipped (inferenceModels obviates discovery); picker = 2 (inferenceModels)」)。
   ⇒ 刷模型把 key 里的 claude-* 写进去(App 只认 Claude 模型,非 claude 写进去会被它拒),菜单就和 key 完全一致;
   App 要**完全退出再打开**才读新配置。只改我们自己那条(_meta.json 里叫 CarHer 的),别的配置不碰。 */
function appConfigEntries() {
  const roots = [];
  if (process.platform === "darwin") roots.push(path.join(os.homedir(), "Library", "Application Support", "Claude-3p"));
  else if (process.platform === "win32") {
    if (process.env.LOCALAPPDATA) roots.push(path.join(process.env.LOCALAPPDATA, "Claude-3p"));
    if (process.env.APPDATA) roots.push(path.join(process.env.APPDATA, "Claude-3p"));
    // MSIX 版数据会被重定向到 Packages\Claude_*\LocalCache\Local\Claude-3p
    try {
      const pk = path.join(process.env.LOCALAPPDATA || "", "Packages");
      for (const d of fs.readdirSync(pk)) if (/^Claude_/i.test(d)) roots.push(path.join(pk, d, "LocalCache", "Local", "Claude-3p"));
    } catch (e) { /* 没有就算了 */ }
  } else roots.push(path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "Claude-3p"));
  const out = [];
  for (const r of roots) {
    const lib = path.join(r, "configLibrary");
    let meta; try { meta = JSON.parse(fs.readFileSync(path.join(lib, "_meta.json"), "utf8")); } catch (e) { continue; }
    for (const e of (meta && meta.entries) || []) {
      if (e && e.name === "CarHer" && /^[a-f0-9-]{36}$/.test(e.id || "")) out.push(path.join(lib, e.id + ".json"));
    }
  }
  return out;
}
function syncAppModels(ids, key) {
  const want = ids.filter((i) => /^claude-/.test(i) && !/image|imagine|video|music/.test(i))
    .sort((a, b) => familyRank(a) - familyRank(b) || b.localeCompare(a, "en", { numeric: true }));
  const res = [];
  for (const p of appConfigEntries()) {
    let c; try { c = JSON.parse(fs.readFileSync(p, "utf8")); } catch (e) { continue; }
    if (key && c.inferenceGatewayApiKey && c.inferenceGatewayApiKey !== key) continue;   // 不是这把 key 配的那条,不动
    const before = Array.isArray(c.inferenceModels) ? c.inferenceModels.map((m) => (typeof m === "string" ? m : m && m.name)).filter(Boolean) : null;
    if (!want.length) {
      // key 里一个 Claude 模型都没有 ⇒ 别写空列表(App 会没有可选的),交回 App 自己拉
      if (before) { delete c.inferenceModels; } else continue;
    } else {
      if (before && before.join("\n") === want.join("\n")) { res.push({ p, changed: false, models: want }); continue; }
      c.inferenceModels = want;
    }
    const tmp = p + ".carher-tmp-" + process.pid;
    fs.writeFileSync(tmp, JSON.stringify(c, null, 2) + "\n", { mode: 0o600 });
    fs.renameSync(tmp, p);
    res.push({ p, changed: true, models: want, added: want.filter((m) => !(before || []).includes(m)), removed: (before || []).filter((m) => !want.includes(m)) });
  }
  return res;
}

/* 把这把 key 记成「已同意」(10-27 王德升:命令行右下角一直「Not logged in · Run /login」)。
   Claude Code 第一次看到 settings 里的 ANTHROPIC_API_KEY 会问「Do you want to use this API key?」,默认选中 **No**;
   回车一下就记进全局配置 customApiKeyResponses.rejected,之后**每次启动都不用这把 key** ⇒ 显示未登录(请求照样走,
   因为 App / -p 另有来源,但交互界面认为没登录)。本机隔离复现:rejected ⇒ Not logged in;approved ⇒ 正常。
   比较的是 key 末尾 20 个字符(Claude Code 自己的 Vpe())。用户换 key 后旧条目留着无害。
   配置文件:CLAUDE_CONFIG_DIR/.claude.json,没设时是 ~/.claude.json(和 settings.json 不在一个目录)。 */
function approveApiKey(key, configDir) {
  const tail = String(key || "").trim().slice(-20);
  if (!tail) return false;
  const p = configDir ? path.join(configDir, ".claude.json") : path.join(os.homedir(), ".claude.json");
  let g = {};
  try { g = JSON.parse(fs.readFileSync(p, "utf8").replace(/^\uFEFF/, "")) || {}; } catch (e) { if (fs.existsSync(p)) return false; }
  const r = (g.customApiKeyResponses && typeof g.customApiKeyResponses === "object") ? g.customApiKeyResponses : {};
  const approved = Array.isArray(r.approved) ? r.approved.filter((x) => typeof x === "string") : [];
  const rejected = Array.isArray(r.rejected) ? r.rejected.filter((x) => typeof x === "string") : [];
  if (approved.includes(tail) && !rejected.includes(tail)) return false;
  g.customApiKeyResponses = { ...r, approved: approved.includes(tail) ? approved : [...approved, tail], rejected: rejected.filter((x) => x !== tail) };
  const tmp = p + ".carher-tmp-" + process.pid;
  fs.writeFileSync(tmp, JSON.stringify(g, null, 2) + "\n", { mode: 0o600 });
  fs.renameSync(tmp, p);
  return true;
}

/* 在 settings.hooks.SessionStart 里挂(或更新)我们那一条自动刷新 hook。重跑幂等:先摘掉旧的
   (认 command 里的 carher_models_refresh.js),用户自己的 hook 一条不动。
   exec 形式(args)不经 shell ⇒ 路径带空格/中文、Windows 没 Git Bash 都不怕(hook args 需 2.1.139+)。 */
function hookEntry(d, nodeAbs, scriptAbs) {
  const ours = (h) => h && JSON.stringify(h).includes("carher_models_refresh.js");
  d.hooks = d.hooks && typeof d.hooks === "object" && !Array.isArray(d.hooks) ? d.hooks : {};
  const list = (Array.isArray(d.hooks.SessionStart) ? d.hooks.SessionStart : [])
    .map((g) => (g && Array.isArray(g.hooks) ? { ...g, hooks: g.hooks.filter((h) => !ours(h)) } : g))
    .filter((g) => !(g && Array.isArray(g.hooks) && g.hooks.length === 0));
  list.push({ matcher: "startup", hooks: [{ type: "command", command: nodeAbs, args: [scriptAbs, "--quiet"], async: true, timeout: 60 }] });
  d.hooks.SessionStart = list;
  return d;
}

async function fetchIds(base, key) {
  if (process.env.CARHER_MODELS != null) return process.env.CARHER_MODELS.split("\n").map((s) => s.trim()).filter(Boolean);
  const r = await fetch(String(base).replace(/\/+$/, "") + "/v1/models", {
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01" }, signal: AbortSignal.timeout(30000),
  });
  if (r.status === 401 || r.status === 403) throw new Error(`key 无效或已停用(HTTP ${r.status})`);
  if (!r.ok) throw new Error(`网关返回 HTTP ${r.status}`);
  return ((await r.json()).data || []).map((m) => m && m.id).filter(Boolean);
}

/* 每次开 Claude Code 自动刷一次(10-25):安装器在 settings.json 里挂一个 SessionStart 异步 hook 跑
   `node carher_models_refresh.js --quiet`。后台跑、不挡输入;--quiet 不往屏幕/对话里打任何东西,
   拉不到就原样退出 0(断网不报错),结果只记到 ~/.claude/carher/refresh.log。
   ⇒ 管理员给 key 开/关模型后,用户下次开 Claude Code 菜单就对了,不用记得说「刷模型」。 */
const QUIET = process.argv.includes("--quiet");
const LOG = path.join(CLAUDE_HOME, "carher", "refresh.log");
function logLine(s) {
  if (!QUIET) return;
  try {
    fs.mkdirSync(path.dirname(LOG), { recursive: true });
    let old = ""; try { old = fs.readFileSync(LOG, "utf8"); } catch (e) { /* 第一次 */ }
    const lines = (old + new Date().toISOString() + " " + s + "\n").split("\n").slice(-200);
    fs.writeFileSync(LOG, lines.join("\n"), { mode: 0o600 });
  } catch (e) { /* 记不了日志也不影响 */ }
}
const say = (s) => { if (!QUIET) console.log(s); };

async function main() {
  let raw;
  try { raw = fs.readFileSync(SETTINGS, "utf8").replace(/^\uFEFF/, ""); }
  catch (e) { throw new Error(`找不到 ${SETTINGS},先跑一键配置脚本`); }
  const d = raw.trim() ? JSON.parse(raw) : {};
  const env = d.env || {};
  const key = process.env.CARHER_KEY || env.ANTHROPIC_API_KEY, base = process.env.CARHER_BASE || env.ANTHROPIC_BASE_URL;
  if (!key || !base) throw new Error("settings.json 里没有 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,先跑一键配置脚本");
  let ids;
  try { ids = await fetchIds(base, key); }
  catch (e) { throw new Error(`没拉到模型列表(${e.message}),菜单没动`); }
  if (!ids.length) throw new Error("网关没返回任何模型,菜单没动");
  const r = applyModels(d, ids);
  const out = JSON.stringify(r.d, null, 2) + "\n";
  if (out !== raw) {
    if (raw.trim()) fs.writeFileSync(SETTINGS + ".bak-carher-refresh", raw, { mode: 0o600 });
    // 先写临时文件再改名:Claude Code 在监视这个文件,写到一半被它读到会当成坏 JSON
    const tmp = SETTINGS + ".carher-tmp-" + process.pid;
    fs.writeFileSync(tmp, out, { mode: 0o600 });
    fs.renameSync(tmp, SETTINGS);
    try { fs.chmodSync(SETTINGS, 0o600); } catch (e) { /* Windows */ }
  }
  try { fs.unlinkSync(path.join(CLAUDE_HOME, "cache", "gateway-models.json")); } catch (e) { /* 没有就算了 */ }
  let app = [];
  try { app = syncAppModels(ids, key); } catch (e) { logLine("app sync failed " + e.message); }
  for (const a of app) if (a.changed) logLine("app menu " + a.models.join(","));
  let keyFixed = false;
  try { keyFixed = approveApiKey(key, process.env.CLAUDE_CONFIG_DIR); } catch (e) { /* 写不了就算了,不影响菜单 */ }
  if (keyFixed) logLine("approved api key (was rejected/unanswered)");
  const n = r.d.modelPicker ? r.d.modelPicker.options.length : 0;
  logLine(`ok menu=${n} added=${r.added.join(",") || "-"} removed=${r.removed.join(",") || "-"}` +
    (r.modelReset ? ` reset=${r.modelReset}` : "") + (r.haikuSet ? ` haiku=${r.haikuSet}` : ""));
  say(`/model 菜单现在一共 ${n} 个模型,就是你的 key 能用来聊天的全部(画图/视频类不放进菜单)`);
  if (r.added.length) say(`新增 ${r.added.length}:${r.added.join(", ")}`);
  if (r.removed.length) say(`删除 ${r.removed.length}:${r.removed.join(", ")}`);
  if (!r.added.length && !r.removed.length) say("菜单和上次一样,没有变化");
  if (r.modelReset) say(`你之前选的 ${r.modelReset} 已经没有权限,已改回默认模型`);
  if (r.haikuSet) say(`后台小任务用的模型设为 ${r.haikuSet}(你的 key 里最新的 haiku)`);
  for (const a of app) {
    if (!a.changed) { say(`桌面 App 菜单和上次一样(${a.models.length} 个 Claude 模型)`); continue; }
    say(`桌面 App 菜单已更新为 ${a.models.length} 个 Claude 模型:${a.models.join(", ")}`);
    if (a.added && a.added.length) say(`  App 新增:${a.added.join(", ")}`);
    if (a.removed && a.removed.length) say(`  App 删除:${a.removed.join(", ")}`);
    say("  ⚠️ 桌面 App 要**完全退出再打开**才会显示新菜单(Mac:Cmd+Q;Windows:右下角托盘图标右键退出)");
  }
  if (keyFixed) say("已修好「Not logged in」:这把 key 之前被拒绝过,现在记成已同意");
  say("新开一个 Claude Code 会话(或桌面 App 新开对话)后 /model 里就是新列表");
}

module.exports = { pickerRows, applyModels, hookEntry, approveApiKey, syncAppModels, appConfigEntries };

if (require.main === module) {
  main().catch((e) => {
    if (QUIET) { logLine("skip " + e.message); process.exit(0); }   // 后台自动刷:失败不打扰用户
    console.error("✗ " + e.message); process.exit(1);
  });
}
CARHER_JS_EOF
CARHER_MODELS="$(curl -fsS --max-time 30 -H "x-api-key: $APIKEY" -H "anthropic-version: 2023-06-01" "$BASE_URL/v1/models" 2>/dev/null \
  | node -e "let b=\"\";process.stdin.on(\"data\",c=>b+=c).on(\"end\",()=>{try{console.log(JSON.parse(b).data.map(m=>m.id).join(\"\\n\"))}catch(e){}})" 2>/dev/null || true)"
CARHER_REFRESH="$CLAUDE_HOME/carher/carher_models_refresh.js" CARHER_MODELS="$CARHER_MODELS" CARHER_KEY="$APIKEY" CARHER_BASE="$BASE_URL" CARHER_SETTINGS="$SETTINGS" node -e '
const fs = require("fs");
const p = process.env.CARHER_SETTINGS;
let d = {};
if (fs.existsSync(p)) {
  const raw = fs.readFileSync(p, "utf8").replace(/^\uFEFF/, "");
  if (raw.trim()) {
    try { d = JSON.parse(raw); }
    catch (e) { console.error("✗ 原有 settings.json 不是合法 JSON,已备份,不动它:" + e.message); process.exit(3); }
  }
}
if (typeof d !== "object" || d === null || Array.isArray(d)) d = {};
d.env = Object.assign({}, d.env || {});
// AUTH_TOKEN 能聊天但拉不到网关模型列表;两者同时在时还会抢优先级 ⇒ 只留 API_KEY
delete d.env.ANTHROPIC_AUTH_TOKEN;
// 这个开关会把模型发现一起关掉 ⇒ /model 里只剩几个 Claude
delete d.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC;
d.env.ANTHROPIC_BASE_URL = process.env.CARHER_BASE;
d.env.ANTHROPIC_API_KEY = process.env.CARHER_KEY;
d.env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1";
// 10-25:MCP 工具按需加载(tool search)。Claude Code 2.1.304 起走网关才默认开,装机下限 2.1.257 ⇒ 老版本同事
// 每句话都把所有 MCP 工具说明整份发出去(本机 rea 210 个工具约 1MB,叠上聊天记录超 100 万上限卡 25 分钟)。
// 用户自己写过(包括 false)就不动。
if (!d.env.ENABLE_TOOL_SEARCH) d.env.ENABLE_TOOL_SEARCH = "true";
// 2026-10-14:旧版 Claude Code 把非 claude 的网关模型显示成 claude-router-<hex>,选了会存进 model;
// 新版(2.1.29x 起)直接认真名,旧名每次新开窗口都报 Model isn t recognized ⇒ 还原成真名。
const real = (n) => { const m = /^claude-router-([0-9a-f]+)$/.exec(n || ""); return m ? Buffer.from(m[1], "hex").toString("utf8") : n; };
if (typeof d.model === "string") d.model = real(d.model);
// 网关里非 claude 的模型(GPT/Grok/Kimi 等)Claude Code 不认识 ⇒ 每次新开窗口都打警告、上下文按 200k 算。
// modelPicker 一行一个,behavesAs 指一个它认识的模型 ⇒ 不再警告,菜单里直接是真名。
// 旧的删、新的加、选中的已没权限就回默认 —— 与「刷模型」同一份逻辑(carher_models_refresh.js)。
const ids = (process.env.CARHER_MODELS || "").split("\n").filter(Boolean);
if (ids.length) require(process.env.CARHER_REFRESH).applyModels(d, ids);
// 10-27:不预先同意 ⇒ 首次启动问「用不用这把 key」默认 No,回车就被记成拒绝 ⇒ 永远「Not logged in」
try { require(process.env.CARHER_REFRESH).approveApiKey(process.env.CARHER_KEY, process.env.CLAUDE_CONFIG_DIR); } catch (e) {}
fs.writeFileSync(p, JSON.stringify(d, null, 2) + "\n", { mode: 0o600 });
fs.chmodSync(p, 0o600);
' || die "✗ 写配置失败"
ok "✔ 配置已写入 $SETTINGS"

# 模型列表缓存不会自己过期 ⇒ 删掉,下次启动重新拉
rm -f "$CLAUDE_HOME/cache/gateway-models.json"

# ── 4b. Claude 桌面 App(Code 标签页)────────────────────────────────
# 🔴 桌面 App **不读** ~/.claude/settings.json 里的 ANTHROPIC_BASE_URL 来切网关 —— 它有自己的
#    「三方模式」(3P),配置放在一个**单独的** userData 目录 `<userData>-3p/configLibrary/` 里
#    (从 App 自身代码读出来的:fwA()=userData+"-3p",hwA()=…/configLibrary,_meta.json 记 appliedId)。
#    用独立目录的好处:用户原来登录的 Claude App(聊天/Cowork)的数据**一个字节都不碰**。
#    键名与取值来自 App 的 schema:inferenceProvider=gateway + GatewayBaseUrl + GatewayApiKey,
#    认证头用 x-api-key(网关两种都认,x-api-key 与 CLI 一致),凭据类型 static。
#    实测(1.15962.0):日志 `[custom-3p] 3P mode active { provider: 'gateway' }`,
#    `Model discovery: 9 found`,真请求在 SpendLogs 落 kiro-claude-haiku-4-5 两发全 ok。
if [ "$OS" = darwin ]; then APP_UD="$HOME/Library/Application Support/Claude-3p"
else APP_UD="${XDG_CONFIG_HOME:-$HOME/.config}/Claude-3p"; fi
CARHER_KEY="$APIKEY" CARHER_BASE="$BASE_URL" CARHER_LIB="$APP_UD/configLibrary" node -e '
const fs = require("fs"), path = require("path"), crypto = require("crypto");
const lib = process.env.CARHER_LIB;
fs.mkdirSync(lib, { recursive: true, mode: 0o700 });
const metaP = path.join(lib, "_meta.json");
let meta = { appliedId: "", entries: [] };
try { const m = JSON.parse(fs.readFileSync(metaP, "utf8")); if (m && Array.isArray(m.entries)) meta = m; } catch {}
// 有我们之前建的那条就复用(重跑幂等);没有就新建一条叫 CarHer,并设为当前生效
let ent = meta.entries.find(e => e && e.name === "CarHer" && /^[a-f0-9-]{36}$/.test(e.id || ""));
if (!ent) { ent = { id: crypto.randomUUID(), name: "CarHer" }; meta.entries.push(ent); }
meta.appliedId = ent.id;
const cfgP = path.join(lib, ent.id + ".json");
let cfg = {};
try { cfg = JSON.parse(fs.readFileSync(cfgP, "utf8")) || {}; } catch {}
Object.assign(cfg, {
  inferenceProvider: "gateway",
  inferenceGatewayBaseUrl: process.env.CARHER_BASE,
  inferenceGatewayApiKey: process.env.CARHER_KEY,
  inferenceGatewayAuthScheme: "x-api-key",
  inferenceCredentialKind: "static",
});
// 10-25 同事反馈:App 的 Code/Cowork 里 npm install 失败,设置页显示「严格沙箱 · 组织开启」,
//    bypass 也没用,终端里正常。真因:这项不写 ⇒ App 按 failClosed 当成 [] ⇒ 给会话下发
//    sandbox{enabled, allowUnsandboxedCommands:false, network.allowedDomains:[], allowManagedDomainsOnly}
//    ⇒ 命令一个外网都连不上(npm E403 "user denied")。官方文档:含 "*" 且没设 allowedWorkspaceFolders
//    时两种沙箱都不开(和终端里的 claude 一样)。只列 npm 源不够:沙箱还挡 ~/.npm 缓存写入(实测 EPERM)。
//    用户/管理员自己在这条里写过非空列表 ⇒ 尊重,不覆盖。
if (!Array.isArray(cfg.coworkEgressAllowedHosts) || !cfg.coworkEgressAllowedHosts.length) cfg.coworkEgressAllowedHosts = ["*"];
fs.writeFileSync(cfgP, JSON.stringify(cfg, null, 2) + "\n", { mode: 0o600 }); fs.chmodSync(cfgP, 0o600);
fs.writeFileSync(metaP, JSON.stringify(meta, null, 2) + "\n", { mode: 0o600 });
' && ok "✔ Claude 桌面 App 已配好(Code 标签页走同一个网关)" \
  || warn "⚠ 桌面 App 配置没写成(不影响命令行 claude)"

# ── 4c. 桌面 App 要的 Claude Code 二进制:预先放好 ────────────────────
# 🔴 10-13 本机实测的「App 发消息不回复」真因:App 每开一个会话都要先从
#    downloads.claude.ai 拉一份**它自己钉死版本**的 Claude Code(本机 Mac 和 Linux VM 各一份),
#    国内直连那个域名 ERR_CONNECTION_TIMED_OUT ⇒ 日志
#    `Host Claude Code binary not available` ⇒ 会话永远起不来,界面上就是"不回复"。
#    App 只认自己的 storageDir + `.verified`(里面写 manifest 的 sha256),不认 npm 装的那个
#    (npm 版是 plain 二进制,App 在 mac 上要 claude.app bundle;实测两者 sha 不同)。
#    ⇒ 从我们自己的镜像 cc.auto-link.com.cn/ccdl/ 取**官方原样**文件,按 App 的目录形状放好,
#    并用 App 内嵌 manifest 的 sha256 校验。版本号从 App 里读(App 升级后自动跟)。
# 10-28 起 Linux 也有官方 App(deb,/usr/lib/claude-desktop),同样每开会话先去国外下组件 ⇒ 一起预置。
if [ "$OS" = darwin ]; then APP_ASAR="/Applications/Claude.app/Contents/Resources/app.asar"
else APP_ASAR="/usr/lib/claude-desktop/resources/app.asar"; fi
CCDL="https://cc.auto-link.com.cn/ccdl/claude-code-releases"
if [ -f "$APP_ASAR" ]; then
  say "→ 准备桌面 App 需要的 Claude Code 组件(约 100MB,首次会慢一点)..."
  CARHER_ASAR="$APP_ASAR" CARHER_UD="$APP_UD" CARHER_CCDL="$CCDL" CARHER_ARCH="$ARCH" CARHER_OS="$OS" node -e '
const fs=require("fs"),path=require("path"),crypto=require("crypto"),zlib=require("zlib"),cp=require("child_process"),https=require("https");
const b=fs.readFileSync(process.env.CARHER_ASAR).toString("latin1");
// App 内嵌 manifest:JSON.parse(\x27{"version":"2.1.x","manifest":{...},"baseUrl":...}\x27)
const m=b.match(/JSON\.parse\(\x27(\{"version":"(\d+\.\d+\.\d+)","manifest".*?)\x27\)/);
if(!m){console.error("读不到 App 内嵌的 Claude Code 版本(App 版本太新/太旧?)");process.exit(2);}
const d=JSON.parse(m[1]), V=d.manifest.version, P=d.manifest.platforms;
const mac  = process.env.CARHER_OS==="darwin";
const host = (mac?"darwin-":"linux-")+(process.env.CARHER_ARCH==="arm64"?"arm64":"x64");
const vm   = "linux-"+(process.env.CARHER_ARCH==="arm64"?"arm64":"x64");
const get1=(u)=>new Promise((res,rej)=>{https.get(u,{headers:{"User-Agent":"carher-oneclick"}},r=>{
  if(r.statusCode!==200){rej(new Error(u+" HTTP "+r.statusCode));r.resume();return;}
  const bufs=[];r.on("data",x=>bufs.push(x));r.on("error",rej);r.on("aborted",()=>rej(new Error("下载中断")));
  r.on("end",()=>res(Buffer.concat(bufs)));}).on("error",rej);});
// 10-28 隔离实测:100MB 下载偶发 socket hang up(连跑 4 次都好)⇒ 断了自动重试 3 次再报
const get=async(u)=>{for(let i=1;;i++){try{return await get1(u);}catch(e){if(i>=3||/HTTP 4/.test(e.message))throw e;await new Promise(r=>setTimeout(r,2000*i));}}};
const sha=(x)=>crypto.createHash("sha256").update(x).digest("hex");
const ok=(dir,want)=>{try{return fs.readFileSync(path.join(dir,".verified"),"utf8").trim()===want;}catch{return false;}};
// 10-28 复核 App 4.29(本机 3.1623.4 已是):本机组件目录改成 claude-code/<ver>/<sha 前 12 位>/
//   (App 代码:installDirFor ⇒ join(storageDir,ver,checksum.slice(0,12)));老的 claude-code/<ver>/ 只当
//   legacy,App 启动时搬进新目录。只写老目录 ⇒ App 搬走后下次跑脚本又判「没有」再下 100MB,doctor 也误报。
//   ⇒ App 是新布局就直接写新目录;老 App(没有这段代码)仍写老目录。VM 那份目录没变。
const newLayout=b.includes("it cannot name an install folder");
const hostDir=(ck)=>path.join(process.env.CARHER_UD,"claude-code",V,...(newLayout?[ck.slice(0,12).toLowerCase()]:[]));
(async()=>{
  // ① 本机这份(mac 上是 claude.app bundle;Linux 上是单个 claude 二进制,App 代码 useBundle 只认 darwin)
  if(!mac){
    const e=P[host]; if(!e) throw new Error("manifest 里没有 "+host);
    const ld=hostDir(e.checksum), exe=path.join(ld,"claude");
    if(!(ok(ld,e.checksum) && fs.existsSync(exe))){
      const z=await get(`${process.env.CARHER_CCDL}/${V}/${host}/claude.zst`);
      if(sha(z)!==e.checksum) throw new Error("本机组件 sha256 不对,拒绝安装");
      if(typeof zlib.zstdDecompressSync!=="function") throw new Error("Node 不支持 zstd(需要 Node>=22.15)");
      fs.mkdirSync(ld,{recursive:true}); fs.writeFileSync(exe,zlib.zstdDecompressSync(z)); fs.chmodSync(exe,0o755);
      fs.writeFileSync(path.join(ld,".verified"),e.checksum);
    }
    console.log(V); return;   // Linux 只预置本机这份;App 的隔离工作区(VM)在 Linux 上是否启用没实测过,先不下那 100MB
  }
  const hb=P[host]&&P[host].bundle; const hd=hb?hostDir(hb.checksum):"";
  if(hb && !(ok(hd,hb.checksum) && fs.existsSync(path.join(hd,"claude.app/Contents/MacOS/claude")))){
    const z=await get(`${process.env.CARHER_CCDL}/${V}/${host}/claude.app.tar.zst`);
    if(sha(z)!==hb.checksum) throw new Error("本机组件 sha256 不对,拒绝安装");
    fs.rmSync(hd,{recursive:true,force:true}); fs.mkdirSync(hd,{recursive:true});
    // 用 Node 自带的 zstd 解(22.15+ 内置;上面已保证 Node>=22)。⛔ 不依赖系统 zstd:
    // 同事 Mac 默认没有,第一版就这么写的,只在装了 brew zstd 的机器上能过 = 假绿。
    if(typeof zlib.zstdDecompressSync!=="function") throw new Error("Node 不支持 zstd(需要 Node>=22.15)");
    const t=path.join(hd,".dl.tar"); fs.writeFileSync(t,zlib.zstdDecompressSync(z));
    cp.execFileSync("tar",["-xf",t,"-C",hd]);
    fs.unlinkSync(t);
    if(!fs.existsSync(path.join(hd,"claude.app/Contents/MacOS/claude"))) throw new Error("本机组件解压失败(需要 zstd)");
    fs.writeFileSync(path.join(hd,".verified"),hb.checksum);
    try{cp.execFileSync("xattr",["-dr","com.apple.quarantine",hd]);}catch{}
  }
  // ② Linux VM 那份(App 的隔离工作区里跑)
  const vp=P[vm]; const vd=path.join(process.env.CARHER_UD,"claude-code-vm",V);
  if(vp && !(ok(vd,vp.checksum) && fs.existsSync(path.join(vd,"claude")))){
    const z=await get(`${process.env.CARHER_CCDL}/${V}/${vm}/claude.zst`);
    if(sha(z)!==vp.checksum) throw new Error("VM 组件 sha256 不对,拒绝安装");
    fs.mkdirSync(vd,{recursive:true});
    fs.writeFileSync(path.join(vd,"claude"),zlib.zstdDecompressSync(z)); fs.chmodSync(path.join(vd,"claude"),0o755);
    fs.writeFileSync(path.join(vd,".verified"),vp.checksum);
  }
  console.log(V);
})().catch(e=>{console.error(e.message);process.exit(3);});
' >"$TMPDIR/carher-ccd.out" 2>"$TMPDIR/carher-ccd.err" \
    && ok "✔ 桌面 App 组件就绪(Claude Code $(cat "$TMPDIR/carher-ccd.out"))" \
    || warn "⚠ 桌面 App 组件没准备好:$(tail -1 "$TMPDIR/carher-ccd.err")。命令行 claude 不受影响。"
  rm -f "$TMPDIR/carher-ccd.out" "$TMPDIR/carher-ccd.err"
fi

# ── 4d. 画图工具(10-16):命令行和桌面 App 的 Code 标签页都会读 ~/.claude/skills ──
# 一个零依赖的 Node 小工具(调 muse-image)+ 一个 skill 告诉 Claude 要画图时调它。
# skill 目录名固定 carher-image,重跑只覆盖这一个目录,不碰用户自己的 skill。
IMG_DIR="$CLAUDE_HOME/carher"; mkdir -p "$IMG_DIR" "$CLAUDE_HOME/skills/carher-image"
cat > "$IMG_DIR/carher_image.js" <<'CARHER_JS_EOF'
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
   2026-10-13 起统一用 muse-image。它返回相对路径 /v1/media/<32hex>.webp(文件在 muse2api 上),
   198 nginx 开了只读出口 https://<网关域名>/muse-media/<32hex>.<ext> ⇒ 改写过去下载。
   输出文件后缀以实际图片格式为准(webp 就存 .webp)。 */
"use strict";
const fs = require("fs"), os = require("os"), path = require("path");

const MODEL = process.env.CARHER_IMAGE_MODEL || "muse-image";
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
    if (r.status === 403) die("你的 key 还没有开通画图模型 " + MODEL + ",请联系运维开通后重试。");
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
CARHER_JS_EOF
NODE_ABS="$(command -v node)"
CARHER_NODE="$NODE_ABS" CARHER_TOOL="$IMG_DIR/carher_image.js" node -e '
const fs=require("fs");let t=fs.readFileSync(0,"utf8");
t=t.split("@@NODE@@").join(process.env.CARHER_NODE).split("@@TOOL@@").join(process.env.CARHER_TOOL);
process.stdout.write(t);' > "$CLAUDE_HOME/skills/carher-image/SKILL.md" <<'CARHER_MD_EOF'
---
name: carher-image
description: 画图 / 生成图片 / 出图 / 做配图、插画、海报、头像、示意图时用。调公司的 muse-image 出一张真图片(约 30 秒),保存到当前文件夹。用户说「画一张…」「生成一张图」「帮我做个配图」都用它,不要用 SVG、ASCII、HTML 或代码画图代替。
---

# 画图

用户要图片时,**只用下面这个命令**(它调公司的 muse-image,key 已经配好):

```
@@NODE@@ "@@TOOL@@" "画面描述" 输出路径
```

- 画面描述用用户的原话,可以补充风格、构图、颜色等细节;用中文或英文都行。
- 输出路径给当前项目里的一个文件夹(比如 `.`)或文件名;后缀会按实际格式自动改(一般是 `.webp`)。
- 约 30 秒出图。以命令最后打印的「已生成:…」那一行为准,把这个路径告诉用户。
- **不要**向用户要 key,也不要在命令里写 key。
- 命令报错就把报错原文告诉用户(比如「你的 key 还没有开通画图模型」),不要换别的方式硬画。
CARHER_MD_EOF
# 再在 ~/.claude/CLAUDE.md 里放一段带标记的常驻说明(10-16 实测:只有 skill 时,一句「画一张…」
# Claude 常直接写 SVG,不去调 skill)。标记块重跑只替换本块,不碰用户自己写的内容;改前备份。
CMD_MD="$CLAUDE_HOME/CLAUDE.md"
[ -f "$CMD_MD" ] && cp -p "$CMD_MD" "$CMD_MD.bak-carher-$(date +%Y%m%d-%H%M%S)"
CARHER_MD="$CMD_MD" CARHER_LINE="\"$NODE_ABS\" \"$IMG_DIR/carher_image.js\" \"画面描述\" 输出路径" node -e '
const fs=require("fs"),p=process.env.CARHER_MD,B="<!-- CARHER-IMAGE-BEGIN -->",E="<!-- CARHER-IMAGE-END -->";
let t="";try{t=fs.readFileSync(p,"utf8")}catch(e){}
const i=t.indexOf(B),j=t.indexOf(E);if(i>=0&&j>i)t=t.slice(0,i)+t.slice(j+E.length);
t=t.replace(/\n+$/,"");
const blk=B+"\n## 画图\n\n用户要画图、生成图片、出图、做配图/插画/海报/头像时,**只用这个命令**(调公司的 muse-image,key 已配好),"
 +"不要用 SVG / ASCII / HTML / 代码画图代替:\n\n    "+process.env.CARHER_LINE+"\n\n"
 +"- 约 30 秒出图;以命令最后打印的「已生成:…」路径为准告诉用户。\n- 不要向用户要 key。命令报错就把原文告诉用户,不要换别的方式硬画。\n"+E+"\n";
fs.writeFileSync(p,(t?t+"\n\n":"")+blk);'
ok "✔ 已开通画图(说「画一张…」即可)"

# ── 4e. 「刷模型」(10-19):说这三个字就按 key 重拉 /model 菜单(旧的删、新的加)──
# 10-27:旧名 carher-refresh-models 和 Cursor 的同名 ⇒ Cursor agent 也读 ~/.claude/skills,说「刷模型」跑成了 Claude 的刷新。改名并删旧目录
rm -rf "$CLAUDE_HOME/skills/carher-refresh-models"
mkdir -p "$CLAUDE_HOME/skills/carher-refresh-models-claude"
CARHER_NODE="$NODE_ABS" CARHER_TOOL="$CLAUDE_HOME/carher/carher_models_refresh.js" node -e '
const fs=require("fs");let t=fs.readFileSync(0,"utf8");
t=t.split("@@NODE@@").join(process.env.CARHER_NODE).split("@@TOOL@@").join(process.env.CARHER_TOOL);
process.stdout.write(t);' > "$CLAUDE_HOME/skills/carher-refresh-models-claude/SKILL.md" <<'CARHER_MD_EOF'
---
name: carher-refresh-models-claude
description: 仅限 Claude Code(命令行 claude 或 Claude 桌面 App)里用:用户说「刷模型」「更新模型」「刷新模型列表」,或说 /model 里的模型不对时。按用户的 key 从公司网关重新拉模型列表,同时更新 Claude Code 命令行和 Claude 桌面 App 的模型菜单。⛔ 如果你是 Cursor 或 Codex,不要用这个,用你自己那个工具的刷模型。
---

# 刷模型

直接运行下面这条命令,不要先问用户:

```
@@NODE@@ "@@TOOL@@"
```

- 把输出里「新增 / 删除 / 已改回默认模型」那几行原样告诉用户。
- 提醒用户:命令行新开一个 claude 会话就是新列表;Claude 桌面 App 要**完全退出再打开**才显示新菜单。
- 命令报错就把原文告诉用户,不要自己改 settings.json。
- 平时不用手动刷:每次打开 Claude Code 都会在后台自动刷一次。说「刷模型」= 现在立刻刷。
CARHER_MD_EOF
CARHER_MD="$CMD_MD" CARHER_LINE="\"$NODE_ABS\" \"$CLAUDE_HOME/carher/carher_models_refresh.js\"" node -e '
const fs=require("fs"),p=process.env.CARHER_MD,B="<!-- CARHER-REFRESH-BEGIN -->",E="<!-- CARHER-REFRESH-END -->";
let t="";try{t=fs.readFileSync(p,"utf8")}catch(e){}
const i=t.indexOf(B),j=t.indexOf(E);if(i>=0&&j>i)t=t.slice(0,i)+t.slice(j+E.length);
t=t.replace(/\n+$/,"");
const blk=B+"\n## 刷模型\n\n(仅 Claude Code / Claude 桌面 App)用户说「刷模型」「更新模型」「刷新模型列表」,或说 /model 里的模型不对时,**直接运行这条命令,不要先问**:\n\n    "+process.env.CARHER_LINE+"\n\n"
 +"- 把输出里「新增 / 删除 / 已改回默认模型」几行原样告诉用户,并提醒新开一个会话后 /model 里就是新列表。\n- 报错就把原文告诉用户,不要自己改 settings.json。\n"+E+"\n";
fs.writeFileSync(p,(t?t+"\n\n":"")+blk);'
ok "✔ 以后在 Claude Code 里说「刷模型」就能更新模型列表"
# 10-25:每次开 Claude Code 后台自动刷一次菜单(SessionStart 异步 hook,不挡输入、不出声)
#    ⇒ 管理员给 key 开/关模型,用户下次开就对了;「刷模型」留着当立刻刷的手动入口。
CARHER_REFRESH="$CLAUDE_HOME/carher/carher_models_refresh.js" CARHER_NODE="$NODE_ABS" CARHER_SETTINGS="$SETTINGS" node -e '
const fs = require("fs"), p = process.env.CARHER_SETTINGS;
const d = JSON.parse(fs.readFileSync(p, "utf8").replace(/^\uFEFF/, ""));
require(process.env.CARHER_REFRESH).hookEntry(d, process.env.CARHER_NODE, process.env.CARHER_REFRESH);
fs.writeFileSync(p, JSON.stringify(d, null, 2) + "\n", { mode: 0o600 });' \
  && ok "✔ 以后每次打开 Claude Code 会自动按 key 更新 /model 菜单" \
  || warn "⚠ 自动刷新没挂上(说「刷模型」照样能手动刷)"

# (会盖过配置的旧 export 在第 5 步测试之后统一处理:注释掉 + 备份,见下)
echo

# ── 5. 验证:真发一句 ────────────────────────────────────────────────
say "→ 测试一下 ..."
A=$((RANDOM % 9000 + 1000)); B=$((RANDOM % 9000 + 1000))
# 只认 settings.json:把会盖过它的 shell 变量全清掉再测(测的是「新开终端」会拿到的东西)
UNSET_ARGS=""
for v in ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_API_KEY CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC \
         ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
         ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_DEFAULT_FABLE_MODEL ANTHROPIC_SMALL_FAST_MODEL; do
  UNSET_ARGS="$UNSET_ARGS -u $v"
done
# shellcheck disable=SC2086
OUT="$(env $UNSET_ARGS "$CLAUDE_BIN" --print "What is $A+$B? Answer with just the number." </dev/null 2>&1 | tail -n1 || true)"
if printf '%s' "$OUT" | grep -q "$((A + B))"; then
  ok "✔ 通了(问 $A+$B,答 $OUT)"
else
  warn "⚠ 测试没拿到预期回答(回:${OUT:-空})。配置已写好,先新开一个终端试试 claude。"
fi
# 测试是在「清掉 shell 变量」的条件下过的;如果 rc 里有会盖过它的 export,新开终端照样会坏 ⇒
# 不能只提醒,要把它们**注释掉**(加标记,可还原),否则用户一开终端就 403 而屏幕上只写"完成"。
FIXED=0
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
  [ -f "$rc" ] || continue
  if grep -qE '^[[:space:]]*export[[:space:]]+(ANTHROPIC_(AUTH_TOKEN|BASE_URL|API_KEY|MODEL|DEFAULT_(OPUS|SONNET|HAIKU|FABLE)_MODEL|SMALL_FAST_MODEL)|CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)=' "$rc"; then
    cp -p "$rc" "$rc.bak-carher-$(date +%Y%m%d-%H%M%S)"
    tmp="$(mktemp)"
    awk '
      /# >>> carher-claude >>>/{s=1} /# <<< carher-claude <<</{s=0; print; next}
      !s && /^[[:space:]]*export[[:space:]]+(ANTHROPIC_(AUTH_TOKEN|BASE_URL|API_KEY|MODEL|DEFAULT_(OPUS|SONNET|HAIKU|FABLE)_MODEL|SMALL_FAST_MODEL)|CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)=/ {
        print "# [carher-claude 已注释,会盖过 ~/.claude/settings.json] " $0; next }
      { print }' "$rc" > "$tmp" && cat "$tmp" > "$rc"
    rm -f "$tmp"; FIXED=1
    warn "↳ $rc 里会盖过配置的旧 export 已注释掉(原文件已备份成 $rc.bak-carher-*)"
  fi
done
[ "$FIXED" = 1 ] && warn "↳ 请**关掉现在这个终端,新开一个**再用 claude(旧变量还留在当前窗口里)"

echo
ok "==================================================="
ok "   完成！"
ok "==================================================="
echo "  新开一个终端窗口,进到你的项目文件夹,输入:  claude"
echo "  换模型:在 Claude Code 里输入  /model"
echo
