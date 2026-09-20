#!/usr/bin/env bash
# Shared, fail-closed transaction helpers for the LiteLLM gray rollout tools.

set -euo pipefail

GRAY_ROOT="${GRAY_ROOT:-/var/lib/litellm-gray-rollout}"
GRAY_GENERATIONS="$GRAY_ROOT/generations"
GRAY_ACTIVE="$GRAY_ROOT/active"
GRAY_LOCK_FILE="$GRAY_ROOT/.txn.lock"
GRAY_LOCK_DIR="$GRAY_ROOT/.txn.lockdir"
GRAY_TXN_JOURNAL="$GRAY_ROOT/transaction-in-progress.env"
GRAY_ALLOW_NONROOT="${GRAY_ALLOW_NONROOT:-0}"
GRAY_TEST_MODE="${GRAY_TEST_MODE:-0}"
GRAY_GATE_EVIDENCE_DIR="${GRAY_GATE_EVIDENCE_DIR:-$GRAY_ROOT/evidence}"
GRAY_GATE_MAX_AGE_SECONDS="${GRAY_GATE_MAX_AGE_SECONDS:-86400}"
# The Helm release that serves production after convergence. Section 7 upgrades it
# with `--reset-values`, which is the single highest-radius action in the whole run,
# and until 2026-09-21 it was the one release nothing ever pinned: the pin was only
# ever run for the gray and guarded-old releases, so the build that BECOMES the
# stable serving build had exactly the hole the pin exists to close.
GRAY_PROD_RELEASE="${GRAY_PROD_RELEASE:-litellm-product-proxy}"

is_test_mode() {
  [[ "$GRAY_TEST_MODE" == "1" ]]
}

die() {
  printf 'gray-rollout: %s\n' "$*" >&2
  exit 1
}

die2() {
  printf 'gray-rollout: %s\n' "$*" >&2
  exit 2
}

require_privilege() {
  if [[ "$GRAY_ALLOW_NONROOT" != "1" ]] && [[ "$(id -u)" != "0" ]]; then
    die "root privileges are required (set GRAY_ALLOW_NONROOT=1 only for isolated tests)"
  fi
}

expected_owner_uid() {
  if [[ "$GRAY_ALLOW_NONROOT" == "1" ]]; then
    id -u
  else
    printf '0\n'
  fi
}

secure_path_check() {
  local path="$1" kind="$2" mode="$3" containment="$4"
  python3 - "$path" "$kind" "$mode" "$containment" "$(expected_owner_uid)" "$(is_test_mode && printf 1 || printf 0)" <<'PY'
import os
import stat
import sys

path, kind, expected_mode, containment, expected_uid, test_mode = sys.argv[1:]
expected_mode = int(expected_mode, 8)
expected_uid = int(expected_uid)
test_mode = test_mode == "1"

try:
    containment_real = os.path.realpath(containment)
    path_abs = os.path.abspath(path)
    resolved = os.path.realpath(path_abs)
    if os.path.commonpath((containment_real, resolved)) != containment_real:
        raise ValueError("path escapes trusted containment")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if kind == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path_abs, flags)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if kind == "file" and not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    if kind == "directory" and not stat.S_ISDIR(info.st_mode):
        raise ValueError("not a directory")
    if info.st_uid != expected_uid:
        raise ValueError(f"owner uid {info.st_uid} != {expected_uid}")
    if stat.S_IMODE(info.st_mode) != expected_mode:
        raise ValueError(f"mode {stat.S_IMODE(info.st_mode):04o} != {expected_mode:04o}")

    current = containment_real
    target_parent = resolved if kind == "directory" else os.path.dirname(resolved)
    relative = os.path.relpath(target_parent, containment_real)
    parts = [] if relative == "." else relative.split(os.sep)
    for part in parts:
        current = os.path.join(current, part)
        parent_info = os.lstat(current)
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise ValueError("trusted path contains a symlink or non-directory")
        if parent_info.st_uid != expected_uid:
            raise ValueError("trusted directory owner mismatch")
        if not test_mode and stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise ValueError("trusted directory is not owner-only")
