#!/bin/sh
# cursor_selfheal.sh —— 补丁被 Cursor 自动升级冲掉后,**在 Cursor 关着的时候**自己补回来。
#
# 为什么要有它(2026-10-28 定住的因果链):
#   Cursor 自动升级会整个换掉 app 目录 ⇒ 我们的 bundle 补丁全没了,而且不报错。
#   其中件C 没了 ⇒ 压缩触发线 = min(300-10000, 300*0.9) = -9700 ⇒ **每条消息都压缩一次**,
#   每次 40~100 秒。同事只觉得「Cursor 比 Claude 慢」,不会知道要去双击 REPAIR。
#   (我们**不是**要关掉压缩 —— 补丁在时 Cursor 只在快满时才压,那是对的。)
#
# 怎么做到安全:
#   - 打补丁必须先退出 Cursor。本脚本**从不退出 Cursor、从不碰开着的 Cursor**:
#     先自己判一次「Cursor 在跑 ⇒ 直接走人」,再交给 cursor_team_setup.js --repair,
#     而 --repair 自己还有第二道闸(Cursor 在跑就 exit 2 拒绝动手)。两道闸都是现成的。
#   - 补丁都在 ⇒ 什么都不做(绝大多数时候就是这个分支,开销是读几个文件)。
#   - 只在「刚装过的那台机器」上跑:路径都写死成安装器拷过去的那份。
#
# 由 launchd 每 5 分钟拉一次(com.carher.cursor-selfheal.plist,安装器生成)。
# 日志:~/Library/Logs/carher-cursor-selfheal.log(只记「做了事」的那几次,不刷屏)。
set -u
SETUP="$HOME/.carher/cursor-setup/cursor_team_setup.js"
APP="${CURSOR_APP:-/Applications/Cursor.app}"
BIN="$APP/Contents/MacOS/Cursor"
RES="$APP/Contents/Resources/app"
LOG="$HOME/Library/Logs/carher-cursor-selfheal.log"
STAMP="$HOME/.carher/cursor-selfheal.last"

log() { mkdir -p "$(dirname "$LOG")"; printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

[ -x "$BIN" ] || exit 0          # 没装 Cursor:没事可做
[ -f "$SETUP" ] || exit 0        # 没跑过我们的安装器:不是我们的机器,不动

# ── 第一道闸:Cursor 在跑就走人 ──
# 与 cursor_team_setup.js 的 cursorRunning() 同一判据,**逐条照搬,别自己发明**:
#   ① 命令行**以** Cursor 二进制**开头**(只是参数里提到 Cursor 路径的 zsh -c "…" 不算)
#   ② 且不带 .js 参数(带 .js = node 模式:zk-delta 小代理 / 安装器自己,不是 GUI)
# 小代理是 7×24 常驻的 Cursor 二进制进程 —— 判据写宽一点就会**永远**以为 Cursor 开着,
# 自愈永远不触发;写窄一点就会在 Cursor 开着时动手。所以必须和安装器那份字字一致。
# 判的是 **$APP 这一个** Cursor(默认 /Applications/Cursor.app)。这样对副本做隔离测试时
# 量的是副本有没有在跑,不会被本机开着的那个 Cursor 干扰 —— 反过来也不会误伤真机。
gui_running() {
  pgrep -fl "$BIN" 2>/dev/null | while IFS= read -r line; do
    pid=${line%% *}; cmd=${line#* }
    [ "$pid" = "$$" ] && continue
    case "$cmd" in "$BIN"|"$BIN "*) ;; *) continue ;; esac   # ① 命令行以**这个** Cursor 二进制开头
    case "$cmd" in *.js|*.js\ *) continue ;; esac        # ② 带 .js 参数 = node 模式,不是 GUI
    echo "$pid"; break
  done | grep -q .
}
gui_running && exit 0

# ── 补丁在不在:数 marker。与安装器/DOCTOR 同一套判据 ──
has() { [ -f "$1" ] && grep -q "$2" "$1" 2>/dev/null; }
need=0
has "$RES/out/vs/workbench/workbench.desktop.main.js" '/\*@cxteam-byokforce\*/' || need=1
# 件C:只查「这份 bundle 里确实有摘要阈值」的那几个(3.28.5 起 agent-host 那份上游挪走了)
for f in extensions/cursor-agent-exec/dist/main.js extensions/cursor-local-agent-runtime/dist/main.js \
         extensions/cursor-agent-host/dist/main.js extensions/cursor-agent-host/dist/agent-host-daemon/dist/bin/daemon.cjs; do
  p="$RES/$f"; [ -f "$p" ] || continue
  grep -q 'unusedTokensThresholdTo\(Start\|Persist\)BackgroundSummarization' "$p" 2>/dev/null || continue
  grep -q '@cx-ctxwin:v3' "$p" 2>/dev/null || need=1
done
[ "$need" = 1 ] || exit 0

# ── 防抖:同一个「Cursor 版本 × 安装器版本」修失败过,1 小时内不再重试 ──
# 🔴 键里必须带**安装器自己的指纹**,不能只看 Cursor 版本:管理员出了新锚点、同事双击
#    UPGRADE 换了安装器 ⇒ 同一个 Cursor 版本此刻**已经修得好了**,只按 Cursor 版本防抖
#    会让它白等一小时(台架 ㉒ 就是被上一腿留下的旧防抖记录挡住才发现的)。
ver=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$RES/package.json" 2>/dev/null | head -1)
who=$(cksum < "$SETUP" 2>/dev/null | cut -d' ' -f1)
key="$ver@$who"
if [ -f "$STAMP" ]; then
  read -r last_key last_ts last_rc < "$STAMP" 2>/dev/null || true
  now=$(date +%s)
  if [ "${last_key:-}" = "$key" ] && [ "${last_rc:-0}" != 0 ] && [ $((now - ${last_ts:-0})) -lt 3600 ]; then exit 0; fi
fi

log "检测到补丁缺失(Cursor $ver,多半是自动升级冲掉的),Cursor 已关闭 ⇒ 自动修复"
out=$(ELECTRON_RUN_AS_NODE=1 "$BIN" "$SETUP" --repair 2>&1); rc=$?
printf '%s %s %s\n' "$key" "$(date +%s)" "$rc" > "$STAMP"
case "$rc" in
  0) log "✅ 修好了。下次打开 Cursor 就不会每条消息都压缩。" ;;
  2) log "Cursor 在修的那一刻被打开了 ⇒ repair 拒绝动手(正常,下一轮再试)" ;;
  5) log "⚠️ 部分修好:有文件的锚点认不出(Cursor $ver 太新)。需要管理员出新锚点。"
     printf '%s\n' "$out" | grep -E '❌|·|件C|锚点' | head -6 | while IFS= read -r l; do log "   $l"; done ;;
  *) log "⚠️ 修复失败 rc=$rc,一小时后再试"; printf '%s\n' "$out" | tail -4 | while IFS= read -r l; do log "   $l"; done ;;
esac
exit 0
