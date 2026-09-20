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
  # Before any gate evidence is even looked at: gate evidence is bound to
  # run_id + generation + config_checksum, and until the workload is pinned
  # config_checksum says nothing about which build is running. So evidence from a
  # pre-patch workload would satisfy a post-patch ramp and every ruler would read
  # green. Check the binding first, then the gates it makes meaningful.
  require_workload_binding "raising split above 0"
  require_gate_evidence split_sample GRAY_SAMPLE_OK
  # A ramp step claims the previous window was observed. Metrics files only
  # prove the cycles that ran; check-monitor-continuity.py proves none were
  # missed *and* that there were enough of them for the deepest stop-loss leg
  # (ABS_SUSTAIN_WINDOWS = 4) to be able to fire. Without the first, an
  # unobserved window looks exactly like a healthy one; without the second, a
  # 9-minute dwell looks exactly like a stop-loss that fired and found nothing
  # -- which is what 5% / 10% / 50% actually did on 2026-09-14.
  require_gate_evidence split_monitor_continuity GRAY_MONITOR_CONTINUITY_OK
fi
if [[ "$PERCENT" -ge 50 ]]; then
  # Produced by check-split-capacity.py, which judges in upstream-SECONDS per
  # ready container, not request count. The two disagree: on 2026-09-18 the canary
  # lane carried 21.4% of the requests and 44.9% of the work, because the class
  # mix differs between pools. Until 2026-09-20 this gate had no producer at all
  # and the evidence file was hand-written -- shape validated, truth never.
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
