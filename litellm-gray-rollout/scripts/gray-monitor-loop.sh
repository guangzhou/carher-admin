#!/usr/bin/env bash
# Run the documented monitor orchestration chain on a fixed interval.
#
# This exists because `split_monitor_continuity` demands a heartbeat ledger with
# no gap longer than the approved interval, and that ledger can only be produced
# by actually observing production over a real window -- not by hand-writing the
# evidence.  Every cycle does what docs section 6.1 prescribes and nothing more:
# snapshot the live inputs, build a desensitised metrics input bound to the
# active state, then hand it to gray-monitor-cycle.sh, which is the only thing
# allowed to reach the dispatcher.
#
# The loop has no rollback powers of its own.  It cannot call
# gray-global-rollback.sh, gray-convergence-abort.sh or gray-auto-dispatch.sh;
# it only feeds the chain that can.
#
# Footprint (nothing here deletes or overwrites anything outside these paths):
#   $RAW_DIR/access.log            0600, overwritten each cycle
#   $RAW_DIR/hard-errors.json      0600, overwritten each cycle
#   $RAW_DIR/backend-health.json   0600, overwritten each cycle
#   $RAW_DIR/readiness.json        0600, overwritten each cycle
#   $RUN_DIR/restart-baseline.json 0600, per-pod restartCount carried between cycles
#   $RUN_DIR/inputs/metrics-input-<UTC>.json  0600, one per cycle, never reused
#   $RUN_DIR/monitor-loop.log      0600, append-only
#   $EVIDENCE_DIR/monitor-heartbeat.jsonl  written by gray-monitor-cycle.sh, append-only
# To stop: kill the PID in $RUN_DIR/monitor-loop.pid, after confirming it with
# `ps -p`.  Never `pkill -f` -- that selector is wide enough to hit unrelated
# processes.  Rollback is simply "stop the loop": it changes no routing state.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_DIR="${GRAY_RUN_DIR:-/root/litellm-gray-run}"
RAW_DIR="$RUN_DIR/raw"
# GRAY_ROOT must be EXPORTED, not just resolved locally.  gray-monitor-cycle.sh
# and the dispatcher each source _lib.sh, which defaults GRAY_ROOT to
# /var/lib/litellm-gray-rollout -- a vestigial directory on 198 that has no
# `active` symlink.  Resolving the right root only in this shell let the cycle
# read the live generation while the dispatcher looked in the empty one and died
# with "invalid active generation", after metrics had already passed.  Exporting
# it means every script in the chain judges the same generation.
GRAY_ROOT_DIR="${GRAY_ROOT:-/etc/nginx/gray-route}"
export GRAY_ROOT="$GRAY_ROOT_DIR"
EVIDENCE_DIR="${GRAY_GATE_EVIDENCE_DIR:-$GRAY_ROOT_DIR/evidence}"
export GRAY_GATE_EVIDENCE_DIR="$EVIDENCE_DIR"
ACTIVE="$GRAY_ROOT_DIR/active"
LOG_FILE="$RUN_DIR/monitor-loop.log"
PID_FILE="$RUN_DIR/monitor-loop.pid"
RESTART_BASELINE="$RUN_DIR/restart-baseline.json"

