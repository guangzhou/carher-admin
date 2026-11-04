#!/usr/bin/env bash
# CarHer AI 一行命令安装(macOS / Linux)—— 网页给的那一行:
#   curl -fsSL https://chat.auto-link.com.cn/ai/i/<tool>.sh | bash -s -- --code <安装码>
# nginx 把 cursor.sh / codex.sh / claude.sh / all.sh 都指到本文件;装哪个以**安装码换回来的 tool** 为准
# (--tool 只用来对一下,不一致就提醒)。tool=all ⇒ 一条命令依次装三个。
#
# 做的事(全程不问用户要 key):
#   1. 安装码 → key(POST /ai/api/install-code/redeem,只换一次;失败把服务端的人话原样打出来)
#   2. 没装 App 就从我们的镜像装(/ccdl/apps、/ccdl/desktop),已装跳过;按 bundle id 找,不写死路径
#   3. 下载并运行现有的一键安装器(codex-mac.command / claude-mac.command / cursor-mac.zip 引擎),
#      key 走环境变量 CARHER_KEY 交给它(不进命令行参数),stdin 接 /dev/null(不会卡在交互读)
#   4. 替用户把 App 退出再打开
#
# ── tool=all(一条命令装三个)────────────────────────────────────────────
# 🔴 顺序写死 codex → claude → cursor,**Cursor 一定在最后**:
#    只有 Cursor 要求「写配置前它必须完全退出」,而且第一次用还要先把它打开、等 state.vscdb
#    初始化(最长 2 分钟),引擎还有两条硬闸(开着 exit 2 / 没库 exit 3)。这一步最容易卡在
#    「Cursor 在等你保存文件」这种**要人动手**的地方。放最后 ⇒ 卡住时另外两个已经装完了;
#    放最前则会把后面两个一起拖住。codex / claude 不需要退 App(装完才重启),先装最省事。
# 🔴 安装码**只换一次**(一次性),拿到的 key 复用给三个工具;每个工具各自 try,一个失败不影响另外两个。
# 退出码:0 = 三个都装好;5 = 都装上了但有标红项;7 = 部分成功(汇总里写了哪个没成);8 = 三个都没成。
#
# 🔴 整个脚本包在 main() 里、最后一行才调用:`curl | bash` 时 bash 是边下边读 stdin 执行的,
#    不包的话 ① 下载断在半截会执行半截脚本 ② 子进程一读 stdin 就把后面的脚本当输入吃掉。
# 🔴 key 只在内存和子进程环境里;屏幕上只打 长度 + 后 4 位;子安装器的输出也过一遍遮罩。
# 🔴 先换码再下 App:码 10 分钟过期,App 包 400~800MB,慢网下完码早过期了。代价是后面任何一步
#    失败码都已用掉 ⇒ 失败提示统一写「回网页重新生成一条命令」。
#
# 只给测试用的环境变量(用户不需要知道):
#   CARHER_PORTAL_BASE      默认 https://chat.auto-link.com.cn/ai
#   CARHER_MIRROR_BASE      默认 https://chat.auto-link.com.cn/ccdl
#   CARHER_APP_DEST_ROOTS   App 装到哪(冒号分隔,取第一个可写的),默认 /Applications:$HOME/Applications
#   CARHER_SKIP_APP_INSTALL=1  不装 App(只配置)
#   CARHER_APP_ROOTS        透传给 find_app_mac.sh(台架用)

main() {
set -euo pipefail

BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; NC=$'\033[0m'
say()  { printf '%s%s%s\n' "$BLUE" "$1" "$NC"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$1" "$NC"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$1" "$NC"; }
FALLBACK_PAGE="https://chat.auto-link.com.cn/oneclick/"
# ERR_SINK:tool=all 时每个工具在自己的子 shell 里跑,失败原因写进这个文件,最后汇总时原话读回来
# (子 shell 的变量传不回父 shell)。单工具时不设 ⇒ 行为和以前完全一样。
die()  { if [ -n "${ERR_SINK:-}" ]; then printf '%s\n' "$1" >> "$ERR_SINK" 2>/dev/null || true; fi
         printf '%s%s%s\n' "$RED" "$1" "$NC" >&2; exit "${2:-1}"; }
MULTI=0   # 1 = 正在 tool=all 的流程里(每个工具的收尾话术不一样)

PORTAL="${CARHER_PORTAL_BASE:-https://chat.auto-link.com.cn/ai}"; PORTAL="${PORTAL%/}"
MIRROR="${CARHER_MIRROR_BASE:-https://chat.auto-link.com.cn/ccdl}"; MIRROR="${MIRROR%/}"

# ── 参数 ────────────────────────────────────────────────────────────
CODE="${CARHER_INSTALL_CODE:-}"; TOOL_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --code) CODE="${2:-}"; shift 2 || shift ;;
    --code=*) CODE="${1#--code=}"; shift ;;
    --tool) TOOL_ARG="${2:-}"; shift 2 || shift ;;
    --tool=*) TOOL_ARG="${1#--tool=}"; shift ;;
    *) shift ;;
  esac
