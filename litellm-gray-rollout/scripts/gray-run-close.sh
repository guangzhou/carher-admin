#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

lock_acquire
load_state
case "$STATE_PHASE" in committed|rolled_back|aborted|post_commit_rolled_back) ;; *) die "only a terminal run can be closed" ;; esac
require_gate_evidence run_close GRAY_RUN_CLOSE_OK
archive="$GRAY_ROOT/closed-${STATE_RUN_ID}"
[[ ! -e "$archive" && ! -L "$archive" ]] || die "closed run archive already exists"
ln -s "$(active_dir)" "$archive"
rm -f "$GRAY_ACTIVE"
# shellcheck disable=SC2034 # consumed by _lib.sh's EXIT trap
GRAY_TXN_COMMITTED=1
printf 'run_id=%s phase=%s archived=%s\n' "$STATE_RUN_ID" "$STATE_PHASE" "$archive"