BASELINE="${GRAY_BASELINE:-}"
SPEND_SOURCE="${GRAY_SPEND_SOURCE:-}"
# Spend reconciliation cannot be a file handed in once.  collect-metrics.py
# rejects a source older than MAX_LOG_AGE (5 min), and metrics.py *requires* the
# source at any split > 0 -- so a static --spend file makes every cycle after the
# first fail with a stale-evidence error, which reads exactly like a real fault.
# When these are set the loop regenerates the document itself each cycle by
# round-tripping real probes through the pool and matching x-litellm-call-id
# against SpendLogs.metadata->>'litellm_call_id'.
SPEND_COLLECTOR="${GRAY_SPEND_COLLECTOR:-}"
SPEND_PROBE_KEY_FILE="${GRAY_SPEND_PROBE_KEY_FILE:-}"
SPEND_PROBE_BASE_URL="${GRAY_SPEND_PROBE_BASE_URL:-http://127.0.0.1:30405}"
SPEND_PROBE_MODEL="${GRAY_SPEND_PROBE_MODEL:-}"
SPEND_PROBES="${GRAY_SPEND_PROBES:-3}"
# Ids awaiting a spend row, carried between cycles. Without it every cycle
# re-reports a row still inside LiteLLM's flush backlog as missing, which is a
# rollback trigger; the default keeps it beside the other per-run raw state.
SPEND_CARRY_FILE="${GRAY_SPEND_CARRY_FILE:-$RAW_DIR/spend-carry.json}"
INTERVAL="${GRAY_CYCLE_INTERVAL_SECONDS:-300}"
MAX_CYCLES="${GRAY_MAX_CYCLES:-0}"        # 0 = run until killed
ACCESS_LOG="${GRAY_ACCESS_LOG:-/var/log/nginx/cc-auto-link.gray.log}"
LATENCY_WINDOW_MINUTES="${GRAY_LATENCY_WINDOW_MINUTES:-30}"
NAMESPACE="${GRAY_NAMESPACE:-litellm-product}"
GRAY_SELECTOR="${GRAY_POD_SELECTOR:-app=litellm-proxy-gray}"
# Ready containers the gray lane must have, for metrics.py's readiness leg.
#
# No default on purpose.  A default would be a hard-coded fleet size that stops
# matching after any scale change and never goes red when it does -- the
# `llm-stab-scrape-down` failure, where a literal 5 sat in a rule for weeks.  It
# comes from the run sheet, and above split 0 metrics.py refuses a cycle without
# it, so forgetting it stops the ramp rather than silently disarming the leg.
EXPECTED_READY="${GRAY_EXPECTED_READY_CONTAINERS:-}"
# Enough log tail to cover the widest window the collector reads.  Measured
# 2026-09-18: ~20MB of this log is ~40 minutes, so 40MB covers the 30-minute
# latency window with room to spare.  Bounded on purpose -- the file grows, and
# copying all of it every cycle would scale with retention rather than window.
TAIL_BYTES="${GRAY_ACCESS_LOG_TAIL_BYTES:-40000000}"

usage() {
  printf 'Usage: %s --baseline FILE [--interval SECONDS] [--max-cycles N] [--spend FILE] [--expected-ready N] [--once]\n' "$0"
  printf '  --expected-ready N  ready containers the gray lane must have (run-sheet value,\n'
  printf '                      no default; required above split 0)\n'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval) INTERVAL="${2:?missing interval}"; shift 2 ;;
    --max-cycles) MAX_CYCLES="${2:?missing max cycles}"; shift 2 ;;
    --baseline) BASELINE="${2:?missing baseline}"; shift 2 ;;
    --spend) SPEND_SOURCE="${2:?missing spend source}"; shift 2 ;;
    --expected-ready) EXPECTED_READY="${2:?missing expected ready count}"; shift 2 ;;
    --once) MAX_CYCLES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ "$INTERVAL" =~ ^[0-9]+$ && "$INTERVAL" -ge 60 ]] || { printf 'interval must be an integer >= 60\n' >&2; exit 2; }
[[ "$MAX_CYCLES" =~ ^[0-9]+$ ]] || { printf 'max-cycles must be a non-negative integer\n' >&2; exit 2; }
[[ -n "$BASELINE" && -f "$BASELINE" ]] || { printf 'a frozen --baseline file is required\n' >&2; exit 2; }
# Validated here rather than at use: `printf '%d'` on a non-number aborts the
# cycle mid-envelope under set -e, which reads as "the monitor broke" instead of
# "you passed a bad flag".  1 is the floor because a lane expected to have zero
# ready containers is not a lane being ramped.
[[ -z "$EXPECTED_READY" || "$EXPECTED_READY" =~ ^[0-9]+$ && "$EXPECTED_READY" -ge 1 ]] \
  || { printf 'expected-ready must be an integer >= 1\n' >&2; exit 2; }

