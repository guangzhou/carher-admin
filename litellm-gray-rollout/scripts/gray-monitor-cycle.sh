#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

usage() {
  printf 'Usage: %s --input METRICS_INPUT.json [--evidence-dir DIR]\n' "$0"
}

INPUT=""
EVIDENCE_DIR="$GRAY_GATE_EVIDENCE_DIR"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --input) INPUT="${2:?missing metrics input}"; shift 2 ;;
    --evidence-dir) EVIDENCE_DIR="${2:?missing evidence directory}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die2 "unknown argument: $1" ;;
  esac
done
[[ -n "$INPUT" ]] || die2 "--input is required"

METRICS_TOOL="${GRAY_METRICS_TOOL:-$SCRIPT_DIR/metrics.py}"
DISPATCH_TOOL="${GRAY_DISPATCH_TOOL:-$SCRIPT_DIR/gray-auto-dispatch.sh}"
if ! is_test_mode && [[ "$METRICS_TOOL" != "$SCRIPT_DIR/metrics.py" || "$DISPATCH_TOOL" != "$SCRIPT_DIR/gray-auto-dispatch.sh" ]]; then
  die2 "GRAY_METRICS_TOOL/GRAY_DISPATCH_TOOL overrides are allowed only in test mode"
fi
require_privilege
umask 077

input_parent="$(cd "$(dirname "$INPUT")" && pwd -P)"
evidence_parent="$(cd "$EVIDENCE_DIR" && pwd -P)"
secure_dir "$input_parent" "$input_parent"
secure_file "$INPUT" "$input_parent"
secure_dir "$evidence_parent" "$evidence_parent"

python3 - "$METRICS_TOOL" "$DISPATCH_TOOL" "$(expected_owner_uid)" <<'PY'
import os
import stat
import sys

expected_uid = int(sys.argv[3])
for path in sys.argv[1:3]:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise SystemExit(f"gray-rollout: cannot stat executable {path}: {exc}")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit(f"gray-rollout: executable is not a regular file: {path}")
    if info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit(f"gray-rollout: executable owner/mode is unsafe: {path}")
    if not os.access(path, os.X_OK):
        raise SystemExit(f"gray-rollout: executable bit is missing: {path}")
PY

