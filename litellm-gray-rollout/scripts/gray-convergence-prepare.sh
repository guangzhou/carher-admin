#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

lock_acquire
load_state
[[ "$STATE_PHASE" == "normal_gray" && "$STATE_FROZEN" == "0" ]] || die "convergence prepare requires normal_gray"
[[ "$STATE_SPLIT" == "100" ]] || die "convergence prepare requires split=100"
[[ -z "$(map_keys "$(active_dir)/force-prod.map")" ]] || die "incident force-prod must be empty"
# Convergence is the point of no easy return: after it, prod is taken offline and
# upgraded. The stability evidence below is only about a build config_checksum can
# name, so require the workload to be pinned before trusting any of it.
require_workload_binding "convergence prepare"
require_gate_evidence convergence_stable GRAY_GRAY_STABLE
require_gate_evidence convergence_control_plane GRAY_CONTROL_PLANE_OK
require_gate_evidence convergence_bridge GRAY_BRIDGE_READY
require_gate_evidence convergence_bypass_disposition GRAY_BYPASS_DISPOSITION_OK
stage_from_active
render_fragments "$STAGE_DIR" 1 "$STATE_SPLIT" off
write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" convergence_ready 1 "$STATE_SPLIT" off 1
commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
printf 'phase=convergence_ready mode=1\n'
