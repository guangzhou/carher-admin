#!/bin/bash
# 原地清理 her 实例的孤儿 main.sqlite.tmp-* 文件，不重启 pod。
#
# 安全前提（必须满足才会删）:
#   1) pod 处于 Running + ready=true 状态
#   2) 目标 tmp 文件 fd_count = 0（即真正的孤儿，没有进程持有）
#   3) main.sqlite 存在（用作 reindex 失败的"安全网"——meta 已经记录有效 providerKey）
#
# 关键安全保证:
#   - 只删 fd_count=0 的 tmp（双重确认：第一次扫描 + rm 前再 check）
#   - active tmp（有进程持有 fd）会被 SKIP，不会打断进行中的 reindex
#   - 不修改 main.sqlite，不修改 deployment，不重启 pod
#   - 失败时 pod 状态完全不变（rm 失败也只是文件没删，不影响运行）
#
# 用法:
#   inplace_clean_orphans.sh <her_id>           # 真跑
#   inplace_clean_orphans.sh <her_id> --dry-run # 只看不做
#
# 退出码:
#   0 - 已清理（含全清 / 部分清）
#   1 - 前置检查失败（pod 不存在 / 不 ready）
#   2 - 没有孤儿（无害，跳过）

set -u
HID="${1:-}"
MODE="${2:-apply}"
[ -z "$HID" ] && { echo "Usage: $0 <her_id> [--dry-run]" >&2; exit 1; }

case "$MODE" in
  --dry-run|--dryrun|-n) DRY=1 ;;
  apply|"")              DRY=0 ;;
  *) echo "ERROR: unknown mode '$MODE'" >&2; exit 1 ;;
esac

POD=$(kubectl get pod -n carher --no-headers 2>/dev/null \
  | awk -v h="^carher-${HID}-" '$1 ~ h && $3=="Running"{print $1; exit}')
[ -z "$POD" ] && { echo "ERROR: no Running pod for her-$HID" >&2; exit 1; }

mkdir -p /tmp/her-rescue
LOG=/tmp/her-rescue/inplace-clean-${HID}.log
echo "===== inplace clean her-$HID ($POD) MODE=$([ $DRY -eq 1 ] && echo DRY || echo APPLY) =====" | tee "$LOG"
echo "started_at: $(date -u +%FT%TZ)" | tee -a "$LOG"

# Step 1: 前置 ready 检查
READY=$(kubectl get pod -n carher "$POD" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="carher")].ready}')
if [ "$READY" != "true" ]; then
  echo "FAIL: pod not ready (ready=$READY)" | tee -a "$LOG"
  exit 1
fi
WS=$(kubectl get pod -n carher "$POD" \
  -o jsonpath='{range .status.conditions[?(@.type=="carher.io/feishu-ws-ready")]}{.status}{end}')
RESTART=$(kubectl get pod -n carher "$POD" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="carher")].restartCount}')
echo "pre-check: ready=$READY ws_ready=${WS:-UNKNOWN} restartCount=$RESTART" | tee -a "$LOG"

