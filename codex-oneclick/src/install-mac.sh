#!/usr/bin/env bash
# CarHer Codex 一键配置 (macOS / Linux)
# 用法：Finder 里双击本文件，或终端里执行 `bash codex-mac.command`
# ⚠️ 这是模板：改这里，然后跑 ../build.sh 生成用户拿到的 codex-mac.command
# 没有 codex 命令行会自动装（npmmirror）；Desktop App 需用户自己装（文档里有安装包）
set -euo pipefail

BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; NC=$'\033[0m'
say()  { printf '%s%s%s\n' "$BLUE" "$1" "$NC"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$1" "$NC"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$1" "$NC"; }
die()  { printf '%s%s%s\n' "$RED" "$1" "$NC" >&2; exit 1; }

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

# 固定配置（用户无需感知）
BASE_URL="https://cc.auto-link.com.cn/pro/v1"
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
  ver="$(curl -fsSL --max-time 30 "$NODE_MIRROR/index.json" \
    | grep -oE '"version":"v'"$NODE_INSTALL_MAJOR"'\.[0-9]+\.[0-9]+"[^}]*"lts":"[A-Za-z]+"' \
    | head -n1 | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')" || return 1
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
@@EMBED carher_image.js@@
CARHER_PY_EOF
cat > "$CODEX_HOME/carher_image.py" <<'CARHER_PY_EOF'
@@EMBED carher_image.py@@
CARHER_PY_EOF
cat > "$CODEX_HOME/carher_models_sync.py" <<'CARHER_PY_EOF'
@@EMBED carher_models_sync.py@@
CARHER_PY_EOF
cat > "$CODEX_HOME/carher_models_sync.js" <<'CARHER_PY_EOF'
@@EMBED carher_models_sync.js@@
CARHER_PY_EOF
chmod +x "$CODEX_HOME/carher_image.py" "$CODEX_HOME/carher_image.js" 2>/dev/null || true

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

用户要画图、生成图片、出图、做配图/插画/海报/头像时，**只用下面这个命令**（它调公司的 muse-image），
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
echo "想换模型（列表里都是你这把 key 能用的，每天自动更新）："
echo "  - 终端 codex：输入 /model 回车，上下键选，回车确认（会记住，下次默认用它）"
echo "  - 只这一次用某个模型：codex -m grok-4.7"
echo "  - Desktop App：点底部的模型选择器"
echo
