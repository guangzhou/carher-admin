#!/usr/bin/env bash
# Pin the K8s half of the frozen artifact into the routing state, as a new generation.
#
# THE HOLE THIS CLOSES
#
# Until 2026-09-21, config_checksum covered exactly seven nginx route files. The
# chart package, the three values files and the image digests were frozen only by
# prose in the run book. So a mid-run patch -- edit values, `helm upgrade`, new
# content-addressed immutable ConfigMap, rolling restart -- left every ruler green:
#
#   * the generation did not rotate, so verify_generation() still passed;
#   * require_gate_evidence() binds evidence to run_id + generation +
#     config_checksum, so every gate approved against the PRE-patch workload
#     stayed valid against the POST-patch one;
#   * gray-monitor-cycle.sh drops sustain counts only across generations, so the
#     streak earned by the old build kept accruing against the new one;
#   * check-pod-spec-shape.py could have caught it, but nothing called it and
#     nothing knew to.
#
# The running build was no longer the approved build, and no ruler in the system
# could say so. That is the empty data column the diagnosis discipline forbids,
# institutionalised.
#
# WHAT PINNING DOES
#
# It records the workload identity into workload.env and rotates the generation.
# workload_checksum() hashes that file into config_checksum, so:
#
#   * every gate evidence file instantly goes stale (generation + checksum both
#     moved) and the ramp must re-earn split_sample,
#     split_monitor_continuity and, at >= 50%, split_capacity;
#   * the sustain streak restarts from zero, because counts do not cross
#     generations -- so the stop-loss legs re-arm against the bytes now running;
#   * `gray-progress.py` shows a new generation, which is what an operator reads.
#
# "I patched" and "I changed the config on the wire" become the same event. To the
# users they always were.
#
# WHAT IT DELIBERATELY DOES NOT DO
#
# It does not talk to the cluster, does not run helm, and does not verify that the
# digests you hand it are what is actually running. It cannot: the values must be
# captured by the operator at the moment of the change, and a tool that fetched
# them itself would just be re-deriving the same claim from the same place. Proving
# the pinned shape matches the live Pod spec is check-pod-spec-shape.py's job, and
# --require-shape-evidence makes that mandatory rather than advisory.
#
# TOUCHES / BACKUP / ROLLBACK
#
# Touches: creates one new generation directory under $GRAY_ROOT/generations and
# moves the `active` symlink to it, exactly like every other routing transition.
# The route maps are copied byte-for-byte from the previous generation -- routing
# does not change, only the recorded identity.
# Backup: the previous generation directory is left intact on disk (nothing is
# deleted), and its path is printed as previous_generation=.
# Rollback: this is a normal generation switch, so `gray-global-rollback.sh` and
# the usual per-key `gray-key-route.sh force-prod` paths are unaffected. To undo a
# pin specifically, pin again with the previous values -- add/verify/cutover, never
# hand-edit workload.env (verify_generation() will refuse it).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "$SCRIPT_DIR/_lib.sh"

usage() {
  cat <<'USAGE'
Usage: gray-workload-pin.sh --reason TEXT --release NAME --chart-package-sha256 HEX \
         --values-sha256 HEX --image-digest sha256:HEX [--release NAME ...] \
         [--shape-evidence FILE] [--no-require-shape-evidence]

Records the K8s side of the frozen artifact and rotates the generation.

  --reason TEXT                what changed and why (goes into workload.env; free text,
                               single line). Required -- an unexplained pin is a pin
                               nobody can audit later.
  --release NAME               Helm release being recorded. Repeatable. Each --release
                               consumes the --chart-package-sha256 / --values-sha256 /
                               --image-digest that follow it.
  --chart-package-sha256 HEX   sha256 of the .tgz AS PACKAGED ON 198. Never recompute
                               this locally: helm 3 writes packaging time into the inner
                               tar, so two packages of identical source differ, and a
                               local helm 4 package differs again. Package once, record
                               that sum, verify with `sha256sum -c`.
  --values-sha256 HEX          sha256 of the frozen values file for that release.
  --image-digest sha256:HEX    the image digest actually deployed (repository@digest).
  --shape-evidence FILE        check-pod-spec-shape.py output proving the live Pod spec
                               matches the target. Required unless explicitly waived.
  --no-require-shape-evidence  waive it. Prints a loud residual-risk line and records
                               shape_evidence=waived in workload.env, so the waiver is
                               part of the hashed artifact rather than a memory.
USAGE
}

