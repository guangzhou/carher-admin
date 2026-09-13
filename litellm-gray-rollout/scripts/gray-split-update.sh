#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

PERCENT="${1:-}"
[[ "$PERCENT" =~ ^(0|1|5|10|50|100)$ ]] || die "split must be one of 0,1,5,10,50,100"
lock_acquire
load_state
[[ "$STATE_PHASE" == "normal_gray" && "$STATE_FROZEN" == "0" ]] || die "split is frozen in phase=$STATE_PHASE"
if [[ "$PERCENT" -gt 0 ]]; then
  require_gate_evidence split_sample GRAY_SAMPLE_OK
  # A ramp step claims the previous window was observed. Metrics files only
  # prove the cycles that ran; check-monitor-continuity.py proves none were
  # missed. Without it an unobserved window looks exactly like a healthy one.
  require_gate_evidence split_monitor_continuity GRAY_MONITOR_CONTINUITY_OK
fi
if [[ "$PERCENT" -ge 50 ]]; then
  require_gate_evidence split_capacity GRAY_CAPACITY_OK
  require_gate_evidence split_bridge GRAY_BRIDGE_READY
fi
if [[ "$PERCENT" == "$STATE_SPLIT" ]]; then
  printf 'split=%s unchanged\n' "$PERCENT"
  exit 0
fi
stage_from_active
render_fragments "$STAGE_DIR" "$STATE_MODE" "$PERCENT" "$STATE_BRIDGE"
write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" "$STATE_PHASE" "$STATE_MODE" "$PERCENT" "$STATE_BRIDGE" "$STATE_FROZEN"
commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
printf 'split=%s\n' "$PERCENT"
