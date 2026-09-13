#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

ACTION="${1:-}"
case "$ACTION" in prepare|prod-restored|finish) ;; *) die "Usage: $0 prepare|prod-restored|finish" ;; esac
lock_acquire
load_state

case "$ACTION:$STATE_PHASE" in
  prepare:committed)
    require_gate_evidence post_commit_bridge GRAY_BRIDGE_READY
    target_phase=post_commit_bridge; bridge=guarded-old
    ;;
  prod-restored:post_commit_bridge)
    require_gate_evidence post_commit_prod_restored GRAY_PROD_GUARDED_VERIFIED
    target_phase=post_commit_prod_verified; bridge=guarded-old
    ;;
  finish:post_commit_prod_verified)
    require_gate_evidence post_commit_finish GRAY_PROD_HEALTHY
    target_phase=post_commit_rolled_back; bridge=off
    ;;
  *) die "post-commit rollback action $ACTION is illegal in phase=$STATE_PHASE" ;;
esac

stage_from_active
render_fragments "$STAGE_DIR" 0 0 "$bridge"
write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" "$target_phase" 0 0 "$bridge" 1
commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
printf 'phase=%s bridge=%s\n' "$target_phase" "$bridge"