done

echo
say "==================================================="
say "   CarHer AI 一行命令安装 (macOS / Linux)"
say "==================================================="
echo

[ -n "$CODE" ] || die "✗ 命令里没有安装码。回网页 ${PORTAL}/ 重新复制完整的一行命令。"
# 码只能是字母数字和 - _ :它要拼进 JSON 请求体,别的字符一律不收(防注入,也防复制时带进空格/引号)
case "$CODE" in *[!A-Za-z0-9_-]*) die "✗ 安装码格式不对(只应有字母、数字、-、_)。回网页重新复制完整的一行命令。" ;; esac

case "$(uname -s)" in Darwin) OS=darwin ;; Linux) OS=linux ;;
  *) die "✗ 不支持的系统:$(uname -s)。Windows 请用网页上的 PowerShell 那条命令。" ;; esac

WROOT="$(mktemp -d "${TMPDIR:-/tmp}/carher-install.XXXXXX")"; W="$WROOT"; MNT=""
trap '[ -n "$MNT" ] && hdiutil detach -quiet "$MNT" >/dev/null 2>&1; rm -rf "$WROOT"' EXIT

mask_key() { printf '长度 %s,后4位 …%s' "${#1}" "${1: -4}"; }

# 从 JSON 里取一个字符串字段。mac 用 plutil(系统自带,能解 \uXXXX);
# ⛔ mac 上不碰 python3:没装开发者工具时 /usr/bin/python3 是个会弹安装窗口的桩。
json_field() {
  local f="$1" body="$2" v=""
  if [ "$OS" = darwin ] && command -v plutil >/dev/null 2>&1; then
    v="$(printf '%s' "$body" | plutil -extract "$f" raw -o - - 2>/dev/null || true)"
  elif command -v python3 >/dev/null 2>&1; then
    v="$(printf '%s' "$body" | python3 -c 'import json,sys
try:
    v=json.load(sys.stdin).get(sys.argv[1])
    print(v if isinstance(v,str) else "")
except Exception: pass' "$f" 2>/dev/null || true)"
  fi
  if [ -z "$v" ]; then  # 兜底:纯文本抠(不解转义)
    v="$(printf '%s' "$body" | sed -n 's/.*"'"$f"'"[[:space:]]*:[[:space:]]*"\([^"\\]*\)".*/\1/p' | head -n1)"
  fi
  printf '%s' "$v"
}

# ── 1. 安装码 → key ──────────────────────────────────────────────────
say "→ 用安装码换取配置 ..."
# 不加 --retry:POST 已到服务端但回包丢了时,重试只会拿到「已用过」,反而把真原因盖掉
RESP="$(curl -sS --connect-timeout 15 --max-time 30 \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  --data-binary "{\"code\":\"${CODE}\"}" -w '\n__CARHER_HTTP__%{http_code}' \
  "${PORTAL}/api/install-code/redeem" 2>&1 || true)"
HTTP="${RESP##*__CARHER_HTTP__}"
BODY="${RESP%__CARHER_HTTP__*}"; BODY="${BODY%$'\n'}"
if [ "$HTTP" != 200 ]; then
  ERR="$(json_field error "$BODY")"
  if [ -z "$ERR" ]; then  # 不是我们服务的标准回包(网络错/网关错)⇒ 原文截一段,顺手遮掉万一出现的 key
    ERR="$(printf '%s' "$BODY" | sed 's/sk-[A-Za-z0-9_-]*/sk-***/g' | head -c 300)"
  fi
  if [ "$HTTP" = 000 ]; then
    die "✗ 连不上安装服务(${PORTAL}):${ERR}
  检查网络后,回网页重新生成一条命令再试。"
  fi
  die "✗ 安装码没换成功(HTTP ${HTTP}):${ERR}"
fi
KEY="$(json_field key "$BODY")"; TOOL="$(json_field tool "$BODY")"
unset RESP BODY
[ -n "$KEY" ] || die "✗ 安装服务回包里没有 key(服务端问题),把这一屏截图发给运维。"
case "$KEY" in *[!A-Za-z0-9_-]*) die "✗ 安装服务给的 key 格式不对(服务端问题),把这一屏截图发给运维。" ;; esac
# 🔴 顺序 = codex → claude → cursor,Cursor 最后(理由见文件头)。改顺序要同时改 test_installer.py 里钉顺序那条。
ALL_TOOLS="codex claude cursor"
case "$TOOL" in cursor|codex|claude) ;; all) MULTI=1 ;;
  *) die "✗ 安装服务没说要装哪个工具(tool=${TOOL:-空}),把这一屏截图发给运维。" ;; esac
