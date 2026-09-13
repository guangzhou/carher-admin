#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

lock_acquire
load_state
[[ "$STATE_PHASE" == "prod_verified" ]] || die "convergence commit requires prod_verified"
[[ -z "$(map_keys "$(active_dir)/force-prod.map")" ]] || die "incident force-prod must be empty"
require_gate_evidence convergence_commit GRAY_PROD_HEALTHY
stage_from_active
archive="$GRAY_ROOT/force-gray-archive-${STATE_RUN_ID}-${STATE_GENERATION}-$(date -u +%Y%m%dT%H%M%SZ)-$$.sids"
umask 077
set -o noclobber
map_keys "$STAGE_DIR/force-gray.map" | while IFS= read -r key; do [[ -n "$key" ]] && printf '%s\n' "$(sid_for_key "$key")"; done >"$archive"
set +o noclobber
chmod 600 "$archive"
empty_map "$STAGE_DIR/force-gray.map"
render_fragments "$STAGE_DIR" 0 0 off
write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" committed 0 0 off 1
commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
printf 'phase=committed route=prod\n'