umask 077
mkdir -p "$RAW_DIR"
chmod 700 "$RUN_DIR" "$RAW_DIR"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG_FILE"; }

# Read the active state fresh every cycle rather than caching it.  A ramp step or
# a rollback changes split/generation underneath us, and a stale binding would
# produce evidence bound to a generation that is no longer on the wire --
# gray-monitor-cycle.sh would reject it, but only after the window was wasted.
state_get() { sed -n "s/^$1=//p" "$ACTIVE/state.env" | head -1; }

# The two Python helpers live in files rather than inline heredocs.  A heredoc
# inside a shell function becomes that command's stdin, so `printf ... | helper`
# silently delivers an empty stdin to Python -- the data goes nowhere and the
# only symptom is a JSON decode error on an empty string.  Writing them out once
# keeps stdin free for the actual payload.
HELPER_DIR="$RUN_DIR/.helpers"
INPUT_DIR="$RUN_DIR/inputs"
mkdir -p "$HELPER_DIR" "$INPUT_DIR"; chmod 700 "$HELPER_DIR" "$INPUT_DIR"

cat > "$HELPER_DIR/envelope.py" <<'PY'
"""Wrap stdin JSON in the envelope collect-metrics.py's load_source() requires.

Exactly {schema_version, source, captured_at, data, payload_sha256}, with the
checksum over the canonical form of `data` alone.
"""
import datetime, hashlib, json, sys

label, target = sys.argv[1:3]
data = json.load(sys.stdin)
canonical = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
envelope = {
    "schema_version": 1,
    "source": label,
    "captured_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds").replace("+00:00", "Z"),
    "data": data,
    "payload_sha256": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
}
with open(target, "w", encoding="utf-8") as handle:
    json.dump(envelope, handle, sort_keys=True)
PY
chmod 600 "$HELPER_DIR/envelope.py"

cat > "$HELPER_DIR/restart_delta.py" <<'PY'
"""Turn per-pod restartCount readings on stdin into a gray_pod_restart delta.

restartCount is per-pod and disappears when a pod is replaced, so the delta is
computed per pod name and clamped at zero: a rollout that swaps every pod reads
as no restarts rather than as a negative count, while a pod that genuinely
crash-loops still shows its increment.
"""
import json, os, sys

baseline_path = sys.argv[1]
current = {}
for line in sys.stdin.read().splitlines():
    name, _, count = line.partition("=")
    name = name.strip()
    if name and count:
        try:
            current[name] = int(count)
        except ValueError:
            pass

previous = {}
if os.path.exists(baseline_path):
    try:
        with open(baseline_path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            previous = {k: v for k, v in loaded.items() if isinstance(v, int)}
    except (OSError, ValueError):
        previous = {}

# Only a pod seen in the previous cycle can contribute a delta.  A pod that is
# new this cycle carries whatever restartCount it already had from before we were
# watching, and counting that would attribute an older fault to this window.
delta = sum(max(0, count - previous[name])
            for name, count in current.items() if name in previous)

with open(baseline_path, "w", encoding="utf-8") as handle:
    json.dump(current, handle, sort_keys=True)
os.chmod(baseline_path, 0o600)
json.dump({"gray_pod_restart": delta}, sys.stdout, sort_keys=True)
PY
chmod 600 "$HELPER_DIR/restart_delta.py"

envelope() {
  python3 "$HELPER_DIR/envelope.py" "$1" "$2"
  chmod 600 "$2"
}

# Probe each lane's own liveness endpoint.  A probe that cannot answer reads as
# false, which is the fail-safe direction: metrics.py refuses to roll traffic
# back onto a prod it cannot confirm healthy, so a probe failure can never
# become a reason to move traffic somewhere unverified.
collect_backend_health() {
  local port code out=()
  for port in 30405 30402 30406; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
      "http://127.0.0.1:$port/health/liveliness" || printf '000')"
    [[ "$code" == "200" ]] && out+=("true") || out+=("false")
  done
  printf '{"gray":%s,"prod":%s,"bridge":%s}' "${out[0]}" "${out[1]}" "${out[2]}" \
    | envelope "curl /health/liveliness on 127.0.0.1:{30405,30402,30406}" \
               "$RAW_DIR/backend-health.json"
}