except Exception as exc:
    print(f"gray-rollout: insecure {kind} {path}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

secure_dir() {
  secure_path_check "$1" directory 700 "$2"
}

secure_file() {
  secure_path_check "$1" file 600 "$2"
}

ensure_root() {
  require_privilege
  umask 077
  if [[ ! -e "$GRAY_ROOT" && ! -L "$GRAY_ROOT" ]]; then
    mkdir -p "$GRAY_ROOT"
  fi
  secure_dir "$GRAY_ROOT" "$GRAY_ROOT"
  if [[ ! -e "$GRAY_GENERATIONS" && ! -L "$GRAY_GENERATIONS" ]]; then
    mkdir "$GRAY_GENERATIONS"
  fi
  secure_dir "$GRAY_GENERATIONS" "$GRAY_ROOT"
  if [[ "$GRAY_GATE_EVIDENCE_DIR" == "$GRAY_ROOT"/* ]]; then
    if [[ ! -e "$GRAY_GATE_EVIDENCE_DIR" && ! -L "$GRAY_GATE_EVIDENCE_DIR" ]]; then
      mkdir -p "$GRAY_GATE_EVIDENCE_DIR"
    fi
    secure_dir "$GRAY_GATE_EVIDENCE_DIR" "$GRAY_ROOT"
  fi
}

lock_acquire() {
  ensure_root
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$GRAY_LOCK_FILE"
    flock -x 9
    GRAY_LOCK_KIND=flock
  elif [[ "$GRAY_ALLOW_NONROOT" == "1" ]]; then
    local tries=0
    while ! mkdir "$GRAY_LOCK_DIR" 2>/dev/null; do
      tries=$((tries + 1))
      [[ "$tries" -lt 100 ]] || die "timed out acquiring transaction lock"
      sleep 0.01
    done
    GRAY_LOCK_KIND="mkdir"
  else
    die "flock is required for production rollout transactions"
  fi
  trap transaction_exit_handler EXIT INT TERM HUP
  recover_incomplete_transaction
}

lock_release() {
  if [[ "${GRAY_LOCK_KIND:-}" == "flock" ]]; then
    flock -u 9 2>/dev/null || true
    exec 9>&- 2>/dev/null || true
  elif [[ "${GRAY_LOCK_KIND:-}" == "mkdir" ]]; then
    rmdir "$GRAY_LOCK_DIR" 2>/dev/null || true
    GRAY_LOCK_KIND=""
  fi
}

write_transaction_journal() {
  local kind="$1" old="$2" stage="$3" status="$4" tmp
  tmp="$(mktemp "$GRAY_ROOT/.transaction.XXXXXX")"
  {
    printf 'kind=%s\n' "$kind"
    printf 'old=%s\n' "$old"
    printf 'stage=%s\n' "$stage"
    printf 'status=%s\n' "$status"
  } >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$GRAY_TXN_JOURNAL"
}

journal_get() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted { print substr($0, index($0, "=") + 1); found=1; exit } END { if (!found) exit 1 }' "$GRAY_TXN_JOURNAL"
}

clear_transaction_journal() {
  rm -f "$GRAY_TXN_JOURNAL"
}

recover_incomplete_transaction() {
  [[ -e "$GRAY_TXN_JOURNAL" || -L "$GRAY_TXN_JOURNAL" ]] || return 0
  secure_file "$GRAY_TXN_JOURNAL" "$GRAY_ROOT"
  local kind old stage status current=""
  kind="$(journal_get kind)"; old="$(journal_get old)"; stage="$(journal_get stage)"; status="$(journal_get status)"
  [[ "$kind" == "generation" || "$kind" == "initial" ]] || die "unknown transaction journal kind"
  [[ "$status" =~ ^(switch_pending|active_switched|reloaded)$ ]] || die "unknown transaction journal status"
  secure_dir "$stage" "$GRAY_GENERATIONS"
  if [[ -L "$GRAY_ACTIVE" ]]; then current="$(active_dir)"; fi
  if [[ "$kind" == "initial" ]]; then
    if [[ "$current" == "$stage" ]]; then
      verify_frozen_initial_rollback "$stage"
      run_optional "${GRAY_INITIAL_ROLLBACK_CMD:-}" || die "cannot recover interrupted initial routing transaction"
      rm -f "$GRAY_ACTIVE"
    elif [[ -n "$current" ]]; then
      die "interrupted initial transaction found an unexpected active generation"
    fi
    rm -rf "$stage"
    clear_transaction_journal
    return 0
  fi
  secure_dir "$old" "$GRAY_GENERATIONS"
  if [[ "$current" == "$stage" ]]; then
    switch_active "$old"
    render_candidate "$old" || die "cannot render old generation during transaction recovery"
    nginx_test || die "old generation fails nginx -t during transaction recovery"
    verify_render_attestation "$old" || die "old generation attestation fails during transaction recovery"
    nginx_reload || die "cannot reload old generation during transaction recovery"
    post_reload_check || die "old generation post-check fails during transaction recovery"
  elif [[ "$current" != "$old" ]]; then
    die "interrupted transaction found an unexpected active generation"
  fi
  rm -rf "$stage"
  clear_transaction_journal
}

transaction_exit_handler() {
  local rc=$?
  trap - EXIT INT TERM HUP
  if [[ "${GRAY_TXN_COMMITTED:-0}" != "1" && ( -e "$GRAY_TXN_JOURNAL" || -L "$GRAY_TXN_JOURNAL" ) ]]; then
    recover_incomplete_transaction || rc=1
  fi
  lock_release
  exit "$rc"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

sha256_text() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | awk '{print $1}'
  else
    printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
  fi
}

canonical_path() {
  python3 - "$1" <<'PY'
import os
import sys
print(os.path.realpath(os.path.abspath(sys.argv[1])))
PY
}

require_external_render_file() {
  local path="$1" label="$2" expected_mode="${3:-}"
  python3 - "$path" "$label" "$expected_mode" "$(expected_owner_uid)" <<'PY'
import os
import stat
import sys

path, label, expected_mode, expected_uid = sys.argv[1:]
try:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("must be a regular non-symlink file")
    if info.st_uid != int(expected_uid):
        raise ValueError(f"owner uid {info.st_uid} != {expected_uid}")
    if expected_mode and stat.S_IMODE(info.st_mode) != int(expected_mode, 8):
        raise ValueError(f"mode {stat.S_IMODE(info.st_mode):04o} != {int(expected_mode, 8):04o}")
except Exception as exc:
    print(f"gray-rollout: invalid {label} {path}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

require_renderer_environment() {
  [[ -n "${GRAY_RENDER_CMD:-}" ]] || die "GRAY_RENDER_CMD is required outside test mode"
  [[ -n "${GRAY_RENDER_BASE_TEMPLATE:-}" ]] || die "GRAY_RENDER_BASE_TEMPLATE is required outside test mode"
  [[ -n "${GRAY_RENDER_DEBUG_TOKEN_FILE:-}" ]] || die "GRAY_RENDER_DEBUG_TOKEN_FILE is required outside test mode"
  [[ -n "${GRAY_RENDERER_FILE:-}" ]] || die "GRAY_RENDERER_FILE is required outside test mode"
  [[ -n "${GRAY_RENDER_OUTPUT:-}" ]] || die "GRAY_RENDER_OUTPUT is required outside test mode"
  require_external_render_file "$GRAY_RENDER_BASE_TEMPLATE" "render base template" 600
  require_external_render_file "$GRAY_RENDER_DEBUG_TOKEN_FILE" "render debug token" 600
  require_external_render_file "$GRAY_RENDERER_FILE" "renderer"
  if [[ -L "$GRAY_RENDER_OUTPUT" ]]; then
    die "GRAY_RENDER_OUTPUT must not be a symlink"
  fi
}

renderer_manifest_lines() {
  require_renderer_environment
  printf 'render_command=%s\n' "$(sha256_text "$GRAY_RENDER_CMD")"
  printf 'render_base_template=%s\n' "$(sha256_file "$GRAY_RENDER_BASE_TEMPLATE")"
  printf 'render_base_template_path=%s\n' "$(sha256_text "$(canonical_path "$GRAY_RENDER_BASE_TEMPLATE")")"
  printf 'render_debug_token=%s\n' "$(sha256_file "$GRAY_RENDER_DEBUG_TOKEN_FILE")"
  printf 'render_debug_token_path=%s\n' "$(sha256_text "$(canonical_path "$GRAY_RENDER_DEBUG_TOKEN_FILE")")"
  printf 'renderer_identity=%s\n' "$(sha256_file "$GRAY_RENDERER_FILE")"
  printf 'renderer_path=%s\n' "$(sha256_text "$(canonical_path "$GRAY_RENDERER_FILE")")"
  printf 'render_output=%s\n' "$(sha256_text "$(canonical_path "$GRAY_RENDER_OUTPUT")")"
}

require_routing_transaction_environment() {
  [[ -n "${GRAY_NGINX_TEST_CMD:-}" ]] || die "GRAY_NGINX_TEST_CMD is required outside test mode"
  [[ -n "${GRAY_RELOAD_CMD:-}" ]] || die "GRAY_RELOAD_CMD is required outside test mode"
  [[ -n "${GRAY_POST_RELOAD_CMD:-}" ]] || die "GRAY_POST_RELOAD_CMD is required outside test mode"
  [[ "$GRAY_GATE_MAX_AGE_SECONDS" =~ ^[0-9]+$ ]] || die "GRAY_GATE_MAX_AGE_SECONDS must be a non-negative integer"
}

require_routing_init_environment() {
  require_routing_transaction_environment
  [[ -n "${GRAY_INITIAL_ROLLBACK_CMD:-}" ]] || die "GRAY_INITIAL_ROLLBACK_CMD is required outside test mode"
  [[ -n "${GRAY_ABORT_VERIFY_CMD:-}" ]] || die "GRAY_ABORT_VERIFY_CMD is required outside test mode"
}

routing_contract_manifest_lines() {
  require_routing_init_environment
  printf 'nginx_test_command=%s\n' "$(sha256_text "$GRAY_NGINX_TEST_CMD")"
  printf 'reload_command=%s\n' "$(sha256_text "$GRAY_RELOAD_CMD")"
  printf 'post_reload_command=%s\n' "$(sha256_text "$GRAY_POST_RELOAD_CMD")"
  printf 'initial_rollback_command=%s\n' "$(sha256_text "$GRAY_INITIAL_ROLLBACK_CMD")"
  printf 'abort_verify_command=%s\n' "$(sha256_text "$GRAY_ABORT_VERIFY_CMD")"
  printf 'gate_max_age_seconds=%s\n' "$(sha256_text "$GRAY_GATE_MAX_AGE_SECONDS")"
  routing_hook_file_manifest_lines
}

routing_hook_file_manifest_lines() {
  local name value token path seen="|" first
  for name in GRAY_NGINX_TEST_CMD GRAY_RELOAD_CMD GRAY_POST_RELOAD_CMD GRAY_INITIAL_ROLLBACK_CMD GRAY_ABORT_VERIFY_CMD; do
    value="${!name:-}"
    first=1
    while IFS= read -r token; do
      [[ -n "$token" ]] || continue
      # Only the executable identity is frozen. Later path arguments may be
      # evidence or marker files that legitimately appear during a retry.
      if (( first == 0 )); then
        continue
      fi
      first=0
      path="$(canonical_path "$token")"
      [[ -f "$path" && ! -L "$path" && -x "$path" ]] || continue
      [[ "$seen" != *"|$path|"* ]] || continue
      seen="${seen}${path}|"
      printf 'hook_file_%s=%s\n' "$(sha256_text "$path" | cut -c1-16)" "$(sha256_file "$path")"
    done < <(python3 - "$value" <<'PY'
import shlex
import sys
try:
    for token in shlex.split(sys.argv[1]):
        if "/" in token:
            print(token)
except ValueError:
    raise SystemExit(1)
PY
    )
  done
}

manifest_get() {
  local dir="$1" key="$2"
  awk -F= -v wanted="$key" '$1 == wanted { print substr($0, index($0, "=") + 1); found=1; exit } END { if (!found) exit 1 }' "$dir/input-checksums.env"
}

verify_frozen_renderer_inputs() {
  local dir="$1"
  is_test_mode && return 0
  require_renderer_environment
  [[ "$(manifest_get "$dir" render_command)" == "$(sha256_text "$GRAY_RENDER_CMD")" ]] || die "frozen render command checksum mismatch"
  [[ "$(manifest_get "$dir" render_base_template)" == "$(sha256_file "$GRAY_RENDER_BASE_TEMPLATE")" ]] || die "frozen render base template checksum mismatch"
  [[ "$(manifest_get "$dir" render_base_template_path)" == "$(sha256_text "$(canonical_path "$GRAY_RENDER_BASE_TEMPLATE")")" ]] || die "frozen render base template path mismatch"
  [[ "$(manifest_get "$dir" render_debug_token)" == "$(sha256_file "$GRAY_RENDER_DEBUG_TOKEN_FILE")" ]] || die "frozen render debug token checksum mismatch"
  [[ "$(manifest_get "$dir" render_debug_token_path)" == "$(sha256_text "$(canonical_path "$GRAY_RENDER_DEBUG_TOKEN_FILE")")" ]] || die "frozen render debug token path mismatch"
  [[ "$(manifest_get "$dir" renderer_identity)" == "$(sha256_file "$GRAY_RENDERER_FILE")" ]] || die "frozen renderer checksum mismatch"
  [[ "$(manifest_get "$dir" renderer_path)" == "$(sha256_text "$(canonical_path "$GRAY_RENDERER_FILE")")" ]] || die "frozen renderer path mismatch"
  [[ "$(manifest_get "$dir" render_output)" == "$(sha256_text "$(canonical_path "$GRAY_RENDER_OUTPUT")")" ]] || die "frozen render output path mismatch"
}

verify_frozen_routing_contract() {
  local dir="$1"
  is_test_mode && return 0
  require_routing_transaction_environment
  [[ "$(manifest_get "$dir" nginx_test_command)" == "$(sha256_text "$GRAY_NGINX_TEST_CMD")" ]] || die "frozen nginx test command checksum mismatch"
  [[ "$(manifest_get "$dir" reload_command)" == "$(sha256_text "$GRAY_RELOAD_CMD")" ]] || die "frozen reload command checksum mismatch"
  [[ "$(manifest_get "$dir" post_reload_command)" == "$(sha256_text "$GRAY_POST_RELOAD_CMD")" ]] || die "frozen post-reload command checksum mismatch"
  [[ "$(manifest_get "$dir" gate_max_age_seconds)" == "$(sha256_text "$GRAY_GATE_MAX_AGE_SECONDS")" ]] || die "frozen gate max age checksum mismatch"
  local expected actual
  expected="$(awk -F= '$1 ~ /^hook_file_/ {print}' "$dir/input-checksums.env" | sort)"
  actual="$(routing_hook_file_manifest_lines | sort)"
  [[ "$expected" == "$actual" ]] || die "frozen routing hook file checksum mismatch"
}

verify_frozen_initial_rollback() {
  local dir="$1"
  is_test_mode && return 0
  [[ -n "${GRAY_INITIAL_ROLLBACK_CMD:-}" ]] || die "GRAY_INITIAL_ROLLBACK_CMD is required outside test mode"
  [[ "$(manifest_get "$dir" initial_rollback_command)" == "$(sha256_text "$GRAY_INITIAL_ROLLBACK_CMD")" ]] || die "frozen initial rollback command checksum mismatch"
}

verify_frozen_abort_verify() {
  local dir="$1"
  is_test_mode && return 0
  [[ -n "${GRAY_ABORT_VERIFY_CMD:-}" ]] || die "GRAY_ABORT_VERIFY_CMD is required outside test mode"
  [[ "$(manifest_get "$dir" abort_verify_command)" == "$(sha256_text "$GRAY_ABORT_VERIFY_CMD")" ]] || die "frozen abort verify command checksum mismatch"
}

verify_frozen_gate_max_age() {
  local dir="$1"
  is_test_mode && return 0
  [[ "$GRAY_GATE_MAX_AGE_SECONDS" =~ ^[0-9]+$ ]] || die "GRAY_GATE_MAX_AGE_SECONDS must be a non-negative integer"
  [[ "$(manifest_get "$dir" gate_max_age_seconds)" == "$(sha256_text "$GRAY_GATE_MAX_AGE_SECONDS")" ]] || die "frozen gate max age checksum mismatch"
}

active_dir() {
  ensure_root
  python3 - "$GRAY_ACTIVE" "$GRAY_GENERATIONS" "$(expected_owner_uid)" <<'PY'
import os
import stat
import sys

active, generations, expected_uid = sys.argv[1], os.path.realpath(sys.argv[2]), int(sys.argv[3])
try:
    link_info = os.lstat(active)
    if not stat.S_ISLNK(link_info.st_mode):
        raise ValueError("active generation is not a symlink")
    if link_info.st_uid != expected_uid:
        raise ValueError("active symlink owner mismatch")
    target = os.path.realpath(active)
    if os.path.commonpath((generations, target)) != generations or target == generations:
        raise ValueError("active target escapes generations")
    target_info = os.lstat(target)
    if not stat.S_ISDIR(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
        raise ValueError("active target is not a real directory")
    if target_info.st_uid != expected_uid or stat.S_IMODE(target_info.st_mode) != 0o700:
        raise ValueError("active generation owner/mode mismatch")
except Exception as exc:
    print(f"gray-rollout: invalid active generation: {exc}", file=sys.stderr)
    raise SystemExit(1)
print(target)
PY
}

state_get_from() {
  local dir="$1" key="$2"
  awk -F= -v wanted="$key" '$1 == wanted { print substr($0, index($0, "=") + 1); found=1; exit } END { if (!found) exit 1 }' "$dir/state.env"
}

state_get() {
  state_get_from "$(active_dir)" "$1"
}

validate_scalar() {
  local key="$1" value="$2"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "invalid newline in state field $key"
  case "$key" in
    phase) [[ "$value" =~ ^(preflight|bridge_preparing|bridge_verified|normal_gray|convergence_ready|prod_offline_upgrading|prod_verified|committed|rolled_back|aborting_to_bridge|aborted|post_commit_bridge|post_commit_prod_verified|post_commit_rolled_back)$ ]] || die "invalid phase" ;;
    mode|routing_frozen) [[ "$value" =~ ^[01]$ ]] || die "invalid $key" ;;
    split) [[ "$value" =~ ^(0|[1-9][0-9]?|100)$ ]] || die "invalid split" ;;
    bridge) [[ "$value" =~ ^(off|guarded-old)$ ]] || die "invalid bridge" ;;
    run_id|generation) [[ "$value" =~ ^[A-Za-z0-9._:-]+$ ]] || die "invalid $key" ;;
    config_checksum|input_checksum) [[ "$value" == test-mode || "$value" =~ ^[0-9a-f]{64}$ ]] || die "invalid $key" ;;
    workload_checksum) [[ "$value" == unbound || "$value" =~ ^[0-9a-f]{64}$ ]] || die "invalid $key" ;;
  esac
}

# Fail closed on a run whose K8s side was never pinned.
#
# This is what stops the hole from simply reappearing as "nobody ran the pin
# tool". An unbound run can still be loaded, inspected and rolled back; it may
# not ramp, prepare convergence, or commit. The distinction matters because the
# rollback path must keep working for runs created before this existed -- making
# them unloadable would trade a visibility hole for an unrecoverable run.
require_workload_binding() {
  local what="$1"
  is_test_mode && return 0
  [[ "${STATE_WORKLOAD_CHECKSUM:-unbound}" != "unbound" ]] || die \
    "$what requires a pinned workload: run gray-workload-pin.sh first (K8s side of the frozen artifact is unrecorded, so no gate can tell an approved build from a patched one)"
}

# Fail closed unless a NAMED release is inside the pin.
#
# `require_workload_binding` only asks "is anything pinned?", and the answer was
# always yes from preflight onwards because preflight pins the gray release. So it
# could not see the actual convergence hole: section 7 replaces the prod release
# with `helm upgrade --reset-values`, and the only thing between that and
# `gray-convergence-commit.sh` was the human-signed `prod_verified` gate. The run
# would commit users onto a build whose chart, values and image digest were never
# recorded anywhere -- the exact state the pin was written to make impossible,
# surviving in the one release that matters most.
#
# Read from workload.env rather than from state, because the release set is not in
# state; verify_generation() has already proven workload.env hashes to
# workload_checksum, so this is reading a verified file, not trusting one.
require_pinned_release() {
  local release="$1" what="$2" dir
  is_test_mode && return 0
  require_workload_binding "$what"
  dir="$(active_dir)"
  [[ -f "$dir/workload.env" ]] || die "$what requires a pinned workload: workload.env is missing"
  # An exact key match. A substring match would let `litellm-product-proxy-old`
  # satisfy a requirement for `litellm-product-proxy`.
  awk -F= -v key="release_${release}_image_digest" '$1 == key { found = 1 } END { exit !found }' \
    "$dir/workload.env" || die \
    "$what requires release '$release' to be pinned: run gray-workload-pin.sh with --release $release (chart package, values and image digest of the build that is actually running), otherwise no gate can tell the approved prod build from a patched one"
}

validate_state_invariants() {
  case "$STATE_PHASE:$STATE_MODE:$STATE_BRIDGE:$STATE_FROZEN" in
    preflight:0:off:0|normal_gray:0:off:0) ;;
    bridge_preparing:0:off:1|bridge_verified:0:guarded-old:1) ;;
    convergence_ready:1:off:1|prod_offline_upgrading:1:off:1|prod_verified:1:off:1) ;;
    aborting_to_bridge:1:guarded-old:1|aborted:1:guarded-old:1) ;;
    committed:0:off:1|rolled_back:0:off:1|post_commit_bridge:0:guarded-old:1|post_commit_prod_verified:0:guarded-old:1|post_commit_rolled_back:0:off:1) ;;
    *) die "state invariant mismatch for phase=$STATE_PHASE mode=$STATE_MODE bridge=$STATE_BRIDGE frozen=$STATE_FROZEN" ;;
  esac
}

load_state() {
  local dir
  dir="$(active_dir)"
  [[ -f "$dir/state.env" ]] || die "state.env is missing"
  STATE_RUN_ID="$(state_get_from "$dir" run_id)" || die "state run_id missing"
  STATE_GENERATION="$(state_get_from "$dir" generation)" || die "state generation missing"
  STATE_PHASE="$(state_get_from "$dir" phase)" || die "state phase missing"
  STATE_MODE="$(state_get_from "$dir" mode)" || die "state mode missing"
  STATE_SPLIT="$(state_get_from "$dir" split)" || die "state split missing"
  STATE_BRIDGE="$(state_get_from "$dir" bridge)" || die "state bridge missing"
  STATE_FROZEN="$(state_get_from "$dir" routing_frozen)" || die "state routing_frozen missing"
  STATE_INPUT_CHECKSUM="$(state_get_from "$dir" input_checksum)" || die "state input_checksum missing"
  # Older generations predate workload pinning and have no such line. Reading
  # that as `unbound` is deliberate: it keeps a pre-2026-09-21 run loadable (so
  # it can still be rolled back) while require_workload_binding() refuses to let
  # it take a forward step.
  STATE_WORKLOAD_CHECKSUM="$(state_get_from "$dir" workload_checksum)" || STATE_WORKLOAD_CHECKSUM="unbound"
  STATE_CONFIG_CHECKSUM="$(state_get_from "$dir" config_checksum)" || die "state config_checksum missing"
  validate_scalar run_id "$STATE_RUN_ID"
  validate_scalar generation "$STATE_GENERATION"
  validate_scalar phase "$STATE_PHASE"
  validate_scalar mode "$STATE_MODE"
  validate_scalar split "$STATE_SPLIT"
  validate_scalar bridge "$STATE_BRIDGE"
  validate_scalar routing_frozen "$STATE_FROZEN"
  validate_scalar input_checksum "$STATE_INPUT_CHECKSUM"
  validate_scalar config_checksum "$STATE_CONFIG_CHECKSUM"
  verify_generation "$dir"
  validate_state_invariants
}

files_checksum() {
  local dir="$1"
  local digest_input
  digest_input="$(for f in protected-prod.map force-prod.map force-gray.map key-sid.map convergence-mode.map bridge-override.map split.conf; do
    secure_file "$dir/$f" "$GRAY_GENERATIONS"
    printf '%s  %s\n' "$(sha256_file "$dir/$f")" "$f"
  done)"
  sha256_text "$digest_input"
}

state_checksum() {
  local run_id="$1" generation="$2" phase="$3" mode="$4" split="$5" bridge="$6" frozen="$7" files="$8" input_checksum="${9:-test-mode}" workload_checksum="${10:-unbound}"
  local payload
  payload="run_id=$run_id|generation=$generation|phase=$phase|mode=$mode|split=$split|bridge=$bridge|routing_frozen=$frozen|files_checksum=$files|input_checksum=$input_checksum|workload_checksum=$workload_checksum"
  sha256_text "$payload"
}

# The K8s half of the frozen artifact, hashed into config_checksum so that a
# mid-run patch cannot hide.
#
# WHY THIS EXISTS.  Until 2026-09-21 config_checksum covered exactly the seven
# nginx route files.  The chart package, the three values files and the image
# digest were frozen only by prose in the run book -- nothing recomputed them.
# So patching a live release (edit values -> helm upgrade -> new content-addressed
# ConfigMap -> rolling restart) left every ruler green: the generation did not
# rotate, verify_generation still passed, and because require_gate_evidence() binds
# evidence to run_id+generation+config_checksum, every gate approved against the
# PRE-patch workload stayed valid against the POST-patch one.  The running build
# was no longer the approved build and no gate could say so.
#
# The fix is not a new discipline layer, it is one more term in the hash that
# already exists.  Recording a patch rotates config_checksum, which invalidates
# every gate evidence file in one step -- so the ramp must re-earn split_sample,
# split_monitor_continuity and (at >=50%) split_capacity, and the sustain streak
# starts from zero because gray-monitor-cycle.sh drops counts across generations.
# "I patched" and "I changed the config on the wire" become the same event,
# because to the users they are.
#
# Absent file == `unbound`, NOT a silent pass: a run whose workload was never
# recorded reads as unbound everywhere and require_workload_binding() fails
# closed on it outside test mode.  An unpinned run is the pre-2026-09-21 state
# and must not look identical to a pinned one.
workload_checksum() {
  local dir="$1"
  if [[ ! -f "$dir/workload.env" ]]; then
    printf 'unbound'
    return 0
  fi
  secure_file "$dir/workload.env" "$GRAY_GENERATIONS"
  sha256_file "$dir/workload.env"
}

input_manifest_checksum() {
  local dir="$1"
  if [[ -f "$dir/input-checksums.env" ]]; then
    secure_file "$dir/input-checksums.env" "$GRAY_GENERATIONS"
    python3 - "$dir" "$(is_test_mode && printf 1 || printf 0)" <<'PY'
import hashlib
import os
import re
import sys

root, test_mode = sys.argv[1], sys.argv[2] == "1"
manifest = os.path.join(root, "input-checksums.env")
lines = open(manifest, encoding="utf-8").read().splitlines()
snapshots = {"execution_plan": "execution-plan.snapshot", "live_summary": "live-summary.snapshot"}
render_inputs = {
    "render_command", "render_base_template", "render_base_template_path",
    "render_debug_token", "render_debug_token_path", "renderer_identity",
    "renderer_path", "render_output",
}
routing_contract_inputs = {
    "nginx_test_command", "reload_command", "post_reload_command",
    "initial_rollback_command", "abort_verify_command", "gate_max_age_seconds",
}
allowed = set(snapshots) | render_inputs | routing_contract_inputs
seen = set()
if lines == ["test_mode=1"]:
    if not test_mode:
        raise SystemExit("gray-rollout: test-only input manifest outside test mode")
else:
    if not lines:
        raise SystemExit("gray-rollout: frozen input manifest is empty")
    for line in lines:
        if "=" not in line:
            raise SystemExit("gray-rollout: malformed frozen input manifest")
        key, expected = line.split("=", 1)
        if (key not in allowed and not re.fullmatch(r"hook_file_[0-9a-f]{16}", key)) or key in seen or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise SystemExit("gray-rollout: invalid frozen input manifest entry")
        seen.add(key)
        if key in snapshots:
            snapshot = os.path.join(root, snapshots[key])
            try:
                actual = hashlib.sha256(open(snapshot, "rb").read()).hexdigest()
            except OSError as exc:
                raise SystemExit(f"gray-rollout: frozen input snapshot missing: {exc}")
            if actual != expected:
                raise SystemExit(f"gray-rollout: frozen input checksum mismatch for {key}")
    if not test_mode:
        required = set(snapshots) | render_inputs | routing_contract_inputs
        if not required.issubset(seen):
            missing = ",".join(sorted(required - seen))
            extra = ",".join(sorted(key for key in seen - required if not key.startswith("hook_file_")))
            raise SystemExit(f"gray-rollout: incomplete frozen input manifest missing={missing} extra={extra}")
        elif any(not key.startswith("hook_file_") for key in seen - required):
            extra = ",".join(sorted(key for key in seen - required if not key.startswith("hook_file_")))
            raise SystemExit(f"gray-rollout: incomplete frozen input manifest missing={missing} extra={extra}")
print(hashlib.sha256(open(manifest, "rb").read()).hexdigest())
PY
  elif is_test_mode; then
    printf 'test-mode'
  else
    die "frozen input manifest is missing"
  fi
}

write_state() {
  local dir="$1" run_id="$2" generation="$3" phase="$4" mode="$5" split="$6" bridge="$7" frozen="$8"
  local files checksum input_checksum workload
  files="$(files_checksum "$dir")"
  input_checksum="$(input_manifest_checksum "$dir")"
  workload="$(workload_checksum "$dir")"
  checksum="$(state_checksum "$run_id" "$generation" "$phase" "$mode" "$split" "$bridge" "$frozen" "$files" "$input_checksum" "$workload")"
  local state_tmp sums_tmp
  state_tmp="$(mktemp "$dir/.state.env.XXXXXX")"
  sums_tmp="$(mktemp "$dir/.SHA256SUMS.XXXXXX")"
  cat >"$state_tmp" <<EOF
run_id=$run_id
generation=$generation
phase=$phase
mode=$mode
split=$split
bridge=$bridge
routing_frozen=$frozen
input_checksum=$input_checksum
workload_checksum=$workload
config_checksum=$checksum
EOF
  chmod 600 "$state_tmp"
  mv -f "$state_tmp" "$dir/state.env"
  for f in protected-prod.map force-prod.map force-gray.map key-sid.map convergence-mode.map bridge-override.map split.conf; do
    printf '%s  %s\n' "$(sha256_file "$dir/$f")" "$f"
  done >"$sums_tmp"
  chmod 600 "$sums_tmp"
  mv -f "$sums_tmp" "$dir/SHA256SUMS"
}

render_fragments() {
  local dir="$1" mode="$2" split="$3" bridge="$4"
  if [[ "$mode" == "1" ]]; then
    printf 'default 1;\n' >"$dir/convergence-mode.map"
  else
    printf 'default 0;\n' >"$dir/convergence-mode.map"
  fi
  if [[ "$bridge" == "guarded-old" ]]; then
    printf 'default guarded-old;\n' >"$dir/bridge-override.map"
  else
    printf 'default off;\n' >"$dir/bridge-override.map"
  fi
  {
    if [[ "$split" == "100" ]]; then
      printf '* litellm_gray;\n'
    elif [[ "$split" -gt 0 ]]; then
      printf '%s%% litellm_gray;\n' "$split"
      printf '* litellm_product;\n'
    else
      printf '* litellm_product;\n'
    fi
  } >"$dir/split.conf"
  chmod 600 "$dir/convergence-mode.map" "$dir/bridge-override.map" "$dir/split.conf"
}

verify_generation() {
  local dir="$1" actual expected files actual_input state_input expected_sums actual_workload state_workload
  secure_dir "$dir" "$GRAY_GENERATIONS"
  for f in protected-prod.map force-prod.map force-gray.map key-sid.map convergence-mode.map bridge-override.map split.conf state.env SHA256SUMS input-checksums.env; do
    secure_file "$dir/$f" "$GRAY_GENERATIONS"
  done
  if [[ -f "$dir/execution-plan.snapshot" ]]; then secure_file "$dir/execution-plan.snapshot" "$GRAY_GENERATIONS"; fi
  if [[ -f "$dir/live-summary.snapshot" ]]; then secure_file "$dir/live-summary.snapshot" "$GRAY_GENERATIONS"; fi
  actual_input="$(input_manifest_checksum "$dir")"
  state_input="$(state_get_from "$dir" input_checksum)" || die "state input checksum missing"
  [[ "$actual_input" == "$state_input" ]] || die "frozen input manifest checksum mismatch"
  # The workload half. Recomputed from workload.env on disk, so editing that file
  # without going through gray-workload-pin.sh fails here rather than passing
  # quietly -- the same treatment the route maps already get.
  actual_workload="$(workload_checksum "$dir")"
  state_workload="$(state_get_from "$dir" workload_checksum)" || state_workload="unbound"
  [[ "$actual_workload" == "$state_workload" ]] || die "workload checksum mismatch: workload.env changed outside a generation transition"
  files="$(files_checksum "$dir")"
  actual="$(state_checksum \
    "$(state_get_from "$dir" run_id)" \
    "$(state_get_from "$dir" generation)" \
    "$(state_get_from "$dir" phase)" \
    "$(state_get_from "$dir" mode)" \
    "$(state_get_from "$dir" split)" \
    "$(state_get_from "$dir" bridge)" \
    "$(state_get_from "$dir" routing_frozen)" \
    "$files" \
    "$actual_input" \
    "$actual_workload")"
  expected="$(state_get_from "$dir" config_checksum)" || die "config checksum missing"
  [[ "$actual" == "$expected" ]] || die "generation checksum mismatch"
  expected_sums="$(for f in protected-prod.map force-prod.map force-gray.map key-sid.map convergence-mode.map bridge-override.map split.conf; do printf '%s  %s\n' "$(sha256_file "$dir/$f")" "$f"; done)"
  [[ "$(cat "$dir/SHA256SUMS")" == "$expected_sums" ]] || die "SHA256SUMS mismatch"
  verify_maps "$dir"
}

verify_maps() {
  local dir="$1" a b c
  a="$(map_keys "$dir/protected-prod.map")"
  b="$(map_keys "$dir/force-prod.map")"
  c="$(map_keys "$dir/force-gray.map")"
  [[ -z "$(comm -12 <(printf '%s\n' "$a" | sort -u) <(printf '%s\n' "$b" | sort -u))" ]] || die "protected-prod/force-prod intersection"
  [[ -z "$(comm -12 <(printf '%s\n' "$a" | sort -u) <(printf '%s\n' "$c" | sort -u))" ]] || die "protected-prod/force-gray intersection"
  [[ -z "$(comm -12 <(printf '%s\n' "$b" | sort -u) <(printf '%s\n' "$c" | sort -u))" ]] || die "force-prod/force-gray intersection"
}

map_keys() {
  local file="$1"
  [[ -f "$file" ]] || die "map missing: $file"
  awk '/^[[:space:]]*"[^"]+"[[:space:]]+1;[[:space:]]*$/ { sub(/^[[:space:]]*"/, ""); sub(/"[[:space:]]+1;[[:space:]]*$/, ""); print }' "$file"
}

map_has() {
  local file="$1" key="$2"
  awk -v wanted="$key" '/^[[:space:]]*"[^"]+"[[:space:]]+1;[[:space:]]*$/ { line=$0; sub(/^[[:space:]]*"/, "", line); sub(/"[[:space:]]+1;[[:space:]]*$/, "", line); if (line == wanted) found=1 } END { exit(found ? 0 : 1) }' "$file"
}

sid_for_key() {
  local key="$1"
  printf '%s' "$(sha256_text "$key")" | cut -c1-12
}

valid_key() {
  local key="$1" tail
  [[ "$key" == sk-* ]] || return 1
  tail="${key#sk-}"
  [[ ${#tail} -ge 8 && ${#tail} -le 512 ]] || return 1
  [[ "$tail" != *[!A-Za-z0-9._~-]* ]]
}

write_key_map() {
  local file="$1" key="$2" sid_file="${3:-}"
  {
    map_keys "$file"
    printf '%s\n' "$key"
  } | awk 'NF' | sort -u | awk '{ printf "\"%s\" 1;\n", $0 }' >"$file.tmp"
  chmod 600 "$file.tmp"
  mv -f "$file.tmp" "$file"
  if [[ -n "$sid_file" ]]; then
    local sid hex12
    sid="$(sid_for_key "$key")"
    # ⚠️ 不要把下面这段写回 `[0-9a-f]{12}`。Ubuntu 的默认 awk 是 **mawk**
    # （198 上实测 mawk 1.3.4 20200120），它**不支持 ERE 区间量词**，而且
    # **不报错**——只是一行都不匹配。后果是 key-sid.map 被写成空文件，
    # nginx 的 `map $canonical_key $key_sid` 全部落到 default `-`，静默失效。
    # 2026-09-14 实测：同一份脚本在 mac（BWK awk，支持区间）绿，在 198 上
    # test_convergence_commit_preserves_protected_key_sid 红，差异只在 awk 实现。
    # `test_scripts_never_use_brace_intervals_in_awk` 会在有人写回去时报红。
    hex12='[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    { cat "$sid_file" 2>/dev/null || true; printf '"%s" %s;\n' "$key" "$sid"; } |
      awk -v hex12="$hex12" '$0 ~ "^[[:space:]]*\"[^\"]+\"[[:space:]]+" hex12 ";[[:space:]]*$" { print }' |
      sort -u >"$sid_file.tmp"
    chmod 600 "$sid_file.tmp"
    mv -f "$sid_file.tmp" "$sid_file"
  fi
}

remove_key_from_map() {
  local file="$1" key="$2"
  awk -v wanted="$key" '/^[[:space:]]*"[^"]+"/ { line=$0; cmp=line; sub(/^[[:space:]]*"/, "", cmp); sub(/"[[:space:]]+[^;]+;[[:space:]]*$/, "", cmp); if (cmp == wanted) next } { print }' "$file" >"$file.tmp"
  chmod 600 "$file.tmp"
  mv -f "$file.tmp" "$file"
}

stage_from_active() {
  local old stage id
  old="$(active_dir)"
  verify_frozen_routing_contract "$old"
  id="g$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM:-0}"
  stage="$GRAY_GENERATIONS/$id"
  (umask 077; mkdir "$stage"; cp -p "$old"/* "$stage"/; chmod 700 "$stage")
  # These globals are consumed by the calling transaction entrypoint.
  # shellcheck disable=SC2034
  STAGE_DIR="$stage"
  # shellcheck disable=SC2034
  OLD_ACTIVE_DIR="$old"
}

switch_active() {
  local target="$1" tmp="$GRAY_ROOT/.active.$$"
  [[ "$target" = /* ]] || target="$(cd "$target" && pwd -P)"
  secure_dir "$target" "$GRAY_GENERATIONS"
  ln -s "$target" "$tmp"
  python3 - "$tmp" "$GRAY_ACTIVE" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
PY
}

run_optional() {
  local command_text="$1"
  [[ -n "$command_text" ]] || return 0
  bash -c "$command_text"
}

nginx_test() {
  if [[ -z "${GRAY_NGINX_TEST_CMD:-}" ]]; then
    is_test_mode && return 0
    die "GRAY_NGINX_TEST_CMD is required outside test mode"
  fi
  run_optional "$GRAY_NGINX_TEST_CMD"
}

nginx_reload() {
  if [[ -z "${GRAY_RELOAD_CMD:-}" ]]; then
    is_test_mode && return 0
    die "GRAY_RELOAD_CMD is required outside test mode"
  fi
  run_optional "$GRAY_RELOAD_CMD"
}

post_reload_check() {
  if [[ -z "${GRAY_POST_RELOAD_CMD:-}" ]]; then
    is_test_mode && return 0
    die "GRAY_POST_RELOAD_CMD is required outside test mode"
  fi
  run_optional "$GRAY_POST_RELOAD_CMD"
}

render_candidate() {
  local generation="$1"
  if [[ -z "${GRAY_RENDER_CMD:-}" ]]; then
    is_test_mode && return 0
    die "GRAY_RENDER_CMD is required outside test mode"
  fi
  if is_test_mode; then
    (
      # shellcheck disable=SC2030
      export GRAY_GENERATION_DIR="$generation"
      run_optional "$GRAY_RENDER_CMD"
    )
    return
  fi
  verify_frozen_renderer_inputs "$generation"
  rm -f "$generation/render-attestation.json"
  (
    # shellcheck disable=SC2031
    export GRAY_GENERATION_DIR="$generation"
    export GRAY_RENDER_ATTESTATION_FILE="$generation/render-attestation.json"
    run_optional "$GRAY_RENDER_CMD"
  )
  verify_render_attestation "$generation"
}

verify_render_attestation() {
  local generation="$1" attestation="$1/render-attestation.json"
  is_test_mode && return 0
  verify_frozen_renderer_inputs "$generation"
  secure_file "$attestation" "$GRAY_GENERATIONS"
  python3 - "$attestation" "$generation" "$GRAY_RENDER_BASE_TEMPLATE" "$GRAY_RENDER_DEBUG_TOKEN_FILE" "$GRAY_RENDERER_FILE" "$GRAY_RENDER_OUTPUT" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys

attestation, generation, base, token, renderer, output = sys.argv[1:]

def sha(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()

def canonical(path):
    return os.path.realpath(os.path.abspath(path))

try:
    output_info = os.lstat(output)
    if stat.S_ISLNK(output_info.st_mode) or not stat.S_ISREG(output_info.st_mode):
        raise ValueError("render output is not a regular non-symlink file")
    if stat.S_IMODE(output_info.st_mode) != 0o600:
        raise ValueError("render output mode is not 0600")
    with open(attestation, encoding="utf-8") as handle:
        obj = json.load(handle)
    if obj.get("tool") != "render-production-nginx-attestation" or obj.get("schema_version") != 1 or obj.get("status") != "PASS":
        raise ValueError("attestation contract mismatch")
    state_values = {}
    with open(os.path.join(generation, "state.env"), encoding="utf-8") as handle:
        for line in handle:
            key, separator, value = line.rstrip("\n").partition("=")
            if separator:
                state_values[key] = value
    checksum = state_values.get("config_checksum", "")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("generation config checksum is invalid")
    expected = {
        "generation": canonical(generation),
        "generation_config_checksum": checksum,
        "base_template": canonical(base),
        "base_template_sha256": sha(base),
        "debug_token_file": canonical(token),
        "debug_token_sha256": sha(token),
        "renderer": canonical(renderer),
        "renderer_sha256": sha(renderer),
        "output": canonical(output),
        "output_sha256": sha(output),
    }
    for key, value in expected.items():
        if obj.get(key) != value:
            raise ValueError(f"{key} mismatch")
except Exception as exc:
    print(f"gray-rollout: invalid render attestation: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

# Which tool is allowed to produce each gate's evidence, for the gates that have
# one. Empty means "no producer exists": that gate is a human attestation and must
# carry a signer instead. The mapping is deliberately a whitelist in code rather
# than a sentence in the manual -- `feedback_gate_property_in_prose_is_not_in_the_code`
# is exactly the failure where the documented property was never checked anywhere.
#
# Adding a producer for a human-signed gate means adding its line here and moving
# its row in manual section 6.2.3. Both halves, or the classification lies again.
gate_expected_producer() {
  case "$1" in
    split_monitor_continuity) printf 'check-monitor-continuity' ;;
    split_capacity) printf 'check-split-capacity' ;;
    *) printf '' ;;
  esac
}

require_gate_evidence() {
  local gate="$1" legacy_var="${2:-}" file env_name
  env_name="GRAY_$(printf '%s' "$gate" | tr '[:lower:]-' '[:upper:]_')_OK"
  if is_test_mode; then
    if [[ -n "$legacy_var" ]]; then
      [[ "${!legacy_var:-0}" == "1" ]] || die "$gate gate is not approved"
    else
      [[ "${!env_name:-0}" == "1" ]] || die "$gate gate is not approved"
    fi
    return 0
  fi
  verify_frozen_gate_max_age "$(active_dir)"
  file="${GRAY_GATE_EVIDENCE_FILE:-$GRAY_GATE_EVIDENCE_DIR/$gate.json}"
  secure_file "$file" "$GRAY_GATE_EVIDENCE_DIR" || die "$gate gate evidence is untrusted: $file"
  python3 - "$file" "$gate" "$STATE_RUN_ID" "$STATE_GENERATION" "$STATE_CONFIG_CHECKSUM" "$GRAY_GATE_MAX_AGE_SECONDS" "$(expected_owner_uid)" "$(gate_expected_producer "$gate")" <<'PY'
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys

path, gate, run_id, generation, checksum, max_age, expected_uid, producer = sys.argv[1:]

# A signer is a person who can be asked "why did you sign this?". These are the
# strings that look like a signature and name nobody: a template left unfilled, a
# role, or the machine's own idea of who was at the keyboard. Sudo makes $USER
# `root` for everyone, so `root` names no one either.
SIGNER_RE = re.compile(r"^[A-Za-z0-9一-鿿][A-Za-z0-9一-鿿 ._@-]{1,63}$")
PLACEHOLDER_SIGNERS = {
    "fill_me", "fill-me", "fillme", "tbd", "todo", "n/a", "na", "none", "null",
    "unknown", "operator", "admin", "root", "me", "self", "someone", "anyone",
    "xxx", "xx", "test", "sudo_user", "user",
}


def canonical(obj):
    """Exactly what the producers hash: every key but captured_at and the hash itself."""
    body = {key: value for key, value in obj.items() if key not in ("captured_at", "result_sha256")}
    rendered = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()


try:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != int(expected_uid) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("untrusted file metadata")
    with os.fdopen(fd, encoding="utf-8") as handle:
        obj = json.load(handle)
    if not isinstance(obj, dict):
        raise ValueError("evidence must be an object")
    if obj.get("gate") != gate or obj.get("status") != "PASS":
        raise ValueError("gate/status mismatch")
    for key, expected in (("run_id", run_id), ("generation", generation), ("config_checksum", checksum)):
        if obj.get(key) != expected:
            raise ValueError(f"{key} mismatch")
    stamp = dt.datetime.fromisoformat(str(obj.get("captured_at")).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("captured_at must include timezone")
    age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
    if age < -60 or age > int(max_age):
        raise ValueError("evidence is stale")

    # A PASS that also lists errors is not a verdict, it is two verdicts. Until
    # 2026-09-21 nothing looked past `status`, so a tool run that FAILED and was
    # then hand-edited to PASS carried its own reason codes through the gate.
    errors = obj.get("errors")
    if errors not in (None, []):
        raise ValueError(f"status is PASS but errors is non-empty: {errors!r}")

    tool = obj.get("tool")
    if producer:
        # A measured gate may not be hand-written. This is the `split_capacity`
        # failure: the gate was enforced for weeks while its evidence was a human
        # typing "status": "PASS", which proves a human typed it.
        if tool != producer:
            raise ValueError(
                f"{gate} is a measured gate: evidence must come from {producer}.py, "
                f"got tool={tool!r}"
            )
        if obj.get("schema_version") != 1:
            raise ValueError(f"schema_version must be 1, got {obj.get('schema_version')!r}")
        # Recomputed, so editing any field after the tool wrote it reds here
        # rather than riding through on a shape that still looks right.
        if obj.get("result_sha256") != canonical(obj):
            raise ValueError("result_sha256 does not match the evidence body (edited after capture)")
    else:
        if tool is not None:
            raise ValueError(
                f"{gate} has no producer, so evidence must not claim tool={tool!r}; "
                "human attestations carry a signer instead"
            )
        # The manual says human-signed gates pin an irreversible action to a named
        # person and a moment. captured_at was always the moment; until now nothing
        # recorded the person, so the claim was prose with no code behind it.
        signer = obj.get("signer")
        if not isinstance(signer, str) or not SIGNER_RE.fullmatch(signer):
            raise ValueError(
                f"{gate} is a human attestation and needs a real \"signer\": got {signer!r}"
            )
        if signer.strip().lower().replace(" ", "_") in PLACEHOLDER_SIGNERS:
            raise ValueError(f"signer {signer!r} names nobody who can be asked why they signed")
except Exception as exc:
    print(f"gray-rollout: invalid {gate} gate evidence: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

require_metrics_evidence() {
  local file="$1" expected_phase="${2:-$STATE_PHASE}"
  verify_frozen_gate_max_age "$(active_dir)"
  secure_file "$file" "$GRAY_GATE_EVIDENCE_DIR" || die "metrics evidence is untrusted: $file"
  python3 - "$file" "$STATE_RUN_ID" "$STATE_GENERATION" "$STATE_CONFIG_CHECKSUM" "$expected_phase" "$GRAY_GATE_MAX_AGE_SECONDS" "$(expected_owner_uid)" <<'PY'
import datetime as dt
import hashlib
import json
import os
import stat
import sys

path, run_id, generation, checksum, phase, max_age, expected_uid = sys.argv[1:]
try:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != int(expected_uid) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("untrusted file metadata")
    with os.fdopen(fd, encoding="utf-8") as handle:
        obj = json.load(handle)
    if not isinstance(obj, dict):
        raise ValueError("evidence must be an object")
    if obj.get("tool") != "metrics" or obj.get("schema_version") != 1:
        raise ValueError("not a metrics v1 result")
    payload_checksum = obj.get("payload_sha256")
    canonical = {key: value for key, value in obj.items() if key != "payload_sha256"}
    rendered = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    actual_checksum = "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()
    if payload_checksum != actual_checksum:
        raise ValueError("metrics payload checksum mismatch")
    for key, expected in (("run_id", run_id), ("generation", generation), ("config_checksum", checksum), ("phase", phase)):
        if obj.get(key) != expected:
            raise ValueError(f"{key} mismatch")
    rec = obj.get("dispatcher_recommendation")
    if not isinstance(rec, dict):
        raise ValueError("dispatcher recommendation missing")
    action, hard = rec.get("action"), rec.get("hard_trigger")
    status = obj.get("status")
    if status not in {"PASS", "FAIL"} or not isinstance(hard, bool):
        raise ValueError("metrics status is not dispatchable")
    if action in {"rollback", "abort_to_bridge"}:
        if status != "FAIL" or hard is not True:
            raise ValueError("unsafe mutating recommendation")
        if action == "abort_to_bridge":
            health = obj.get("backend_health")
            if not isinstance(health, dict) or health.get("gray") is not False or health.get("bridge") is not True:
                raise ValueError("abort requires confirmed gray failure and healthy bridge")
    elif action == "alert_only" and status == "FAIL" and hard is True:
        pass
    elif action in {"none", "hold_gray", "alert_only"}:
        if status != "PASS" or hard is not False:
            raise ValueError("invalid non-mutating recommendation")
    else:
        raise ValueError("unknown dispatcher action")
    stamp = dt.datetime.fromisoformat(str(obj.get("captured_at")).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("captured_at must include timezone")
    age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
    if age < -60 or age > int(max_age):
        raise ValueError("evidence is stale")
except Exception as exc:
    print(f"gray-rollout: invalid metrics evidence: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

require_dispatch_authorization() {
  local file="$1" expected_action="$2"
  require_metrics_evidence "$file"
  python3 - "$file" "$expected_action" <<'PY'
import json
import sys

path, expected_action = sys.argv[1:]
try:
    obj = json.load(open(path, encoding="utf-8"))
    rec = obj.get("dispatcher_recommendation")
    if not isinstance(rec, dict):
        raise ValueError("dispatcher recommendation missing")
    if rec.get("action") != expected_action or rec.get("hard_trigger") is not True:
        raise ValueError("dispatcher action mismatch")
    if expected_action == "abort_to_bridge":
        health = obj.get("backend_health")
        if not isinstance(health, dict) or health.get("gray") is not False or health.get("bridge") is not True:
            raise ValueError("abort health authorization mismatch")
except Exception as exc:
    print(f"gray-rollout: invalid dispatcher authorization: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

commit_stage() {
  local stage="$1" old="$2"
  verify_generation "$stage"
  render_candidate "$stage" || { rm -rf "$stage"; die "candidate render failed; active generation unchanged"; }
  nginx_test || { rm -rf "$stage"; die "nginx -t failed; active generation unchanged"; }
  verify_render_attestation "$stage" || { rm -rf "$stage"; die "render output drifted during nginx -t; active generation unchanged"; }
  write_transaction_journal generation "$old" "$stage" switch_pending
  switch_active "$stage"
  write_transaction_journal generation "$old" "$stage" active_switched
  if ! nginx_test || ! verify_render_attestation "$stage" || ! nginx_reload || ! post_reload_check; then
    switch_active "$old"
    render_candidate "$old" >/dev/null 2>&1 || die "candidate failed; active symlink restored but old generation render failed"
    nginx_test >/dev/null 2>&1 || die "candidate failed and restored generation does not pass nginx -t"
    verify_render_attestation "$old" >/dev/null 2>&1 || die "candidate failed and restored render output drifted during nginx -t"
    nginx_reload >/dev/null 2>&1 || die "candidate failed; active symlink restored but rollback reload failed"
    post_reload_check >/dev/null 2>&1 || die "candidate failed; rollback reload ran but restored worker/probe check failed"
    die "reload/worker check failed; active generation restored"
  fi
  write_transaction_journal generation "$old" "$stage" reloaded
  clear_transaction_journal
  GRAY_TXN_COMMITTED=1
  printf 'generation=%s\n' "$(basename "$stage")"
}

phase_allowed() {
  local from="$1" to="$2"
  case "$from:$to" in
    preflight:bridge_preparing|bridge_preparing:bridge_verified|bridge_verified:preflight|preflight:normal_gray|normal_gray:convergence_ready|convergence_ready:prod_offline_upgrading|prod_offline_upgrading:prod_verified|prod_verified:committed|normal_gray:rolled_back|convergence_ready:rolled_back|prod_offline_upgrading:aborting_to_bridge|prod_verified:aborting_to_bridge|aborting_to_bridge:aborted|committed:post_commit_bridge|post_commit_bridge:post_commit_prod_verified|post_commit_prod_verified:post_commit_rolled_back) return 0 ;;
    *) return 1 ;;
  esac
}

set_phase_in_dir() {
  local dir="$1" phase="$2"
  local run generation mode split bridge frozen
  run="$(state_get_from "$dir" run_id)"; generation="$(basename "$dir")"
  mode="$(state_get_from "$dir" mode)"; split="$(state_get_from "$dir" split)"
  bridge="$(state_get_from "$dir" bridge)"; frozen="$(state_get_from "$dir" routing_frozen)"
  render_fragments "$dir" "$mode" "$split" "$bridge"
  write_state "$dir" "$run" "$generation" "$phase" "$mode" "$split" "$bridge" "$frozen"
}

empty_map() {
  : >"$1"
  chmod 600 "$1"
}

require_phase() {
  local expected="$1"
  load_state
  [[ "$STATE_PHASE" == "$expected" ]] || die "phase=$STATE_PHASE; expected $expected"
}

require_any_phase() {
  local p
  load_state
  for p in "$@"; do [[ "$STATE_PHASE" == "$p" ]] && return 0; done
  die "phase=$STATE_PHASE is not allowed"
}