# Single cleanup path for every temporary this script owns. Each `trap ... EXIT`
# REPLACES the previous one, so the lock directory and the scratch input cannot
# register separate handlers: a second trap would silently leak the lock dir and
# wedge every later cycle behind "another metrics cycle is running".
CLEANUP_LOCK_DIR=""
CLEANUP_FILES=()
cleanup() {
  local path
  for path in ${CLEANUP_FILES+"${CLEANUP_FILES[@]}"}; do
    [[ -n "$path" ]] && rm -f "$path"
  done
  [[ -n "$CLEANUP_LOCK_DIR" ]] && rmdir "$CLEANUP_LOCK_DIR" 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

lock_path="$evidence_parent/.monitor-cycle.lock"
if command -v flock >/dev/null 2>&1; then
  exec 8>"$lock_path"
  flock -n 8 || die "another metrics cycle is running"
else
  lock_dir="$lock_path.dir"
  mkdir "$lock_dir" 2>/dev/null || die "another metrics cycle is running"
  CLEANUP_LOCK_DIR="$lock_dir"
fi

# Sustain state. A single window over a statistical threshold is mostly noise,
# so metrics.py only promotes a breach to a trigger once it has repeated. That
# requires carrying counts across cycles, and the input file is frozen evidence
# we must not rewrite -- so the state lives beside the ledger and is spliced into
# a private copy of the input. Absent state reads as empty, which makes a fresh
# cycle need the full streak again: losing this file can only ever delay a
# rollback, never cause one.
sustain_state="$evidence_parent/monitor-sustain.json"
if [[ -L "$sustain_state" ]]; then
  die "monitor sustain state must not be a symlink: $sustain_state"
fi
metrics_input="$(mktemp "$evidence_parent/.metrics-input.XXXXXX")"
chmod 600 "$metrics_input"
CLEANUP_FILES+=("$metrics_input")
python3 - "$INPUT" "$sustain_state" "$metrics_input" <<'PY'
import json
import sys

source, state_path, target = sys.argv[1:]
with open(source, encoding="utf-8") as handle:
    payload = json.load(handle)
if not isinstance(payload, dict):
    raise SystemExit("gray-rollout: metrics input is not an object")
try:
    with open(state_path, encoding="utf-8") as handle:
        carried = json.load(handle)
except (OSError, ValueError):
    carried = {}
# A state file that is not a flat object is treated as absent rather than
# trusted: metrics.py filters the contents anyway, and the fail-safe direction
# is "require the streak again".
if not isinstance(carried, dict):
    carried = {}
# Streaks are only meaningful within one generation. A new generation is a new
# config on the wire, so counts earned against the old one say nothing about the
# new one; drop them instead of letting a pre-cutover breach half-complete a
# streak that then finishes against different bytes.
evidence = payload.get("evidence")
evidence = evidence if isinstance(evidence, dict) else {}
same_run = (
    carried.get("run_id") == evidence.get("run_id")
    and carried.get("generation") == evidence.get("generation")
)
counts = carried.get("counts") if same_run else {}
payload["sustain_state"] = counts if isinstance(counts, dict) else {}
with open(target, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
PY

temporary="$(mktemp "$evidence_parent/.metrics-cycle.XXXXXX")"
chmod 600 "$temporary"
if "$METRICS_TOOL" --input "$metrics_input" >"$temporary"; then
  metrics_rc=0
else
  metrics_rc=$?
fi
if [[ "$metrics_rc" -ne 0 && "$metrics_rc" -ne 1 ]]; then
  rm -f "$temporary"
  die "metrics evaluation failed with rc=$metrics_rc; dispatcher was not called"
fi

python3 - "$temporary" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("tool") != "metrics":
        raise ValueError("not a metrics result")
    if payload.get("status") not in {"PASS", "FAIL"}:
        raise ValueError("metrics result is not dispatchable")
except Exception as exc:
    raise SystemExit(f"gray-rollout: invalid metrics output; dispatcher was not called: {exc}")
PY

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
evidence="$evidence_parent/metrics-$stamp-$$.json"
[[ ! -e "$evidence" && ! -L "$evidence" ]] || {
  rm -f "$temporary"
  die "metrics evidence path already exists"
}
mv "$temporary" "$evidence"
chmod 600 "$evidence"

# Persist the streak counts BEFORE dispatching. If the dispatcher rolls back it
# never returns here, and a breach that was one window short of promotion would
# be forgotten -- so the window that would have completed the streak has to be
# recorded while we still can. Written under the same lock as everything else.
python3 - "$evidence" "$sustain_state" <<'PY'
import json
import os
import stat
import sys

evidence, state_path = sys.argv[1:]
with open(evidence, encoding="utf-8") as handle:
    result = json.load(handle)
summary = result.get("sustain")
counts = summary.get("counts", {}) if isinstance(summary, dict) else {}
record = {
    "schema_version": 1,
    "run_id": result.get("run_id"),
    "generation": result.get("generation"),
    "counts": counts if isinstance(counts, dict) else {},
}
flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(state_path, flags, 0o600)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("gray-rollout: monitor sustain state has unsafe mode")
    os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
    os.fsync(fd)
finally:
    os.close(fd)
PY

if ! "$DISPATCH_TOOL" --input "$evidence"; then
  die "dispatcher rejected frozen metrics evidence: $evidence"
fi

# Deadman ledger. A cycle that never ran leaves no trace anywhere else: metrics
# evidence files only prove the cycles that DID run, so an unobserved window and
# a healthy window look identical at ramp time. Append one line per completed
# cycle, under the same lock, and let check-monitor-continuity.py turn the gaps
# into a gate. Written only after dispatch succeeds: a half-finished cycle did
# not observe anything.
heartbeat="$evidence_parent/monitor-heartbeat.jsonl"
if [[ -L "$heartbeat" ]]; then
  die "monitor heartbeat ledger must not be a symlink: $heartbeat"
fi
python3 - "$heartbeat" "$evidence" "$INPUT" "$(is_test_mode && printf 1 || printf 0)" <<'PY'
import datetime as dt
import json
import os
import stat
import sys

ledger, evidence, source, test_mode = sys.argv[1:]

with open(evidence, encoding="utf-8") as handle:
    result = json.load(handle)

run_id = generation = None
try:
    with open(source, encoding="utf-8") as handle:
        bound = json.load(handle).get("evidence") or {}
    if isinstance(bound, dict):
        run_id = bound.get("run_id")
        generation = bound.get("generation")
except (OSError, ValueError):
    pass
if test_mode != "1" and not (isinstance(run_id, str) and isinstance(generation, str)):
    raise SystemExit(
        "gray-rollout: metrics input is not bound to a run_id/generation; "
        "regenerate it with collect-metrics.py"
    )

record = {
    "schema_version": 1,
    "tool": "gray-monitor-cycle",
    "cycle_completed_at": dt.datetime.now(dt.timezone.utc)
    .isoformat(timespec="seconds")
    .replace("+00:00", "Z"),
    "run_id": run_id,
    "generation": generation,
    "metrics_status": result.get("status"),
    "evidence": os.path.basename(evidence),
}
flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(ledger, flags, 0o600)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("gray-rollout: monitor heartbeat ledger has unsafe mode")
    os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
    os.fsync(fd)
finally:
    os.close(fd)
PY

printf 'evidence=%s\n' "$evidence"
printf 'heartbeat=%s\n' "$heartbeat"