# Hard errors are observed faults, not sampled ratios -- metrics.py does not hold
# them behind the sustain gate, so one is enough to dispatch.  That makes the
# ruler's exactness the whole point, and it is why this reports only
# gray_pod_restart: restartCount is an exact counter read from the API server.
#
# Deliberately NOT reported: prisma_error, callback_import_error and the redis_*
# codes.  A log grep for those is unreliable here -- Python tracebacks in these
# pods are multi-line and not timestamp-prefixed, so a line-oriented count both
# under-counts one fault and re-counts an old one.  Reporting an unverified 0 for
# them would be a false negative wearing the shape of evidence; omitting the key
# says "not measured", which is the truth.  metrics.py accepts any subset of
# HARD_ERROR_CODES.
#
# restartCount is per-pod and resets when a pod is replaced, so the delta is
# computed per pod name and clamped at zero.  A rollout that swaps every pod
# therefore reads as no restarts rather than as a negative count, and a pod that
# genuinely crash-loops still shows its increment.
collect_hard_errors() {
  local current
  current="$(kubectl -n "$NAMESPACE" get pods -l "$GRAY_SELECTOR" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.status.containerStatuses[0].restartCount}{"\n"}{end}' \
    2>/dev/null || true)"
  if [[ -z "$current" ]]; then
    # No reading at all is not the same as "zero restarts".  Omit the key so the
    # cycle does not assert a fault it did not observe either way.
    printf '{}' | envelope "kubectl get pods -l $GRAY_SELECTOR (restartCount unavailable)" \
                           "$RAW_DIR/hard-errors.json"
    return 0
  fi
  printf '%s' "$current" \
    | python3 "$HELPER_DIR/restart_delta.py" "$RESTART_BASELINE" \
    > "$RAW_DIR/.hard-errors.data"
  envelope "kubectl get pods -l $GRAY_SELECTOR restartCount delta (per pod, clamped at 0)" \
           "$RAW_DIR/hard-errors.json" < "$RAW_DIR/.hard-errors.data"
  rm -f "$RAW_DIR/.hard-errors.data"
}

# Ready CONTAINERS, counted from `.status.containerStatuses[*].ready` -- never
# from replicas, and never from a Deployment's `Available` condition.  Both of
# those are spec-side numbers that read healthy for a lane with nothing running:
# a Deployment scaled to 0 still reports `Available: True`, and on the acct pool
# 165 deployments with replicas>0 had only 54 actually serving.
#
# An unreadable count omits the document entirely, so the cycle fails on a
# missing source rather than reporting 0 ready and dispatching a rollback on our
# own broken kubectl.
collect_readiness() {
  local split="$1" raw ready
  rm -f "$RAW_DIR/readiness.json"
  if [[ -z "$EXPECTED_READY" ]]; then
    # Above split 0 metrics.py turns the absent document into READINESS_MISSING
    # and the ramp stops, which is the intended shape: an unset expectation
    # disarms the leg, and a disarmed leg must not be able to move traffic.
    [[ "$split" -gt 0 ]] && log "FAIL readiness required at split=$split but --expected-ready is unset"
    return 0
  fi
  # kubectl's output is captured BEFORE counting.  `kubectl ... | grep -c` cannot
  # tell "no pods matched the selector" from "kubectl could not answer": grep
  # prints 0 for both, so a broken kubectl would report zero ready containers and
  # dispatch a rollback on our own tooling rather than on production.
  raw="$(kubectl -n "$NAMESPACE" get pods -l "$GRAY_SELECTOR" \
    -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.ready}{"\n"}{end}{end}' \
    2>/dev/null || true)"
  if [[ -z "$raw" ]]; then
    log "SKIP readiness unreadable (kubectl returned nothing for -l $GRAY_SELECTOR)"
    return 1
  fi
  # `grep -c` exits 1 on zero matches under `set -e`, and zero ready containers is
  # a real reading we must keep -- so the count is taken with awk, which exits 0
  # either way and cannot turn a genuine 0 into a skipped cycle.
  ready="$(printf '%s\n' "$raw" | awk '$0 == "true" { n++ } END { print n + 0 }')"
  printf '{"lane":"gray","ready_containers":%d,"expected_containers":%d,"observed_at":"%s"}' \
    "$ready" "$EXPECTED_READY" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    | envelope "kubectl get pods -l $GRAY_SELECTOR .status.containerStatuses[*].ready" \
               "$RAW_DIR/readiness.json"
  return 0
}

