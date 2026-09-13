#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

ACTION="${1:-}"
case "$ACTION" in activate|verified|deactivate) ;; *) die "Usage: $0 activate|verified|deactivate" ;; esac
lock_acquire
load_state

case "$ACTION:$STATE_PHASE" in
  activate:preflight)
    require_gate_evidence bridge_activate GRAY_BRIDGE_READY
    target_phase=bridge_preparing; bridge=off; frozen=1
    ;;
  verified:bridge_preparing)
    require_gate_evidence bridge_verified GRAY_BRIDGE_READY
    target_phase=bridge_verified; bridge=guarded-old; frozen=1
    ;;
  deactivate:bridge_verified)
    require_gate_evidence bridge_deactivate GRAY_PROD_GUARDED_VERIFIED
    target_phase=preflight; bridge=off; frozen=0
    ;;
  *)
    die "bridge action $ACTION is illegal in phase=$STATE_PHASE"
    ;;
esac

stage_from_active
render_fragments "$STAGE_DIR" "$STATE_MODE" "$STATE_SPLIT" "$bridge"
write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" "$target_phase" "$STATE_MODE" "$STATE_SPLIT" "$bridge" "$frozen"
commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
printf 'bridge=%s phase=%s\n' "$bridge" "$target_phase"
