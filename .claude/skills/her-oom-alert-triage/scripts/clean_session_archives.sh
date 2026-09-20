#!/bin/bash
# 清理单个 her 实例的 session 老 archive
# 用法: clean_session_archives.sh <her_id>          # dry-run, 列出会删什么
#        clean_session_archives.sh <her_id> yes      # 真删
#
# 策略 (谨慎):
#   保留: 最近 2 份 *.reset.* (loadBeforeResetTranscript 可能需要最新归档)
#   删除: 7 天前的所有 *.reset.* / *.deleted.* / *.bak-*
#   不动: 当前 active *.jsonl
#
# 不重启 pod；清理是在线的。但如果 active session 还在被 hook 引用旧 archive，
# 可能需要后续 resize_her.sh 重启一次让进程释放任何 cache 引用。

set -u
NS=carher
HID="${1:-}"
DO="${2:-no}"
[ -z "$HID" ] && { echo "Usage: $0 <her_id> [yes]" >&2; exit 1; }

POD=$(kubectl get pod -n $NS --no-headers 2>/dev/null \
  | grep "^carher-${HID}-" | head -n1 | awk '{print $1}')
[ -z "$POD" ] && { echo "ERROR: no running pod for her-$HID"; exit 1; }

echo "===== clean session archives for her-$HID (DO=$DO) ====="
echo "pod=$POD"

LOG=${LOG:-/tmp/her-triage/her-${HID}-clean.log}
mkdir -p "$(dirname "$LOG")"

if [ "$DO" = "yes" ]; then
  ACTION="-delete"
  ACTION_LABEL="DELETE"
else
  ACTION=""
  ACTION_LABEL="DRY-RUN (will list only)"
fi

echo "ACTION: $ACTION_LABEL"
echo

kubectl exec -n $NS "$POD" -c carher --request-timeout=60s -- sh -c '
DIR=/data/.openclaw/agents/main/sessions
cd "$DIR" 2>/dev/null || exit 1

echo "--- before ---"
echo "all archive files:"
ls -1 2>/dev/null | grep -E "\.(reset|deleted)\.|\.bak-" | wc -l
echo "total archive size:"
ls -1 2>/dev/null | grep -E "\.(reset|deleted)\.|\.bak-" | xargs -I{} stat -c "%s" {} 2>/dev/null | awk "{s+=\$1} END{printf \"%.1f MB\\n\", s/1024/1024}"

echo
echo "--- 保留 (最近 2 份 .reset) ---"
KEEP=$(ls -1t 2>/dev/null | grep "\.reset\." | head -2)
echo "$KEEP"

echo
echo "--- 计划删除 (7 天前的 reset/deleted/bak, 排除最近 2 份 reset) ---"
CANDIDATES=$(find . -maxdepth 1 -type f \( -name "*.reset.*" -o -name "*.deleted.*" -o -name "*.bak-*" \) -mtime +7 2>/dev/null)
echo "$CANDIDATES" | while read F; do
  [ -z "$F" ] && continue
  BASENAME=$(basename "$F")
  if echo "$KEEP" | grep -qF "$BASENAME"; then
    echo "  SKIP (keep recent): $F"
    continue
  fi
  SZ=$(stat -c "%s" "$F" 2>/dev/null)
  SZH=$(awk -v s=$SZ "BEGIN{printf \"%.1fM\", s/1024/1024}")
  echo "  TO_DELETE: $SZH $F"
  if [ "'"$DO"'" = "yes" ]; then
    rm -fv "$F"
  fi
done

echo
echo "--- after ---"
echo "remaining archive files:"
ls -1 2>/dev/null | grep -E "\.(reset|deleted)\.|\.bak-" | wc -l
echo "remaining archive size:"
ls -1 2>/dev/null | grep -E "\.(reset|deleted)\.|\.bak-" | xargs -I{} stat -c "%s" {} 2>/dev/null | awk "{s+=\$1} END{printf \"%.1f MB\\n\", s/1024/1024}"
' 2>&1 | tee "$LOG"

if [ "$DO" != "yes" ]; then
  echo
  echo "(DRY-RUN) re-run with: $0 $HID yes"
fi