# Regenerate the spend reconciliation document for THIS cycle.
#
# Why a probe rather than production traffic: measured 2026-09-18, production
# cannot supply the id set.  The gray nginx log carries no request id, and adding
# one edits render-production-nginx.py, whose sha256 *is* renderer_identity
# inside config_checksum -- it would invalidate the three hand-signed split
# gates.  SpendLogs.request_id is LiteLLM-internal with no client-visible
# counterpart, metadata->>'litellm_call_id' covers only ~21% of aresponses rows,
# and per-sid request counts run a steady ~25% deficit with a 10x duration
# mismatch.  The one join that does hold end-to-end is the x-litellm-call-id
# response header, which equals metadata->>'litellm_call_id' on the row that
# request writes -- which is the "request-ID spend reconciliation" the runbook
# names at the 1% and 5% rows.
#
# At split=0 the source is legitimately optional (metrics.py only requires it
# above 0), so a cycle there proceeds without one rather than synthesising a
# document that would then have to be trusted for rollback.
collect_spend() {
  local split="$1"
  [[ "$split" -gt 0 ]] || return 0
  if [[ -z "$SPEND_COLLECTOR" || -z "$SPEND_PROBE_KEY_FILE" || -z "$SPEND_PROBE_MODEL" ]]; then
    # Only a hard failure above split 0: metrics.py returns
    # SPEND_RECONCILIATION_MISSING there anyway, and failing here says why.
    log "FAIL spend reconciliation required at split=$split but collector is not configured"
    return 1
  fi
  SPEND_SOURCE="$RAW_DIR/spend.json"
  # The collector writes with O_EXCL-free semantics via tmp+replace, but a stale
  # document from a previous cycle must never survive a failed run and be read as
  # this cycle's evidence.  Remove it first, so absence is the failure shape.
  rm -f "$SPEND_SOURCE"
  if ! python3 "$SPEND_COLLECTOR" \
      --probe-key-file "$SPEND_PROBE_KEY_FILE" \
      --base-url "$SPEND_PROBE_BASE_URL" \
      --model "$SPEND_PROBE_MODEL" \
      --probes "$SPEND_PROBES" \
      --carry-file "$SPEND_CARRY_FILE" \
      --output "$SPEND_SOURCE" >>"$LOG_FILE" 2>&1; then
    log "FAIL collect-spend-reconciliation.py (split=$split)"
    return 1
  fi
  [[ -s "$SPEND_SOURCE" ]] || { log "FAIL spend document empty: $SPEND_SOURCE"; return 1; }
  chmod 600 "$SPEND_SOURCE"
  return 0
}