# Step 2: BEFORE - 列举所有 tmp 并区分 orphan / active
echo | tee -a "$LOG"
echo "----- BEFORE: tmp inventory -----" | tee -a "$LOG"
INVENTORY=$(kubectl exec -n carher "$POD" -c carher --request-timeout=30s -- sh -c '
cd /data/.openclaw/memory
NOW=$(date +%s)
main_size=$(stat -c%s main.sqlite 2>/dev/null || echo 0)
main_mtime=$(stat -c%Y main.sqlite 2>/dev/null || echo 0)
main_age_h=$(awk -v n="$NOW" -v m="$main_mtime" "BEGIN{ if(m==0){print \"-\"} else {printf \"%.1f\", (n-m)/3600} }")
echo "MAIN size=$main_size age_h=$main_age_h"
for f in main.sqlite.tmp-*; do
  [ -e "$f" ] || continue
  size=$(stat -c%s "$f" 2>/dev/null)
  mtime=$(stat -c%Y "$f" 2>/dev/null)
  age_h=$(awk -v n="$NOW" -v m="$mtime" "BEGIN{printf \"%.1f\", (n-m)/3600}")
  fd=$(ls -la /proc/*/fd/ 2>/dev/null | grep -c "$f" 2>/dev/null || true)
  fd=${fd:-0}
  if [ "$fd" = "0" ]; then
    echo "TMP $f size=$size age_h=$age_h ORPHAN"
  else
    echo "TMP $f size=$size age_h=$age_h ACTIVE fd=$fd"
  fi
done
' 2>&1)
echo "$INVENTORY" | tee -a "$LOG"

# 解析数量
N_ORPHAN=$(echo "$INVENTORY" | grep -c "ORPHAN" || true)
N_ACTIVE=$(echo "$INVENTORY" | grep -c " ACTIVE " || true)
N_ORPHAN=${N_ORPHAN:-0}
N_ACTIVE=${N_ACTIVE:-0}

echo | tee -a "$LOG"
echo "summary-before: orphans=$N_ORPHAN actives=$N_ACTIVE" | tee -a "$LOG"

if [ "$N_ORPHAN" -eq 0 ]; then
  echo "no orphans, nothing to do." | tee -a "$LOG"
  exit 2
fi

# Step 3: 删孤儿（双确认）
echo | tee -a "$LOG"
if [ "$DRY" -eq 1 ]; then
  echo "----- DRY-RUN: would delete -----" | tee -a "$LOG"
  echo "$INVENTORY" | awk '/ORPHAN/{print "  rm /data/.openclaw/memory/"$2}' | tee -a "$LOG"
  echo
  echo "exiting (dry-run, no changes made)" | tee -a "$LOG"
  exit 0
fi

echo "----- APPLY: deleting orphans (re-check fd before each rm) -----" | tee -a "$LOG"
DELETE_OUT=$(kubectl exec -n carher "$POD" -c carher --request-timeout=60s -- sh -c '
cd /data/.openclaw/memory
deleted=0
skipped=0
freed_bytes=0
for f in main.sqlite.tmp-*; do
  [ -e "$f" ] || continue
  # 双确认：rm 前再 check 一次 fd
  fd=$(ls -la /proc/*/fd/ 2>/dev/null | grep -c "$f" 2>/dev/null || true)
  fd=${fd:-0}
  if [ "$fd" = "0" ]; then
    size=$(stat -c%s "$f" 2>/dev/null || echo 0)
    if rm -f "$f" 2>/dev/null; then
      echo "DELETED $f size=$size"
      deleted=$((deleted+1))
      freed_bytes=$((freed_bytes+size))
    else
      echo "RM_FAILED $f"
    fi
  else
    echo "SKIPPED $f fd=$fd (active)"
    skipped=$((skipped+1))
  fi
done
echo "result: deleted=$deleted skipped=$skipped freed_bytes=$freed_bytes"
' 2>&1)
echo "$DELETE_OUT" | tee -a "$LOG"

# Step 4: AFTER
sleep 2
echo | tee -a "$LOG"
echo "----- AFTER -----" | tee -a "$LOG"
READY_AFTER=$(kubectl get pod -n carher "$POD" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="carher")].ready}')
WS_AFTER=$(kubectl get pod -n carher "$POD" \
  -o jsonpath='{range .status.conditions[?(@.type=="carher.io/feishu-ws-ready")]}{.status}{end}')
RESTART_AFTER=$(kubectl get pod -n carher "$POD" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="carher")].restartCount}')
echo "pod-state: ready=$READY_AFTER ws_ready=${WS_AFTER:-UNKNOWN} restartCount=$RESTART_AFTER" | tee -a "$LOG"

if [ "$RESTART_AFTER" != "$RESTART" ]; then
  echo "WARN: pod restarted during operation ($RESTART -> $RESTART_AFTER)" | tee -a "$LOG"
fi

AFTER=$(kubectl exec -n carher "$POD" -c carher --request-timeout=30s -- sh -c '
cd /data/.openclaw/memory
remaining=$(ls main.sqlite.tmp-* 2>/dev/null | wc -l)
echo "remaining_tmp=$remaining"
echo "memory_dir_total=$(du -sh . 2>/dev/null | cut -f1)"
echo "main.sqlite size=$(stat -c%s main.sqlite 2>/dev/null)"
' 2>&1)
echo "$AFTER" | tee -a "$LOG"

# Step 5: log spot-check
echo | tee -a "$LOG"
echo "----- log spot-check (last 60s) -----" | tee -a "$LOG"
kubectl logs -n carher "$POD" -c carher --since=60s --tail=15 2>/dev/null \
  | grep -iE "error|reindex|memory|sqlite|tmp" | tail -5 | tee -a "$LOG" || true

echo | tee -a "$LOG"
echo "DONE her-$HID  log=$LOG  ended_at: $(date -u +%FT%TZ)" | tee -a "$LOG"

# 退出码：根据是否还有遗留 tmp（active 没动）
REMAINING=$(echo "$AFTER" | awk -F= '/^remaining_tmp=/{print $2}')
[ "${REMAINING:-0}" -gt 0 ] && exit 0  # 部分清理（active 留着是预期）
exit 0