REASON=""
SHAPE_EVIDENCE=""
REQUIRE_SHAPE=1
RELEASES=()
declare -a REL_CHART=() REL_VALUES=() REL_IMAGE=()
CURRENT=-1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason) REASON="${2:?missing reason}"; shift 2 ;;
    --release)
      RELEASES+=("${2:?missing release name}")
      REL_CHART+=("") ; REL_VALUES+=("") ; REL_IMAGE+=("")
      CURRENT=$(( ${#RELEASES[@]} - 1 ))
      shift 2 ;;
    --chart-package-sha256)
      [[ "$CURRENT" -ge 0 ]] || die "--chart-package-sha256 must follow a --release"
      REL_CHART[$CURRENT]="${2:?missing chart checksum}"; shift 2 ;;
    --values-sha256)
      [[ "$CURRENT" -ge 0 ]] || die "--values-sha256 must follow a --release"
      REL_VALUES[$CURRENT]="${2:?missing values checksum}"; shift 2 ;;
    --image-digest)
      [[ "$CURRENT" -ge 0 ]] || die "--image-digest must follow a --release"
      REL_IMAGE[$CURRENT]="${2:?missing image digest}"; shift 2 ;;
    --shape-evidence) SHAPE_EVIDENCE="${2:?missing shape evidence path}"; shift 2 ;;
    --no-require-shape-evidence) REQUIRE_SHAPE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$REASON" ]] || { usage; die "--reason is required"; }