if [ -n "$TOOL_ARG" ] && [ "$TOOL_ARG" != "$TOOL" ]; then
  if [ "$MULTI" = 1 ]; then
    warn "↳ 这条命令的网址是 ${TOOL_ARG},但安装码是「三个都装」的 ⇒ 按安装码装三个。"
  else
    warn "↳ 这条命令的网址是 ${TOOL_ARG},但安装码是给 ${TOOL} 的 ⇒ 按安装码装 ${TOOL}。"
  fi
fi
if [ "$MULTI" = 1 ]; then
  ok "✔ 安装码有效:三个都装(Codex → Claude Code → Cursor),key 已拿到($(mask_key "$KEY"))"
else
  ok "✔ 安装码有效:装 ${TOOL},key 已拿到($(mask_key "$KEY"))"
fi
echo

# 子进程输出过一遍遮罩:万一哪个安装器把 key 打出来,屏幕上也只剩 ***。
# key 走环境变量进 perl(不进命令行参数);按块读、留 len-1 字节尾巴,跨块的 key 也遮得住,进度条不卡。
mask_stream() {
  if command -v perl >/dev/null 2>&1; then
    CARHER_MASK_K="$KEY" perl -e '$|=1;$k=$ENV{CARHER_MASK_K};$n=length($k);$t="";
      while(sysread(STDIN,$b,4096)){$t.=$b;$t=~s/\Q$k\E/***/g;
        if(length($t)>=$n){print substr($t,0,length($t)-$n+1);$t=substr($t,length($t)-$n+1);}}
      $t=~s/\Q$k\E/***/g;print $t;'
  else cat; fi
}

dl() {  # dl <url> <文件>:GET 可以安全重试
  curl -fSL --connect-timeout 15 --retry 3 --retry-delay 2 --progress-bar -o "$2" "$1"
}

# ── 2. App ───────────────────────────────────────────────────────────
# >>> find_app_mac.sh(原样拷自 codex-oneclick/src/find_app_mac.sh;test_installer.py 逐字节比对,改那边要同步这里) >>>
# ── 找 Claude 桌面 App(mac)。安装器和自检共用一份源,别各写一份 ──
# 🔴 11-03 buyitian 实机:App 明明装着,自检却报「没装(只用命令行可以忽略)」 ——
#    因为这里原来写死 `/Applications/Claude.app`。装在别处(个人 ~/Applications、
#    或自己改过 .app 名字)就一律看不见,而**误报"没装"比不报更糟**:
#    用户按提示去装一遍,或者干脆以为自己该忽略,真问题(App 没配网关 / 组件没预置
#    ⇒ 发消息不回)被这句话盖住。安装器那边同一处写死还会让组件预置整段静默跳过。
# ⇒ 按 **bundle id** 找,不认死路径、也不认 .app 的名字。
#    CARHER_DOCTOR_APP 可强制指定;CARHER_APP_ROOTS 只给台架用。
CARHER_CLAUDE_BUNDLE_ID=com.anthropic.claudefordesktop
CARHER_CODEX_BUNDLE_ID=com.openai.codex
# 11-04:网页一行命令(ai-portal 安装码)要判 Cursor 装没装。Cursor 的 bundle id 是 todesktop 打包时给的,
#    本机 /Applications/Cursor.app 实测(`defaults read …/Info CFBundleIdentifier`)。
CARHER_CURSOR_BUNDLE_ID=com.todesktop.230313mzl4w4u92

# 真身判据 = 有 app.asar(Claude/Codex)或 app/package.json(Cursor 不打 asar,资源是目录)
#            + MacOS 下有可执行文件(光有个壳子目录不算)
_cfa_ok() {
  [ -n "$1" ] && [ -d "$1" ] &&
    { [ -f "$1/Contents/Resources/app.asar" ] || [ -f "$1/Contents/Resources/app/package.json" ]; } &&
    [ -n "$(ls "$1/Contents/MacOS" 2>/dev/null | head -n1)" ]
}
_cfa_id() { defaults read "$1/Contents/Info" CFBundleIdentifier 2>/dev/null; }

