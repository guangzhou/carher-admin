#!/bin/sh
# bundle_audit_daily.sh —— 每日自动巡检:Cursor 出新版就立刻跑完四腿台架并告警。
#
# 为什么要这个:AST 定位把「每版重新审」从**改代码**降成**跑一遍台架**,但没有消灭它。
# 真实的坏形状是「同事升级了 Cursor → 补丁全掉 → 三天后才有人反馈模型不见了」。
# 这个巡检把发现时间从「同事反馈」提前到「官方放版本当天」。
#
# 逻辑(便宜 + 吵):
#   1. 问官方 stable 元数据拿版本号。~1KB 请求,每天一次。
#   2. 和状态文件里「上次审过的版本」比。**没变就直接退出,不下 dmg、不出声。**
#   3. 变了 → 跑 `bundle_patch_regress.sh --fetch`(它自己下 dmg 抽两条 bundle 跑全四腿)。
#   4. 无论绿红都告警(绿=一条"新版已验过,可以重打包";红=贴失败项)。
#      绿也要出声:否则"没告警"到底是"没新版"还是"巡检自己挂了"分不出来。
#   5. 只有**全绿**才把版本号写进状态文件。红的话下次还会重跑同一版。
#
# 用法:
#   sh bundle_audit_daily.sh              # 巡检一次(版本没变则静默退出 0)
#   sh bundle_audit_daily.sh --force      # 忽略状态文件,强跑一遍
#   CX_AUDIT_STATE=/path sh ...           # 换状态/日志目录(默认 ~/.cursor-bundle-audit)
#   FEISHU_WEBHOOK=https://... sh ...     # 额外推飞书;**不设就只发本机通知**
#
# 交付物纪律:本文件不含任何硬编码 webhook / token,飞书地址只从环境变量读。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${CX_AUDIT_STATE:-$HOME/.cursor-bundle-audit}"
STATE="$STATE_DIR/last_audited_version"
LOG_DIR="$STATE_DIR/logs"
mkdir -p "$LOG_DIR"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/$STAMP.log"

# 告警:本机通知(一定发)+ 飞书(仅当 FEISHU_WEBHOOK 已设且不是 stub)
notify() {
  _title="$1"; _body="$2"
  echo "[notify] $_title — $_body"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$_body\" with title \"$_title\"" >/dev/null 2>&1 || true
  fi
  case "${FEISHU_WEBHOOK:-}" in
    https://*)
      # msg_type:text,超时 10s,失败不影响退出码(告警通道坏了不该把巡检结果吃掉)
      python3 - "$FEISHU_WEBHOOK" "$_title" "$_body" <<'PY' 2>&1 | sed 's/^/  [feishu] /' || true
import json, sys, urllib.request
url, title, body = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.dumps({"msg_type": "text", "content": {"text": title + "\n" + body}}).encode()
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=10)
    payload = json.loads(r.read().decode() or "{}")
    # 飞书 HTTP 200 + body code!=0 = 没送到,必须读 body 才算判据
    print("sent" if payload.get("code") == 0 else "NOT DELIVERED: %s" % payload)
except Exception as e:
    print("send failed: %s" % e)
PY
      ;;
  esac
}

META="$(curl -fsS --max-time 30 https://api2.cursor.sh/updates/api/download/stable/darwin-arm64/cursor 2>/dev/null)" || META=""
VER="$(printf '%s' "$META" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
if [ -z "$VER" ]; then
  # 取不到版本要出声:静默失败会让巡检变成"永远绿"的装饰品
  notify "Cursor 补丁巡检:拿不到版本号" "官方更新元数据请求失败(网络/接口变了)。巡检这次什么都没验。"
  exit 1
fi

LAST="$(cat "$STATE" 2>/dev/null || echo '')"
if [ "$FORCE" = "0" ] && [ "$VER" = "$LAST" ]; then
  echo "$(date '+%F %T')  最新 stable=$VER,与上次审过的一致 → 不下 dmg,静默退出" >>"$LOG_DIR/quiet.log"
  exit 0
fi

echo "=== $(date '+%F %T')  官方最新 stable=$VER(上次审过=${LAST:-<无>})→ 跑四腿 ===" | tee "$LOG"
sh "$HERE/bundle_patch_regress.sh" --fetch >>"$LOG" 2>&1
RC=$?

# ⚠️ 两个踩过的坑,别"简化"回去:
#  1. BSD sed 的 BRE 不认 `\(A\|B\)` 交替 → `sed -n 's/^  \(FAIL\|SKIP\)  /…/p'` 在 mac 上
#     **静默匹配不到**,告警正文里那几行失败项会整段消失(只剩空行)。改用 grep -E 挑行 + sed 去前缀。
#  2. `grep -c` 没命中时会**打印 0 并且退出码 1** → `$(grep -c … || echo 0)` 会吐出两行 "0\n0",
#     告警里出现 "FAIL=0\n0"。所以用 wc -l 数,它没命中也只给一个 0、退出码 0。
SUMMARY="$(grep -E '^  (FAIL|SKIP)  ' "$LOG" | sed 's/^  //' | head -12)"
countline() { grep -c "$1" "$LOG" 2>/dev/null | head -1 | tr -d ' \n'; }
PASSN="$(countline '^  PASS  ')"
FAILN="$(countline '^  FAIL  ')"
SKIPN="$(countline '^  SKIP  ')"

if [ "$RC" = "0" ]; then
  echo "$VER" >"$STATE"
  # ⚠️ `$SKIPN。` 不许写成裸的:UTF-8 locale 下 bash 会把「。」当成标识符的一部分吞进变量名,
  # set -u 直接报 "SKIPN。: unbound variable" 把告警整条打掉(实测踩过)。变量紧跟中文标点必须加 {}。
  notify "Cursor $VER 补丁台架全绿" "PASS=${PASSN} SKIP=${SKIPN}。下一步:VERIFIED_VERSIONS 加大版本 → sh package_team_setup.sh → 换飞书文档附件。日志 $LOG"
else
  # 红的时候**不写**状态文件 → 明天还会重跑同一版,不会因为"审过了"而静音
  notify "🔴 Cursor $VER 补丁台架失败" "FAIL=$FAILN PASS=$PASSN SKIP=$SKIPN
$SUMMARY
完整日志 $LOG"
fi
exit "$RC"
