#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

INPUT="-"
if [[ "${1:-}" == "--input" ]]; then
  INPUT="${2:?missing input path}"
  shift 2
fi
[[ $# -eq 0 ]] || die2 "Usage: $0 [--input FILE|-]"

if [[ "$INPUT" == "-" ]]; then
  JSON_INPUT="$(cat)"
  EVIDENCE_FILE=""
else
  JSON_INPUT=""
  EVIDENCE_FILE="$INPUT"
fi

load_state
if ! is_test_mode; then
  [[ -n "$EVIDENCE_FILE" ]] || die2 "dispatcher evidence must be a frozen file outside test mode"
  require_metrics_evidence "$EVIDENCE_FILE"
  JSON_INPUT="$(cat "$EVIDENCE_FILE")"
elif [[ -n "$EVIDENCE_FILE" ]]; then
  [[ -f "$EVIDENCE_FILE" ]] || die2 "dispatcher input file not found"
  JSON_INPUT="$(cat "$EVIDENCE_FILE")"
fi

if ! parsed="$(printf '%s' "$JSON_INPUT" | python3 -c '
import json
import sys

try:
    obj = json.load(sys.stdin)
    rec = obj["dispatcher_recommendation"]
    action = rec["action"]
    reasons = rec["reason_codes"]
    hard = rec["hard_trigger"]
    if action not in {"none", "rollback", "hold_gray", "abort_to_bridge", "alert_only"}:
        raise ValueError("unknown action")
    if not isinstance(reasons, list) or not all(isinstance(v, str) and v for v in reasons):
        raise ValueError("invalid reason_codes")
    if not isinstance(hard, bool):
        raise ValueError("invalid hard_trigger")
    if action in {"rollback", "abort_to_bridge"} and not hard:
        raise ValueError("mutating action without hard trigger")
    print(action)
except Exception as exc:
    print(f"invalid dispatcher input: {exc}", file=sys.stderr)
    raise SystemExit(2)
')"; then
  exit 2
fi

case "$parsed" in
  none)
    printf 'action=none phase=%s\n' "$STATE_PHASE"
    ;;
  alert_only)
    printf 'action=alert_only phase=%s mutation=none\n' "$STATE_PHASE"
    ;;
  rollback)
    case "$STATE_PHASE" in normal_gray|convergence_ready) ;; *) die2 "rollback recommendation is illegal in phase=$STATE_PHASE" ;; esac
    if is_test_mode && [[ -z "$EVIDENCE_FILE" ]]; then
      "$SCRIPT_DIR/gray-global-rollback.sh"
    else
      GRAY_DISPATCH_AUTHORIZED_ACTION=rollback \
        GRAY_DISPATCH_EVIDENCE_FILE="$EVIDENCE_FILE" \
        "$SCRIPT_DIR/gray-global-rollback.sh"
    fi
    ;;
  hold_gray)
    case "$STATE_PHASE" in prod_offline_upgrading|prod_verified) ;; *) die2 "hold_gray recommendation is illegal in phase=$STATE_PHASE" ;; esac
    if is_test_mode; then
      [[ "${GRAY_GRAY_HEALTHY:-0}" == "1" ]] || die "gray is not healthy enough to hold"
    fi
    printf 'action=hold_gray phase=%s mutation=none\n' "$STATE_PHASE"
    ;;
  abort_to_bridge)
    case "$STATE_PHASE" in prod_offline_upgrading|prod_verified) ;; *) die2 "abort recommendation is illegal in phase=$STATE_PHASE" ;; esac
    if is_test_mode; then
      [[ "${GRAY_GRAY_HEALTHY:-1}" == "0" ]] || die2 "abort requested while gray is reported healthy"
    fi
    if is_test_mode && [[ -z "$EVIDENCE_FILE" ]]; then
      "$SCRIPT_DIR/gray-convergence-abort.sh"
    else
      GRAY_DISPATCH_AUTHORIZED_ACTION=abort_to_bridge \
        GRAY_DISPATCH_EVIDENCE_FILE="$EVIDENCE_FILE" \
        "$SCRIPT_DIR/gray-convergence-abort.sh"
    fi
    ;;
  *) die2 "unknown dispatcher action" ;;
esac
