#!/bin/bash
# 一次性把 Phase A → Observe → Phase C 串起来跑给一个或多个 her id。
# 用法:
#   run_full_rescue.sh 54           # 单个
#   run_full_rescue.sh 40 73 8 67   # 多个串行
#
# 时间预估: 每个实例 ~8 分钟（A 90s + observe 5min + C 90s）

set -u
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -eq 0 ] && { echo "Usage: $0 <her_id> [<her_id> ...]" >&2; exit 1; }

mkdir -p /tmp/her-rescue
TS=$(date -u +%FT%TZ)
echo "===== full rescue start $TS targets=$* ====="

for HID in "$@"; do
  echo
  echo "################################################################"
  echo "  her-$HID  $(date -u +%FT%TZ)"
  echo "################################################################"
  "$SKILL_DIR/scripts/sop_phase_a.sh" "$HID" || { echo "phase-a failed for $HID"; continue; }
  "$SKILL_DIR/scripts/sop_observe.sh" "$HID" || { echo "observe failed for $HID"; continue; }
  "$SKILL_DIR/scripts/sop_phase_c.sh" "$HID" || { echo "phase-c failed for $HID"; continue; }
  echo "  her-$HID OK  $(date -u +%FT%TZ)"
done

echo
echo "===== full rescue done $(date -u +%FT%TZ) ====="
echo "logs in /tmp/her-rescue/her-<id>.log and /tmp/her-rescue/her-<id>-observe.log"