cycle() {
  local run_id generation checksum phase split
  run_id="$(state_get run_id)"; generation="$(state_get generation)"
  checksum="$(state_get config_checksum)"; phase="$(state_get phase)"
  split="$(state_get split)"
  if [[ -z "$run_id" || -z "$generation" || -z "$checksum" || -z "$phase" || -z "$split" ]]; then
    log "SKIP could not read active state from $ACTIVE/state.env"
    return 1
  fi

  # Snapshot rather than read in place: a rotation mid-parse would truncate the
  # sample, and a short window is exactly the condition under which the stop-loss
  # legs go dark.
  tail -c "$TAIL_BYTES" "$ACCESS_LOG" > "$RAW_DIR/access.log"
  chmod 600 "$RAW_DIR/access.log"

  collect_backend_health
  collect_hard_errors
  if ! collect_readiness "$split"; then
    # Same fail-safe reasoning as spend: READY_CONTAINERS_SHORT bypasses the
    # sustain gate, so a kubectl that cannot answer must not be able to shape
    # itself into "the lane is empty".
    log "SKIP readiness unavailable (split=$split)"
    return 1
  fi
  if ! collect_spend "$split"; then
    # Refusing the cycle is the fail-safe direction.  A spend mismatch is a
    # rollback trigger that bypasses the sustain gate, so a *collector* failure
    # must never be shaped like a mismatch: emitting a document with a shrunken
    # expected set, or an empty one, would either fake a clean reconciliation or
    # dispatch a rollback on our own broken probe rather than on production.
    log "SKIP spend reconciliation unavailable (split=$split)"
    return 1
  fi

  # A fresh output path per cycle.  collect-metrics.py opens its output O_EXCL and
  # refuses a path that already exists, which is the right call -- clobbering
  # would let one cycle overwrite another cycle's evidence -- so the loop must
  # never reuse a name.  Deleting the previous file instead would destroy the
  # record of the cycle that just ran, so each one keeps its own timestamp.
  local input="$INPUT_DIR/metrics-input-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  if [[ -e "$input" || -L "$input" ]]; then
    log "SKIP input path already exists: $input"
    return 1
  fi

  local -a args=(
    --access-log "$RAW_DIR/access.log"
    --hard-errors "$RAW_DIR/hard-errors.json"
    --backend-health "$RAW_DIR/backend-health.json"
    --baseline "$BASELINE"
    --latency-window-minutes "$LATENCY_WINDOW_MINUTES"
    --run-id "$run_id"
    --generation "$generation"
    --config-checksum "$checksum"
    --phase "$phase"
    --rollout-percent "$split"
    --output "$input"
  )
  # metrics.py requires spend reconciliation once any traffic is split by
  # percentage (rollout_percent > 0), and errors the whole cycle without it.  At
  # split=0 it is legitimately absent, so this stays optional here rather than
  # silently synthesising a source that would have to be trusted for rollback.
  [[ -n "$SPEND_SOURCE" ]] && args+=(--spend-reconciliation "$SPEND_SOURCE")
  # Same rule for readiness: present when it could be read, absent otherwise, and
  # metrics.py decides whether absence is fatal for this split.  The collector also
  # derives data_sources from whichever flags are passed, so an omitted readiness
  # here means "not requested" rather than "requested and silent" -- the liveness
  # leg must not report a source we never asked for as having gone quiet.
  [[ -f "$RAW_DIR/readiness.json" ]] && args+=(--readiness "$RAW_DIR/readiness.json")

  if ! python3 "$SCRIPT_DIR/collect-metrics.py" "${args[@]}" >>"$LOG_FILE" 2>&1; then
    log "FAIL collect-metrics.py (split=$split phase=$phase)"
    return 1
  fi
  chmod 600 "$input"

  # gray-monitor-cycle.sh exits non-zero on a FAIL verdict.  That is a statement
  # about production, not a failure of this loop, and it still writes its
  # heartbeat -- so the rc is recorded and the loop continues.
  local rc=0
  "$SCRIPT_DIR/gray-monitor-cycle.sh" \
    --input "$input" \
    --evidence-dir "$EVIDENCE_DIR" >>"$LOG_FILE" 2>&1 || rc=$?
  log "cycle rc=$rc split=$split phase=$phase generation=$generation"
  return 0
}

printf '%s' "$$" >"$PID_FILE"; chmod 600 "$PID_FILE"
log "loop start interval=${INTERVAL}s max_cycles=$MAX_CYCLES baseline=$BASELINE spend=${SPEND_SOURCE:-none} spend_collector=${SPEND_COLLECTOR:-none} expected_ready=${EXPECTED_READY:-unset} pid=$$"

n=0
while :; do
  cycle || true
  n=$((n + 1))
  if [[ "$MAX_CYCLES" -ne 0 && "$n" -ge "$MAX_CYCLES" ]]; then
    log "loop done after $n cycles"
    break
  fi
  sleep "$INTERVAL"
done
rm -f "$PID_FILE"