# 用法:carher_find_app_by_id <bundle id> [名字里该含的词]
#   第二个参数是**同 id 多装**时的挑选偏好,不是过滤条件:
#   🔴 11-03 本机实测:`/Applications/ChatGPT.app` 和 `/Applications/OpenAI Codex.app`
#   **CFBundleIdentifier 都是 com.openai.codex**(同一个 bundle,ChatGPT 自带 codex-cli)。
#   只按 id 挑会挑到 ChatGPT.app,屏幕上打出来让人以为认错了 App。
#   ⇒ 带这个词的优先;一个都不带才退回第一个命中(功能上两者都有 codex-cli,都能用)。
carher_find_app_by_id() {
  _cfa_want="$1"; _cfa_hint="$2"
  if [ -n "${CARHER_DOCTOR_APP:-}" ]; then
    [ -d "$CARHER_DOCTOR_APP" ] && printf '%s' "$CARHER_DOCTOR_APP"
    return
  fi
  [ "$(uname -s)" = Darwin ] || return

  _cfa_first=""
  # ① 问系统:按 bundle id 找,装在哪、叫什么名字都认
  #    🔴 结果必须逐行读:`for a in $(...)` 会按**空格**切开 ——
  #    「/Applications/Claude for Desktop.app」或装在「~/My Apps/」下都会被切碎成一堆不存在的路径,
  #    于是又回到「装着却说没装」。11-03 我自己先写错过一次,台架里的「改名+带空格」那条当场红。
  while IFS= read -r a; do
    _cfa_ok "$a" || continue
    [ -n "$_cfa_first" ] || _cfa_first="$a"
    case "$_cfa_hint" in
      "") printf '%s' "$a"; return;;
      *) case "$a" in *"$_cfa_hint"*) printf '%s' "$a"; return;; esac;;
    esac
  done <<EOF
$(mdfind "kMDItemCFBundleIdentifier == '$_cfa_want'" 2>/dev/null)
EOF
  [ -n "$_cfa_first" ] && { printf '%s' "$_cfa_first"; return; }

  # ② Spotlight 关了 / 刚装还没索引:扫常见目录,仍按 bundle id 认
  #    🔴 用 find 自己匹配,别在 shell 里写裸通配符:zsh 下没命中会报错中断(bash 是原样传)
  while IFS= read -r r; do
    [ -n "$r" ] && [ -d "$r" ] || continue
    while IFS= read -r a; do
      _cfa_ok "$a" || continue
      if [ "$(_cfa_id "$a")" = "$_cfa_want" ]; then printf '%s' "$a"; return; fi
    done <<EOF2
$(find "$r" -maxdepth 1 -name '*.app' -print 2>/dev/null)
EOF2
  done <<EOF3
$(printf '%s' "${CARHER_APP_ROOTS:-/Applications:$HOME/Applications}" | tr ':' '\n')
EOF3

  # ③ 正在跑就从进程路径反推(装在任何地方都兜得住)
  while IFS= read -r a; do
    [ -n "$a" ] || continue
    _cfa_ok "$a" || continue
    if [ "$(_cfa_id "$a")" = "$_cfa_want" ]; then printf '%s' "$a"; return; fi
  done <<EOF4
$(ps -Ao comm= 2>/dev/null | sed -n 's#\(/.*\.app\)/Contents/MacOS/.*#\1#p' | sort -u)
EOF4
}

carher_find_claude_app() { carher_find_app_by_id "$CARHER_CLAUDE_BUNDLE_ID" ""; }
carher_find_codex_app()  { carher_find_app_by_id "$CARHER_CODEX_BUNDLE_ID" Codex; }
carher_find_cursor_app() { carher_find_app_by_id "$CARHER_CURSOR_BUNDLE_ID" Cursor; }
# <<< find_app_mac.sh <<<

find_tool_app() { "carher_find_${TOOL}_app"; }