# workload.env is a key=value file read with `awk -F=`, and only the first `=` on a
# line splits, so an `=` inside the reason is harmless. A newline is not: it would
# forge a new key. Reject every control character rather than everything non-ASCII,
# because the reason is where an operator writes what they changed and they write it
# in the language they think in.
[[ ! "$REASON" =~ [[:cntrl:]] ]] || die "--reason must be a single line with no control characters"
[[ ${#RELEASES[@]} -gt 0 ]] || { usage; die "at least one --release is required"; }

for index in "${!RELEASES[@]}"; do
  name="${RELEASES[$index]}"
  [[ "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die "invalid release name: $name"
  [[ "${REL_CHART[$index]}" =~ ^[0-9a-f]{64}$ ]] || die "release $name is missing a valid --chart-package-sha256"
  [[ "${REL_VALUES[$index]}" =~ ^[0-9a-f]{64}$ ]] || die "release $name is missing a valid --values-sha256"
  [[ "${REL_IMAGE[$index]}" =~ ^sha256:[0-9a-f]{64}$ ]] || die "release $name is missing a valid --image-digest"
  # A digest of all-zeros/all-ones is what a template placeholder looks like, and
  # prepare-values.py already rejects the same set. Catch it here too: a pin is
  # the artifact of record, and a placeholder pinned once is a placeholder trusted
  # forever.
  case "${REL_IMAGE[$index]}" in
    sha256:0000000000000000000000000000000000000000000000000000000000000000|\
    sha256:1111111111111111111111111111111111111111111111111111111111111111|\
    sha256:2222222222222222222222222222222222222222222222222222222222222222|\
    sha256:3333333333333333333333333333333333333333333333333333333333333333)
      die "release $name has a placeholder image digest" ;;
  esac
done

# Duplicate release names would make the recorded set ambiguous about which entry
# describes what is running.
duplicate="$(printf '%s\n' "${RELEASES[@]}" | sort | uniq -d | head -1)"
[[ -z "$duplicate" ]] || die "release recorded twice: $duplicate"

lock_acquire
load_state

# Pinning is legal in the phases where a workload can legitimately change, and in
# preflight where the initial pin happens. It is NOT legal mid-convergence: between
# convergence_ready and committed the prod release is being swapped by section 7's
# own sequence, and a pin in the middle of that would record a half-applied state
# as if it were the approved one.
# `prod_offline_upgrading` is on this list on purpose, and it is the whole of gap A:
# that is the phase in which section 7 replaces the prod release, so it is the only
# phase in which the prod release's identity CAN be recorded. Barring it was what
# left the release that becomes the stable serving build permanently unpinned.
# `convergence_ready` is still barred -- there the swap has not happened yet, so a
# pin could only record a claim about the future -- and so is `prod_verified`, where
# the phase transition has already consumed the pin as its precondition.
case "$STATE_PHASE" in
  preflight|normal_gray|bridge_preparing|bridge_verified|prod_offline_upgrading) ;;
  *) die "workload pinning is not allowed in phase=$STATE_PHASE (legal phases: preflight, normal_gray, bridge_preparing, bridge_verified, prod_offline_upgrading -- pin the prod release during the upgrade itself, before gray-phase.sh set prod_verified)" ;;
esac

SHAPE_RECORD="waived"
if [[ "$REQUIRE_SHAPE" == "1" ]]; then
  [[ -n "$SHAPE_EVIDENCE" ]] || die "--shape-evidence is required (or pass --no-require-shape-evidence and accept the residual risk)"
  evidence_parent="$(cd "$(dirname "$SHAPE_EVIDENCE")" && pwd -P)"
  secure_file "$SHAPE_EVIDENCE" "$evidence_parent" || die "shape evidence is untrusted: $SHAPE_EVIDENCE"
  # Validated as a real PASS from the real tool, not merely present. A file that
  # exists is not a verdict; this is the exact failure section 6.1.4 of the manual
  # exists to remove.
  python3 - "$SHAPE_EVIDENCE" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        obj = json.load(handle)
except (OSError, ValueError) as exc:
    raise SystemExit(f"gray-rollout: shape evidence is not readable JSON: {exc}")
if not isinstance(obj, dict):
    raise SystemExit("gray-rollout: shape evidence must be an object")
if obj.get("tool") != "check-pod-spec-shape":
    raise SystemExit("gray-rollout: shape evidence was not produced by check-pod-spec-shape.py")
if obj.get("status") != "PASS":
    raise SystemExit(f"gray-rollout: shape evidence status is {obj.get('status')!r}, not PASS")
PY
  SHAPE_RECORD="$(sha256_file "$SHAPE_EVIDENCE")"
else
  printf 'residual_risk: shape evidence waived; the pinned workload identity is NOT proven to match the live Pod spec. 38 subPath overlays can be silently unmounted while rollout status and /health both read green.\n' >&2
fi

# Release entries are emitted in sorted order so the same set of releases always
# hashes the same regardless of the order the flags were typed in. Otherwise
# re-pinning an unchanged workload with the arguments rearranged would rotate the
# generation and invalidate every gate for no reason -- a false red, and false reds
# are what train operators to work around gates.
RELEASE_BLOCK=""
for index in "${!RELEASES[@]}"; do
  RELEASE_BLOCK="$RELEASE_BLOCK$(printf 'release_%s_chart_package_sha256=%s\nrelease_%s_values_sha256=%s\nrelease_%s_image_digest=%s\n' \
    "${RELEASES[$index]}" "${REL_CHART[$index]}" \
    "${RELEASES[$index]}" "${REL_VALUES[$index]}" \
    "${RELEASES[$index]}" "${REL_IMAGE[$index]}")"
done
# LC_ALL=C so the byte order is the same on every host. A locale-dependent sort
# would make the pinned file -- and therefore config_checksum -- depend on the
# operator's shell environment, which is exactly the kind of invisible input this
# whole mechanism exists to eliminate.
RELEASE_BLOCK="$(printf '%s' "$RELEASE_BLOCK" | LC_ALL=C sort)"

# The pinned body is assembled and hashed BEFORE any staging happens, so the no-op
# case below costs nothing and leaves no half-built generation behind.
#
# `pinned_at` is deliberately OUTSIDE this comparison body but INSIDE the file that
# gets hashed into config_checksum: two pins of the same workload must compare
# equal (no false red), yet once a rotation does happen the recorded time must be
# the time it actually happened.
WORKLOAD_BODY="$(printf 'schema_version=1\nreason=%s\nshape_evidence=%s\nrelease_count=%s\n%s\n' \
  "$REASON" "$SHAPE_RECORD" "${#RELEASES[@]}" "$RELEASE_BLOCK")"

PREVIOUS_WORKLOAD="${STATE_WORKLOAD_CHECKSUM:-unbound}"
ACTIVE_DIR="$(active_dir)"
PREVIOUS_BODY_SUM="unbound"
if [[ -f "$ACTIVE_DIR/workload.env" ]]; then
  PREVIOUS_BODY_SUM="$(sha256_text "$(sed '/^pinned_at=/d' "$ACTIVE_DIR/workload.env")")"
fi

# Re-pinning byte-identical content would rotate the generation and invalidate
# every gate while changing nothing on the wire. Routing is untouched by a pin, so
# in that case there is nothing to commit -- say so and stop.
if [[ "$(sha256_text "$WORKLOAD_BODY")" == "$PREVIOUS_BODY_SUM" ]]; then
  printf 'workload unchanged; generation not rotated\nworkload_checksum=%s\n' "$PREVIOUS_WORKLOAD"
  exit 0
fi

stage_from_active
{
  printf 'schema_version=1\n'
  printf 'pinned_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'reason=%s\n' "$REASON"
  printf 'shape_evidence=%s\n' "$SHAPE_RECORD"
  printf 'release_count=%s\n' "${#RELEASES[@]}"
  printf '%s\n' "$RELEASE_BLOCK"
} >"$STAGE_DIR/workload.env"
chmod 600 "$STAGE_DIR/workload.env"
NEW_WORKLOAD="$(sha256_file "$STAGE_DIR/workload.env")"

render_fragments "$STAGE_DIR" "$STATE_MODE" "$STATE_SPLIT" "$STATE_BRIDGE"
write_state "$STAGE_DIR" "$STATE_RUN_ID" "$(basename "$STAGE_DIR")" "$STATE_PHASE" "$STATE_MODE" "$STATE_SPLIT" "$STATE_BRIDGE" "$STATE_FROZEN"
commit_stage "$STAGE_DIR" "$OLD_ACTIVE_DIR"

printf 'previous_generation=%s\n' "$(basename "$OLD_ACTIVE_DIR")"
printf 'previous_workload_checksum=%s\n' "$PREVIOUS_WORKLOAD"
printf 'workload_checksum=%s\n' "$NEW_WORKLOAD"
cat <<'NOTICE'
gate evidence is now stale by construction: config_checksum rotated, so
split_sample / split_monitor_continuity / split_capacity must be re-earned and
the sustain streak restarts at zero. Re-arm before the next ramp step.
NOTICE
