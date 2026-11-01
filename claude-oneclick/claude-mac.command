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
BASE_URL="https://chat.auto-link.com.cn/model"
LEGACY_BASE_URL="https://cc.auto-link.com.cn/pro"   # 10-29 前的旧地址,共存期只在新地址不通时兜底
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
# 10-29 换了网关域名(旧地址还能用一段时间):新地址这会儿不通、旧地址通 ⇒ 先用旧地址,
#   以后每次开 Claude Code 的自动刷新会在新地址通了以后自动换过去。
if [ "$CODE" != 200 ] && [ "$CODE" != 401 ] && [ "$CODE" != 403 ]; then
  OLD_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -H "x-api-key: $APIKEY" "$LEGACY_BASE_URL/v1/models" || echo 000)"
  if [ "$OLD_CODE" = 200 ]; then
    warn "↳ 新网关地址暂时不通(HTTP ${CODE}),这次先用旧地址;以后会自动换到新地址。"
    BASE_URL="$LEGACY_BASE_URL"; CODE=200
  fi
fi
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

/* 10-29 换对外域名:网关地址从旧的 /pro 换成 chat.auto-link.com.cn/model(同一个网关,路径改名)。
   旧地址共存一段时间 ⇒ 新旧两种都认作「我们的网关」。是旧的 ⇒ 先用这把 key 打新地址 /v1/models,
   拿到列表(200)才改写;不通就保持原样,下次开会话再试。只认我们那一个地址,用户自己指的别的网关不碰。 */
const NEW_BASE = process.env.CARHER_MIGRATE_TO || "https://chat.auto-link.com.cn/model";   // 环境变量只给测试用
const LEGACY_BASE = /^https:\/\/cc\.auto-link\.com\.cn\/pro\/?$/;
const isLegacyBase = (b) => LEGACY_BASE.test(String(b || "").trim());
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
/* newBase:这次已用这把 key 在新地址上拉到了列表 ⇒ App 我们那条还写着旧地址就一起换过去(10-29 换域名)。 */
function syncAppModels(ids, key, newBase) {
  const want = ids.filter((i) => /^claude-/.test(i) && !/image|imagine|video|music/.test(i))
    .sort((a, b) => familyRank(a) - familyRank(b) || b.localeCompare(a, "en", { numeric: true }));
  const res = [];
  for (const p of appConfigEntries()) {
    let c; try { c = JSON.parse(fs.readFileSync(p, "utf8")); } catch (e) { continue; }
    if (key && c.inferenceGatewayApiKey && c.inferenceGatewayApiKey !== key) continue;   // 不是这把 key 配的那条,不动
    const baseMoved = !!(newBase && isLegacyBase(c.inferenceGatewayBaseUrl));
    if (baseMoved) c.inferenceGatewayBaseUrl = newBase;
    const before = Array.isArray(c.inferenceModels) ? c.inferenceModels.map((m) => (typeof m === "string" ? m : m && m.name)).filter(Boolean) : null;
    let menuChanged = true;
    if (!want.length) {
      // key 里一个 Claude 模型都没有 ⇒ 别写空列表(App 会没有可选的),交回 App 自己拉
      if (before) { delete c.inferenceModels; } else if (baseMoved) menuChanged = false; else continue;
    } else if (before && before.join("\n") === want.join("\n")) {
      if (!baseMoved) { res.push({ p, changed: false, models: want }); continue; }
      menuChanged = false;
    } else c.inferenceModels = want;
    const tmp = p + ".carher-tmp-" + process.pid;
    fs.writeFileSync(tmp, JSON.stringify(c, null, 2) + "\n", { mode: 0o600 });
    fs.renameSync(tmp, p);
    res.push({ p, changed: true, menuChanged, baseMoved, models: want, added: want.filter((m) => !(before || []).includes(m)), removed: (before || []).filter((m) => !want.includes(m)) });
  }
  return res;
}

/* settings.json 里是旧网关地址 ⇒ 用这把 key 打新地址;拿到列表才改 d.env.ANTHROPIC_BASE_URL。
   fetch 可注入(测试)。返回 {ids, base, moved:bool, why?}:moved=false 时 ids 为 null,调用方照旧地址拉。 */
