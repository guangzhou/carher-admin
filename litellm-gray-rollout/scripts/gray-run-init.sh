#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

usage() {
  printf 'Usage: %s [--run-id ID] --execution-plan FILE --live-summary FILE --expected-live-sha256 HEX --execution-plan-sha256 HEX\n' "$0"
}

RUN_ID=""
EXECUTION_PLAN=""
LIVE_SUMMARY=""
EXPECTED_LIVE_SHA=""
EXPECTED_PLAN_SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) RUN_ID="${2:?missing run id}"; shift 2 ;;
    --execution-plan) EXECUTION_PLAN="${2:?missing execution plan}"; shift 2 ;;
    --live-summary) LIVE_SUMMARY="${2:?missing live summary}"; shift 2 ;;
    --expected-live-sha256) EXPECTED_LIVE_SHA="${2:?missing live checksum}"; shift 2 ;;
    --execution-plan-sha256) EXPECTED_PLAN_SHA="${2:?missing execution plan checksum}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

lock_acquire
[[ ! -e "$GRAY_ACTIVE" && ! -L "$GRAY_ACTIVE" ]] || die "an active run already exists"
if ! is_test_mode; then
  [[ -n "$EXECUTION_PLAN" && -f "$EXECUTION_PLAN" ]] || die "execution plan is required outside test mode"
  [[ -n "$LIVE_SUMMARY" && -f "$LIVE_SUMMARY" ]] || die "live summary is required outside test mode"
  [[ "$EXPECTED_LIVE_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "--expected-live-sha256 is required outside test mode"
  [[ "$EXPECTED_PLAN_SHA" =~ ^[0-9a-fA-F]{64}$ ]] || die "--execution-plan-sha256 is required outside test mode"
  require_routing_init_environment
  require_renderer_environment
else
  [[ -z "$EXECUTION_PLAN" || -f "$EXECUTION_PLAN" ]] || die "execution plan not found"
  [[ -z "$LIVE_SUMMARY" || -f "$LIVE_SUMMARY" ]] || die "live summary not found"
fi
if [[ -n "$EXPECTED_LIVE_SHA" ]]; then
  [[ -n "$LIVE_SUMMARY" ]] || die "--expected-live-sha256 requires --live-summary"
  [[ "$(sha256_file "$LIVE_SUMMARY")" == "$EXPECTED_LIVE_SHA" ]] || die "live summary checksum drift"
fi
if [[ -n "$EXPECTED_PLAN_SHA" ]]; then
  [[ -n "$EXECUTION_PLAN" ]] || die "--execution-plan-sha256 requires --execution-plan"
  [[ "$(sha256_file "$EXECUTION_PLAN")" == "$EXPECTED_PLAN_SHA" ]] || die "execution plan checksum drift"
fi
if [[ -z "$RUN_ID" ]]; then
  RUN_ID="run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
fi
validate_scalar run_id "$RUN_ID"
if find "$GRAY_GENERATIONS" -mindepth 1 -maxdepth 1 -type d -exec grep -l "^run_id=$RUN_ID$" '{}/state.env' ';' 2>/dev/null | grep -q .; then
  die "run id already exists and cannot be reused"
fi

GENERATION="g000001"
GEN_DIR="$GRAY_GENERATIONS/$RUN_ID-$GENERATION"
umask 077
mkdir "$GEN_DIR"
chmod 700 "$GEN_DIR"
for file in protected-prod.map force-prod.map force-gray.map key-sid.map; do
  empty_map "$GEN_DIR/$file"
done
render_fragments "$GEN_DIR" 0 0 off
[[ -z "$EXECUTION_PLAN" ]] || cp -p "$EXECUTION_PLAN" "$GEN_DIR/execution-plan.snapshot"
[[ -z "$LIVE_SUMMARY" ]] || cp -p "$LIVE_SUMMARY" "$GEN_DIR/live-summary.snapshot"
[[ -z "$EXECUTION_PLAN" ]] || chmod 600 "$GEN_DIR/execution-plan.snapshot"
[[ -z "$LIVE_SUMMARY" ]] || chmod 600 "$GEN_DIR/live-summary.snapshot"
{
  if [[ -n "$EXECUTION_PLAN" ]]; then printf 'execution_plan=%s\n' "$(sha256_file "$EXECUTION_PLAN")"; fi
  if [[ -n "$LIVE_SUMMARY" ]]; then printf 'live_summary=%s\n' "$(sha256_file "$LIVE_SUMMARY")"; fi
  if ! is_test_mode; then
    renderer_manifest_lines
    routing_contract_manifest_lines
  fi
} >"$GEN_DIR/input-checksums.env"
if is_test_mode && [[ ! -s "$GEN_DIR/input-checksums.env" ]]; then
  printf 'test_mode=1\n' >"$GEN_DIR/input-checksums.env"
fi
chmod 600 "$GEN_DIR/input-checksums.env"
write_state "$GEN_DIR" "$RUN_ID" "$GENERATION" preflight 0 0 off 0
verify_frozen_routing_contract "$GEN_DIR"
verify_frozen_initial_rollback "$GEN_DIR"
render_candidate "$GEN_DIR" || { rm -rf "$GEN_DIR"; die "initial candidate render failed"; }
nginx_test || { rm -rf "$GEN_DIR"; die "initial nginx -t failed"; }
verify_render_attestation "$GEN_DIR" || { rm -rf "$GEN_DIR"; die "initial render output drifted during nginx -t"; }
write_transaction_journal initial "" "$GEN_DIR" switch_pending
switch_active "$GEN_DIR"
write_transaction_journal initial "" "$GEN_DIR" active_switched
if ! nginx_reload || ! post_reload_check; then
  if ! run_optional "${GRAY_INITIAL_ROLLBACK_CMD:-}"; then
    die "initial reload/worker check failed and live nginx rollback failed; active generation retained for emergency diagnosis"
  fi
  clear_transaction_journal
  rm -f "$GRAY_ACTIVE"
  rm -rf "$GEN_DIR"
  die "initial reload/worker check failed; previous live nginx config restored"
fi
write_transaction_journal initial "" "$GEN_DIR" reloaded
clear_transaction_journal
# shellcheck disable=SC2034 # consumed by _lib.sh's EXIT trap
GRAY_TXN_COMMITTED=1
printf 'run_id=%s\ngeneration=%s\nphase=preflight\n' "$RUN_ID" "$GENERATION"
