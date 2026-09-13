#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

case "${1:-}" in
  current)
    load_state
    printf 'run_id=%s\ngeneration=%s\nphase=%s\nmode=%s\nsplit=%s\nbridge=%s\nrouting_frozen=%s\n' \
      "$STATE_RUN_ID" "$STATE_GENERATION" "$STATE_PHASE" "$STATE_MODE" "$STATE_SPLIT" "$STATE_BRIDGE" "$STATE_FROZEN"
    ;;
  verify)
    load_state
    printf 'phase=%s checksum=ok\n' "$STATE_PHASE"
    ;;
  set)
    target="${2:-}"
    [[ -n "$target" ]] || die "missing target phase"
    validate_scalar phase "$target"
    lock_acquire
    load_state
    if [[ "$target" == "$STATE_PHASE" ]]; then
      printf 'phase=%s unchanged\n' "$target"
      exit 0
    fi
    case "$target" in
      normal_gray|prod_offline_upgrading|prod_verified) ;;
      *) die "phase transition to $target has a dedicated transaction entrypoint" ;;
    esac
    phase_allowed "$STATE_PHASE" "$target" || die "illegal phase transition: $STATE_PHASE -> $target"
    if [[ "$STATE_PHASE:$target" == "convergence_ready:prod_offline_upgrading" ]]; then
      require_gate_evidence prod_zero GRAY_PROD_ZERO
    elif ! is_test_mode; then
      case "$STATE_PHASE:$target" in
        preflight:normal_gray) require_gate_evidence gray_entry "normal_gray" ;;
        prod_offline_upgrading:prod_verified) require_gate_evidence prod_verified "prod_verified" ;;
      esac
    fi
    stage_from_active
    set_phase_in_dir "$STAGE_DIR" "$target"
    commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"
    printf 'phase=%s\n' "$target"
    ;;
  -h|--help|"")
    printf 'Usage: %s current|verify|set PHASE\n' "$0"
    [[ -n "${1:-}" ]] || exit 2
    ;;
  *) die "unknown command: $1" ;;
esac