async function migrateIfReachable(d, key, fetch = fetchIdsLive) {
  const cur = d && d.env && d.env.ANTHROPIC_BASE_URL;
  if (!isLegacyBase(cur)) return { ids: null, base: cur, moved: false };
  try {
    const ids = await fetch(NEW_BASE, key);
    if (!ids.length) return { ids: null, base: cur, moved: false, why: "新地址没返回任何模型" };
    d.env.ANTHROPIC_BASE_URL = NEW_BASE;
    return { ids, base: NEW_BASE, moved: true };
  } catch (e) { return { ids: null, base: cur, moved: false, why: e.message }; }
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
  return fetchIdsLive(base, key);
}
/* 一定真打网关(不吃 CARHER_MODELS):换地址前「新地址真的通」只能靠真请求判 */
async function fetchIdsLive(base, key) {
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
  // 10-29 换域名:旧地址 ⇒ 新地址拿得到列表才改写(只在没被 CARHER_BASE 指定时;安装器/测试会指定)
  const mig = process.env.CARHER_BASE ? { ids: null, moved: false } : await migrateIfReachable(d, key);
  if (mig.moved) { ids = mig.ids; logLine(`moved base ${base} -> ${mig.base}`); }
  else if (mig.why) logLine(`keep old base (新地址不通:${mig.why})`);
  try { if (!ids) ids = await fetchIds(base, key); }
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
  // App 那条只在「这一趟真从新地址拉到了列表」时才换地址(CARHER_MODELS 注入的不算真通)
  const newVerified = mig.moved || (base === NEW_BASE && process.env.CARHER_MODELS == null);
  try { app = syncAppModels(ids, key, newVerified ? NEW_BASE : null); } catch (e) { logLine("app sync failed " + e.message); }
  for (const a of app) if (a.changed) logLine("app menu " + a.models.join(",") + (a.baseMoved ? " base->" + NEW_BASE : ""));
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
  if (mig.moved) say(`网关地址已换成新地址 ${NEW_BASE}(同一个网关,换了域名)`);
  for (const a of app) {
    if (!a.changed) { say(`桌面 App 菜单和上次一样(${a.models.length} 个 Claude 模型)`); continue; }
    if (a.baseMoved) say(`桌面 App 的网关地址也换成了 ${NEW_BASE}`);
    if (a.menuChanged === false) { say("  ⚠️ 桌面 App 要**完全退出再打开**才会用新地址"); continue; }
    say(`桌面 App 菜单已更新为 ${a.models.length} 个 Claude 模型:${a.models.join(", ")}`);
    if (a.added && a.added.length) say(`  App 新增:${a.added.join(", ")}`);
    if (a.removed && a.removed.length) say(`  App 删除:${a.removed.join(", ")}`);
    say("  ⚠️ 桌面 App 要**完全退出再打开**才会显示新菜单(Mac:Cmd+Q;Windows:右下角托盘图标右键退出)");
  }
  if (keyFixed) say("已修好「Not logged in」:这把 key 之前被拒绝过,现在记成已同意");
  say("新开一个 Claude Code 会话(或桌面 App 新开对话)后 /model 里就是新列表");
}

module.exports = { pickerRows, applyModels, hookEntry, approveApiKey, syncAppModels, appConfigEntries, migrateIfReachable, isLegacyBase, NEW_BASE };

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
#    ⇒ 从我们自己的镜像 chat.auto-link.com.cn/ccdl/ 取**官方原样**文件,按 App 的目录形状放好,
#    并用 App 内嵌 manifest 的 sha256 校验。版本号从 App 里读(App 升级后自动跟)。
# 10-28 起 Linux 也有官方 App(deb,/usr/lib/claude-desktop),同样每开会话先去国外下组件 ⇒ 一起预置。
if [ "$OS" = darwin ]; then APP_ASAR="/Applications/Claude.app/Contents/Resources/app.asar"
else APP_ASAR="/usr/lib/claude-desktop/resources/app.asar"; fi
CCDL="https://chat.auto-link.com.cn/ccdl/claude-code-releases"
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

