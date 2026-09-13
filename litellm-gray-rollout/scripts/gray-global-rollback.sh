#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

lock_acquire
load_state
case "$STATE_PHASE" in normal_gray|convergence_ready) ;; *) die "global rollback is illegal in phase=$STATE_PHASE" ;; esac
if [[ "${GRAY_DISPATCH_AUTHORIZED_ACTION:-}" == "rollback" ]]; then
  [[ -n "${GRAY_DISPATCH_EVIDENCE_FILE:-}" ]] || die "dispatcher authorization is missing evidence"
  require_dispatch_authorization "$GRAY_DISPATCH_EVIDENCE_FILE" rollback
else
  require_gate_evidence global_rollback GRAY_PROD_HEALTHY
fi
stage_from_active
while IFS= read -r key; do
  [[ -n "$key" ]] || continue
  remove_key_from_map "$STAGE_DIR/protected-prod.map" "$key"
  remove_key_from_map "$STAGE_DIR/force-prod.map" "$key"
  write_key_map "$STAGE_DIR/force-prod.map" "$key" "$STAGE_DIR/key-sid.map"
done < <(map_keys "$STAGE_DIR/force-gray.map")
empty_map "$STAGE_DIR/force-gray.map"
render_fragments "$STAGE_DIR" 0 0 off
write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" rolled_back 0 0 off 1
commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
printf 'phase=rolled_back route=prod\n'