# 「GUI 主程序在跑」:可执行文件本身在 <App>/Contents/MacOS/ 下,且命令行不带 .js 参数
# (Cursor 的 zk-delta 小代理是借 Cursor 二进制跑 sidecar.js 的常驻进程,不算 GUI,和引擎的判法一致)
app_running() {
  pgrep -fl "$1/Contents/MacOS/" 2>/dev/null | awk -v p="$1/Contents/MacOS/" '
    { sub(/^[0-9]+[ \t]+/, ""); if (index($0, p) == 1 && $0 !~ /\.js([ \t]|$)/) f = 1 } END { exit !f }'
}
quit_app() {  # quit_app <App 路径> <最多等几秒> ⇒ 0=已退出
  local app="$1" n="${2:-30}" i=0
  app_running "$app" || return 0
  # 走 AppleScript 正常退出(有没保存的东西 App 会自己问),不 kill
  osascript - "$app" >/dev/null 2>&1 <<'OSA' || true
on run argv
  tell application (item 1 of argv) to quit
end run
OSA
  while app_running "$app"; do
    i=$((i + 1)); [ "$i" -ge "$n" ] && return 1
    sleep 1
  done
  return 0
}

install_app_mac() {
  local src="" root="" dest="" team="" got_id="" ext="${APP_PKG##*.}" ts
  say "→ 没找到 ${APP_NAME},从公司镜像下载安装(几百 MB,首次会慢一点)..."
  dl "${MIRROR}/${APP_PKG}" "$W/app.${ext}" || die "✗ 下载 ${APP_NAME} 失败:${MIRROR}/${APP_PKG}
  也可以去 ${FALLBACK_PAGE} 手动下载安装 App 后,回网页重新生成命令再跑。"
  if [ "$ext" = dmg ]; then
    MNT="$W/mnt"; mkdir -p "$MNT"
    hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MNT" "$W/app.dmg" </dev/null >/dev/null \
      || { MNT=""; die "✗ 打开 ${APP_NAME} 安装包(dmg)失败,安装包可能没下完整。回网页重新生成命令再试。"; }
    src="$(find "$MNT" -maxdepth 1 -name '*.app' -print 2>/dev/null | head -n1)"
  else
    mkdir -p "$W/unz"
    ditto -x -k "$W/app.zip" "$W/unz" || die "✗ 解压 ${APP_NAME} 安装包失败,安装包可能没下完整。回网页重新生成命令再试。"
    src="$(find "$W/unz" -maxdepth 1 -name '*.app' -print 2>/dev/null | head -n1)"
  fi
  [ -n "$src" ] || die "✗ ${APP_NAME} 安装包里没找到 .app(镜像出问题了),把这一屏截图发给运维。"
  # 镜像里的包必须是官方原样:bundle id 对 + 苹果签名有效 + 签名团队是官方那家
  got_id="$(_cfa_id "$src" || true)"
  [ "$got_id" = "$APP_ID" ] || die "✗ ${APP_NAME} 安装包的 bundle id 不对(${got_id:-空}),拒绝安装。把这一屏截图发给运维。"
  codesign --verify --strict "$src" >/dev/null 2>&1 || die "✗ ${APP_NAME} 安装包签名校验没过,拒绝安装。把这一屏截图发给运维。"
  team="$(codesign -dv "$src" 2>&1 | sed -n 's/^TeamIdentifier=//p' | head -n1)"
  [ "$team" = "$APP_TEAM" ] || die "✗ ${APP_NAME} 安装包不是官方签名(团队 ${team:-空}),拒绝安装。把这一屏截图发给运维。"
  # 装到第一个能写的目录:/Applications 要管理员组,普通账号退到 ~/Applications(不要 sudo)
  while IFS= read -r root; do
    [ -n "$root" ] || continue
    case "$root" in "$HOME"/*) mkdir -p "$root" 2>/dev/null || true ;; esac
    if [ -d "$root" ] && [ -w "$root" ]; then dest="$root"; break; fi
  done <<EOF
$(printf '%s' "${CARHER_APP_DEST_ROOTS:-/Applications:$HOME/Applications}" | tr ':' '\n')
EOF
  [ -n "$dest" ] || die "✗ 没有能装 App 的目录(/Applications 和 ~/Applications 都写不进去)。"
  dest="$dest/$(basename "$src")"
  if [ -e "$dest" ]; then  # 同名但上面没认出来 = 坏的/空壳。改名留着,不删用户的东西
    ts="$(date +%Y%m%d-%H%M%S)"; mv "$dest" "$dest.bak-carher-$ts"
    warn "↳ 原来的 $dest 不完整,已改名为 $dest.bak-carher-$ts"
  fi
  ditto "$src" "$dest" || die "✗ 拷贝 ${APP_NAME} 到 $dest 失败。"
  xattr -dr com.apple.quarantine "$dest" 2>/dev/null || true
  if [ -n "$MNT" ]; then hdiutil detach -quiet "$MNT" >/dev/null 2>&1 || true; MNT=""; fi
  rm -f "$W/app.${ext}"; rm -rf "$W/unz"
  ok "✔ ${APP_NAME} 已装到 $dest"
}

# ════════════════════════════════════════════════════════════════════════
# install_one <tool> —— 装一个工具(第 2~5 步)。单工具时在 main 里直接调(exit 行为和以前逐字一致);
# tool=all 时每个工具在**自己的子 shell** 里调 ⇒ 里面的 die / exit 只结束那一个工具,另外两个照跑。
# 读 $KEY(上面换来的那一把,三个工具共用)、$W、$OS;写 $TOOL / $APP / $RC 等局部名。
# ════════════════════════════════════════════════════════════════════════
install_one() {
TOOL="$1"
# 每个工具的 App:显示名 / bundle id / 镜像文件 / 苹果签名团队号(本机三个正版 App `codesign -dv` 实测)
case "$TOOL" in
  cursor) TOOL_LABEL=Cursor;      APP_NAME=Cursor;          APP_ID="$CARHER_CURSOR_BUNDLE_ID"; APP_PKG="apps/Cursor-mac.dmg";    APP_TEAM=VDXQ22DGB9 ;;
  codex)  TOOL_LABEL=Codex;       APP_NAME=Codex;           APP_ID="$CARHER_CODEX_BUNDLE_ID";  APP_PKG="apps/Codex-mac.dmg";     APP_TEAM=2DC432GLL2 ;;
  claude) TOOL_LABEL="Claude Code"; APP_NAME="Claude 桌面 App"; APP_ID="$CARHER_CLAUDE_BUNDLE_ID"; APP_PKG="desktop/Claude-mac.zip"; APP_TEAM=Q6L2SF6YDW ;;
esac

APP=""
# CARHER_SKIP_APP_INSTALL=1 ⇒ App 相关一概不碰(不找、不装、不退出、不重开)。
#   台架靠它保证:在开发机上跑测试时,mdfind 找到的是**真装着的** Codex/Claude/Cursor,不许去退出它们。
if [ "${CARHER_SKIP_APP_INSTALL:-}" = 1 ]; then
  warn "↳ 跳过 App 这一步(CARHER_SKIP_APP_INSTALL=1)"
elif [ "$OS" = darwin ]; then
  APP="$(find_tool_app || true)"
  if [ -n "$APP" ]; then ok "✔ 已装 ${APP_NAME}:$APP"
  else
    install_app_mac
    APP="$(find_tool_app || true)"
    [ -n "$APP" ] || die "✗ ${APP_NAME} 装完了却找不到(按 bundle id 找),把这一屏截图发给运维。"
  fi
elif [ "$TOOL" = cursor ]; then
  for c in /usr/share/cursor/cursor /opt/cursor/cursor "$(command -v cursor 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && { APP="$c"; break; }
  done
  [ -n "$APP" ] || die "✗ Linux 上没装 Cursor。先按 ${FALLBACK_PAGE} 第 1 步装好(.deb / .rpm,要 sudo)并打开一次,再回网页重新生成命令。"
else
  say "↳ Linux 上不自动装桌面 App(要 sudo 装 .deb);命令行照样配好。要 App 见 ${FALLBACK_PAGE}"
fi
echo

# ── 3. Cursor 专属:设置库要先有、Cursor 要先退出(引擎两条硬闸:没库 exit 3,开着 exit 2)──
if [ "$TOOL" = cursor ] && [ -n "$APP" ]; then
  if [ "$OS" = darwin ]; then CDB="$HOME/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
  else CDB="${XDG_CONFIG_HOME:-$HOME/.config}/Cursor/User/globalStorage/state.vscdb"; fi
  if [ ! -f "$CDB" ]; then
    [ "$OS" = darwin ] || die "✗ Cursor 还没打开过。先打开一次 Cursor(登录),完全退出后回网页重新生成命令。"
    say "→ Cursor 第一次用:先自动打开一次让它初始化(10~60 秒),好了会自动关掉 ..."
    open "$APP" || true
    i=0; while [ ! -f "$CDB" ] && [ "$i" -lt 120 ]; do sleep 1; i=$((i + 1)); done
    [ -f "$CDB" ] || die "✗ Cursor 打开 2 分钟还没初始化好。手动打开 Cursor 登录一次、完全退出后,回网页重新生成命令。"
    sleep 5   # 库文件刚出现时里面的设置还没写完
  fi
  if [ "$OS" = darwin ] && app_running "$APP"; then
    say "→ 先把 Cursor 退出(配置要在它关着时写;有没保存的文件 Cursor 会问你)..."
    quit_app "$APP" 60 || die "✗ Cursor 没退出(可能在等你保存文件)。保存后按 Cmd+Q 退出 Cursor,回网页重新生成命令再跑。"
    ok "✔ Cursor 已退出"
  fi
fi

# ── 4. 运行现有一键安装器 ────────────────────────────────────────────
say "→ 下载 ${TOOL} 一键安装器 ..."
case "$TOOL" in
  codex|claude)
    dl "${PORTAL}/i/pkg/${TOOL}-mac.command" "$W/inst.command" || die "✗ 下载安装器失败:${PORTAL}/i/pkg/${TOOL}-mac.command"
    bash -n "$W/inst.command" 2>/dev/null || die "✗ 安装器下载不完整(语法检查没过),回网页重新生成命令再试。"
    echo
    set +e
    ( export CARHER_KEY="$KEY"; exec bash "$W/inst.command" ) </dev/null 2>&1 | mask_stream
    RC="${PIPESTATUS[0]}"
    set -e
    ;;
  cursor)
    dl "${PORTAL}/i/pkg/cursor-mac.zip" "$W/cursor.zip" || die "✗ 下载安装器失败:${PORTAL}/i/pkg/cursor-mac.zip"
    mkdir -p "$W/cz"
    unzip -q -o "$W/cursor.zip" -d "$W/cz" </dev/null || die "✗ 安装器解压失败(没下完整?),回网页重新生成命令再试。"
    ENG="$W/cz/cursor-mac"
    [ -f "$ENG/cursor_team_setup.sh" ] && [ -f "$ENG/cursor_team_setup.js" ] || die "✗ 安装包里缺 cursor_team_setup(镜像出问题了),把这一屏截图发给运维。"
    echo
    set +e
    # 新引擎认 CARHER_KEY 环境变量(不进命令行,ps 看不到);老包只认 --key ⇒ 看包里的引擎认不认再决定
    if grep -q 'process.env.CARHER_KEY' "$ENG/cursor_team_setup.js"; then
      ( export CARHER_KEY="$KEY"; if [ "$OS" = darwin ] && [ -n "$APP" ]; then export CURSOR_APP="$APP"; fi
        exec sh "$ENG/cursor_team_setup.sh" --apply ) </dev/null 2>&1 | mask_stream
    else
      ( if [ "$OS" = darwin ] && [ -n "$APP" ]; then export CURSOR_APP="$APP"; fi
        exec sh "$ENG/cursor_team_setup.sh" --apply --key "$KEY" ) </dev/null 2>&1 | mask_stream
    fi
    RC="${PIPESTATUS[0]}"
    set -e
    ;;
esac
echo
unset KEY

# ── 5. App 退出再开 ──────────────────────────────────────────────────
if [ "$OS" = darwin ] && [ -n "$APP" ] && { [ "$RC" = 0 ] || [ "$RC" = 5 ]; }; then
  if app_running "$APP"; then
    say "→ 重启 ${APP_NAME} 让配置生效 ..."
    if quit_app "$APP" 30; then open "$APP" || true; ok "✔ ${APP_NAME} 已重新打开"
    else warn "⚠ ${APP_NAME} 没退出(可能在等你保存),请自己按 Cmd+Q 退出再打开。"; fi
  else
    open "$APP" || true; ok "✔ 已打开 ${APP_NAME}"
  fi
fi

if [ "$RC" = 0 ]; then
  if [ "$MULTI" = 1 ]; then ok "✔ ${TOOL_LABEL} 装好了。"; return 0; fi
  ok "✅ 全部完成。"
elif [ "$RC" = 5 ]; then
  warn "⚠ 基本装好了,但上面有标红的项(退出码 5)。把这一屏截图发给运维。"
  exit 5
else
  if [ "$MULTI" = 1 ]; then
    die "✗ ${TOOL_LABEL} 没装成:安装器退出码 ${RC}(原因见上面的报错)。" "$RC"
  fi
  die "✗ 安装器没跑完(退出码 ${RC})。这个安装码已经用掉了:看上面的报错处理后,回网页重新生成一条命令再跑。
  也可以用备用下载页:${FALLBACK_PAGE}" "$RC"
fi
}

# ════════════════════════════════════════════════════════════════════════
# 装一个 / 装三个
# ════════════════════════════════════════════════════════════════════════
if [ "$MULTI" != 1 ]; then
  install_one "$TOOL"
  exit 0
fi

# ── tool=all:依次装三个,每个各自 try;一个失败不影响另外两个 ──────────────
# 总体预估先说清楚:App 包是大头(每个几百 MB ~ 1GB),三个一起装慢网下要半小时以上。
say "   三个工具一起装:Codex → Claude Code → Cursor"
say "   要下 3 个 App 安装包(每个几百 MB,已装过的会跳过),整个过程大约 10~40 分钟,看网速。"
warn "   ⚠ 中途别关这个终端窗口。Cursor 放在最后装(它要求先完全退出,最可能需要你动手)。"
echo

TOTAL=0; for t in $ALL_TOOLS; do TOTAL=$((TOTAL + 1)); done
SUM_DIR="$W/summary"; mkdir -p "$SUM_DIR"
N=0
for t in $ALL_TOOLS; do
  N=$((N + 1))
  echo
  say "═══ 第 ${N}/${TOTAL} 个:${t} ═══"
  echo
  # 每个工具一个子 shell:里面的 die / exit / set -e 只结束这一个工具。
  # 失败原因写进 SUM_DIR/<tool>.err(子 shell 的变量传不回来),退出码写 .rc。
  # 🔴 子 shell 不继承父 shell 的 EXIT trap(bash 3.2 实测)⇒ dmg 要在子 shell 里自己卸,
  #    否则这个工具失败退出时挂载点留着,父 shell 的 trap 看到的 MNT 还是空的。
  #    只卸 dmg、不删 $WROOT:汇总文件还在里面,父 shell 的 trap 最后统一删。
  set +e
  ( ERR_SINK="$SUM_DIR/${t}.err"; W="$WROOT/${t}"; mkdir -p "$W"
    trap '[ -n "$MNT" ] && hdiutil detach -quiet "$MNT" >/dev/null 2>&1' EXIT
    install_one "$t" )
  printf '%s' "$?" > "$SUM_DIR/${t}.rc"
  set -e
done
unset KEY
echo

# ── 汇总 ────────────────────────────────────────────────────────────────
say "==================================================="
say "   安装结果"
say "==================================================="
NOK=0; NBAD=0; NWARN=0; FAILED=""
for t in $ALL_TOOLS; do
  case "$t" in codex) L=Codex ;; claude) L="Claude Code" ;; cursor) L=Cursor ;; *) L="$t" ;; esac
  TRC="$(cat "$SUM_DIR/${t}.rc" 2>/dev/null || echo 1)"
  if [ "$TRC" = 0 ]; then
    NOK=$((NOK + 1)); ok "  ✔ ${L}  装好了"
  elif [ "$TRC" = 5 ]; then
    NOK=$((NOK + 1)); NWARN=$((NWARN + 1)); warn "  ⚠ ${L}  装上了,但有标红的项(退出码 5)"
  else
    NBAD=$((NBAD + 1)); FAILED="${FAILED}${L}、"
    printf '%s  ✗ %s  没装成(退出码 %s)%s\n' "$RED" "$L" "$TRC" "$NC"
    # 失败原因原话(最后一条,就是 die 的那句)
    if [ -s "$SUM_DIR/${t}.err" ]; then
      printf '%s      原因:%s%s\n' "$RED" "$(tail -n 20 "$SUM_DIR/${t}.err" | tr '\n' ' ' | sed 's/  */ /g')" "$NC"
    fi
  fi
done
echo
if [ "$NBAD" = 0 ] && [ "$NWARN" = 0 ]; then
  ok "✅ 三个都装好了。"
  exit 0
fi
if [ "$NBAD" = 0 ]; then
  warn "⚠ 三个都装上了,但有标红的项(退出码 5)。把这一屏截图发给运维。"
  exit 5
fi
FAILED="${FAILED%、}"
if [ "$NOK" = 0 ]; then
  printf '%s✗ 三个都没装成(%s)。看上面每个的原因处理后,回网页 %s/ 重新生成一条命令再跑。%s\n' \
    "$RED" "$FAILED" "$PORTAL" "$NC" >&2
  printf '%s  也可以用备用下载页:%s%s\n' "$RED" "$FALLBACK_PAGE" "$NC" >&2
  exit 8
fi
printf '%s⚠ %s 个装好了,%s 没装成。%s\n' "$YELLOW" "$NOK" "$FAILED" "$NC" >&2
printf '%s  这个安装码已经用掉了 ⇒ 补装方法:回网页 %s/ ,在「选工具」里**单独选 %s**,复制那一行再跑一次。%s\n' \
  "$YELLOW" "$PORTAL" "$FAILED" "$NC" >&2
printf '%s  已经装好的不用再动(重复装也不会坏)。备用下载页:%s%s\n' "$YELLOW" "$FALLBACK_PAGE" "$NC" >&2
exit 7
}

main "$@"