# ── 4d2. 一句话生成视频(11-01):同一个路子,skill carher-video + CLAUDE.md 标记块 ──
mkdir -p "$CLAUDE_HOME/skills/carher-video"
cat > "$IMG_DIR/carher_video.js" <<'CARHER_JS_EOF'
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
CARHER_JS_EOF
CARHER_NODE="$NODE_ABS" CARHER_TOOL="$IMG_DIR/carher_video.js" node -e '
const fs=require("fs");let t=fs.readFileSync(0,"utf8");
t=t.split("@@NODE@@").join(process.env.CARHER_NODE).split("@@TOOL@@").join(process.env.CARHER_TOOL);
process.stdout.write(t);' > "$CLAUDE_HOME/skills/carher-video/SKILL.md" <<'CARHER_MD_EOF'
---
name: carher-video
description: 生成视频 / 做短视频 / 出一段视频 / 把一句话变成视频 / 做个动画视频时用。调公司的 gemini-video 真实生成一段约 10 秒、带声音的 mp4(约 2 分钟),保存到当前文件夹。用户说「生成一个视频…」「做个短视频」「帮我出一段…的视频」都用它,不要用代码画动画、PIL/ffmpeg 合成、HTML 或别的方式代替,也不要说「没法生成视频」。
---

# 生成视频

用户要视频时,**只用下面这个命令**(它调公司的 gemini-video,key 已经配好):

```
@@NODE@@ "@@TOOL@@" "画面描述" 输出路径
```

- 画面描述用用户的原话,可以补充镜头、动作、风格、光线等细节;中英文都行。
  输出路径给当前项目里的一个文件夹(比如 `.`)或 `.mp4` 文件名。用户要竖屏(手机、抖音)就在末尾加 `--portrait`。
- 命令最多等约 1.5 分钟就会自己结束。
  - 最后一行是「已生成:…」⇒ 把这个路径告诉用户,完成。
  - 打印「⏳ 视频还没好」⇒ 视频在后台继续生成。**运行它给出的那条 `--wait` 命令接着等**,
    还没好就再运行一次,直到出现「已生成:…」。不要重新提交原命令,不要另开 ls / ps / sleep 去查。
- 这条命令要联网、要写 `~/.carher`:在有命令沙箱的工具里请直接申请在沙箱外运行(Cursor:`required_permissions: ["all"]`)。
  报「被命令沙箱拦住了」「连不上网关」时还没扣额度,在沙箱外重跑同一条命令即可。
  命令被工具转到后台时,等它结束、读最后几行结果,不要直接结束回合。
- 一次只出一个;用户要多个就一个出完再出下一个。
- **不要**向用户要 key,也不要在命令里写 key。
- 命令报错就把报错原文告诉用户(比如「你的 key 还没有开通视频模型」「视频服务这会儿忙」),**不要自动重试**、不要换别的方式硬做;
  用户说再试一次时,在原命令末尾加 `--retry`。
CARHER_MD_EOF
CARHER_MD="$CMD_MD" CARHER_LINE="\"$NODE_ABS\" \"$IMG_DIR/carher_video.js\" \"画面描述\" 输出路径" node -e '
const fs=require("fs"),p=process.env.CARHER_MD,B="<!-- CARHER-VIDEO-BEGIN -->",E="<!-- CARHER-VIDEO-END -->";
let t="";try{t=fs.readFileSync(p,"utf8")}catch(e){}
const i=t.indexOf(B),j=t.indexOf(E);if(i>=0&&j>i)t=t.slice(0,i)+t.slice(j+E.length);
t=t.replace(/\n+$/,"");
const blk=B+"\n## 生成视频\n\n用户要生成视频、做短视频、出一段视频时，**只用下面这个命令**（它调公司的 gemini-video 真实生成 10 秒带声音的 mp4），**不要说「没法生成视频」，不要用代码画动画 / PIL / ffmpeg / HTML 合成代替**：\n\n    "+process.env.CARHER_LINE+"\n\n"
 +"- 画面描述用用户的原话，可以补充镜头、动作、风格、光线等细节。用户要竖屏（手机、抖音）就在末尾加 --portrait。\n- 命令最多等约 1.5 分钟就自己结束：最后一行是「已生成：…」就把这个路径告诉用户；\n  打印「⏳ 视频还没好」就**运行它给出的那条 --wait 命令接着等**（还没好就再运行一次），直到「已生成：…」。\n  不要重新提交原命令（会多扣一次额度），不要另开 ls / ps / sleep 去查。\n- 一次只出一个。key 已配好，不要向用户索要，也不要在命令里写 key。\n- 命令报错就把报错原文告诉用户，**不要自动重试**、不要换别的方式硬做；用户说再试一次时在原命令末尾加 --retry。\n"+E+"\n";
fs.writeFileSync(p,(t?t+"\n\n":"")+blk);'
ok "✔ 已开通生成视频(说「生成一个…的视频」即可)"

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
