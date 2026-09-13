#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

lock_acquire
load_state
verify_frozen_abort_verify "$(active_dir)"
case "$STATE_PHASE" in
  prod_offline_upgrading|prod_verified)
    if [[ "${GRAY_DISPATCH_AUTHORIZED_ACTION:-}" == "abort_to_bridge" ]]; then
      [[ -n "${GRAY_DISPATCH_EVIDENCE_FILE:-}" ]] || die "dispatcher authorization is missing evidence"
      require_dispatch_authorization "$GRAY_DISPATCH_EVIDENCE_FILE" abort_to_bridge
    else
      require_gate_evidence convergence_abort GRAY_BRIDGE_READY
    fi
    stage_from_active
    render_fragments "$STAGE_DIR" 1 "$STATE_SPLIT" guarded-old
    write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" aborting_to_bridge 1 "$STATE_SPLIT" guarded-old 1
    commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
    ;;
  aborting_to_bridge)
    ;;
  *) die "convergence abort is illegal in phase=$STATE_PHASE" ;;
esac

if [[ -z "${GRAY_ABORT_VERIFY_CMD:-}" ]]; then
  is_test_mode || die "GRAY_ABORT_VERIFY_CMD is required outside test mode"
else
  run_optional "$GRAY_ABORT_VERIFY_CMD"
fi
load_state
stage_from_active
render_fragments "$STAGE_DIR" 1 "$STATE_SPLIT" guarded-old
write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" aborted 1 "$STATE_SPLIT" guarded-old 1
commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
printf 'phase=aborted route=guarded-old\n'
