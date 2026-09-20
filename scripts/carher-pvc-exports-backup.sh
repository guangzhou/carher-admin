#!/usr/bin/env bash
# Safely archive a Her PVC's disposable workspace content.
# Shared-NAS moves are the default because every ACK worker mounts the same /Data.

set -euo pipefail

UID_VALUE=""
PV=""
WORKER="k8s-work-226"
BACKUP_HOST="laoyang"
BACKUP_ROOT="/Data/_backups/carher-pvc-backups"
BACKUP_MODE="shared-nas"
RUN=""
PORT=""
ACTION=""
STATE_DIR=""
WORKER_IP="172.16.0.226"
INCLUDE_PATHS="workspace/exports"

usage() {
  cat <<'EOF'
Usage:
  scripts/carher-pvc-exports-backup.sh --inspect --uid UID --pv PV [options]
  scripts/carher-pvc-exports-backup.sh --start --uid UID --pv PV [options]
  scripts/carher-pvc-exports-backup.sh --status --state-dir DIR
  scripts/carher-pvc-exports-backup.sh --finalize --state-dir DIR

Options:
  --worker ASSET        NAS worker JumpServer asset (default: k8s-work-226)
  --backup-root PATH    ACK shared-NAS backup root
                        (default: /Data/_backups/carher-pvc-backups)
  --offhost             explicitly use the legacy cross-host transfer to laoyang
  --backup-host ASSET   off-host destination asset (default: laoyang)
  --worker-ip IP        worker IP reachable from the backup host (default: 172.16.0.226)
  --port PORT           temporary TCP port; default is derived from the uid
  --run NAME            stable run name; default includes UID and timestamp
  --include PATHS       comma-separated safe roots: workspace/exports,
                        workspace/temp,workspace/tmp,workspace/artifacts
                        (default: workspace/exports)

The default shared-NAS mode uses an atomic rename inside /Data, so it avoids
scp/nc/rsync and does not copy bytes between ACK nodes. It recreates empty source
directories after the move. User project directories, memory, sessions and
configuration cannot be selected.

Use --offhost only when the user explicitly requests an independent copy or when
shared-NAS quota accounting has been proven not to clear the alert.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inspect|--start|--status|--finalize) ACTION="${1#--}" ;;
    --uid) UID_VALUE="${2:?}"; shift ;;
    --pv) PV="${2:?}"; shift ;;
    --worker) WORKER="${2:?}"; shift ;;
    --backup-host) BACKUP_HOST="${2:?}"; shift ;;
    --backup-root) BACKUP_ROOT="${2:?}"; shift ;;
    --offhost)
      BACKUP_MODE="offhost"
      BACKUP_ROOT="/Data/carher-pvc-backups"
      ;;
    --worker-ip) WORKER_IP="${2:?}"; shift ;;
    --port) PORT="${2:?}"; shift ;;
    --run) RUN="${2:?}"; shift ;;
    --include) INCLUDE_PATHS="${2:?}"; shift ;;
    --state-dir) STATE_DIR="${2:?}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

[[ -n "$ACTION" ]] || { usage >&2; exit 2; }
command -v scripts/jms >/dev/null || die "run this from the carher-admin repository"

remote() {
  local asset="$1"
  local command="$2"
  scripts/jms ssh --tty --timeout 45 "$asset" "$command"
}

validate_include_paths() {
  local item
  IFS=',' read -r -a INCLUDE_ARRAY <<< "$INCLUDE_PATHS"
  ((${#INCLUDE_ARRAY[@]})) || die "--include cannot be empty"
  for item in "${INCLUDE_ARRAY[@]}"; do
    case "$item" in
      workspace/exports|workspace/temp|workspace/tmp|workspace/artifacts) ;;
      *) die "unsafe --include path refused: $item" ;;
    esac
  done
}

resolve_pv() {
  [[ "$UID_VALUE" =~ ^[0-9]+$ ]] || die "--uid must be numeric"
  if [[ -z "$PV" ]]; then
    PV=$(kubectl --kubeconfig ~/.kube/config --request-timeout=20s -n carher \
      get pvc "carher-${UID_VALUE}-data" -o jsonpath='{.spec.volumeName}') ||
      die "PV lookup failed; pass --pv explicitly when ACK access is unavailable"
  fi
  [[ "$PV" =~ ^nas-[A-Za-z0-9-]+$ ]] || die "--pv must be a NAS PV name"
}

inspect() {
  resolve_pv
  validate_include_paths
  local roots="" remote_root
  local item
  for item in "${INCLUDE_ARRAY[@]}"; do
    printf -v remote_root '%q' "\$base/$item"
    roots+=" $remote_root"
  done
  remote "$WORKER" "set -eu; base=/Data/$PV; echo PVC=carher-$UID_VALUE-data PV=$PV; du -sk \"\$base\"; for p in$roots; do [ -e \"\$p\" ] && du -sk \"\$p\"; done; echo 'Largest workspace entries (not automatically eligible):'; du -sk \"\$base/workspace\"/* 2>/dev/null | sort -nr | head -20 || true"
}

load_state() {
  [[ -n "$STATE_DIR" && -f "$STATE_DIR/state.env" ]] || die "--state-dir must contain state.env"
  # state.env is created locally by this script and contains only shell-quoted literals.
  # shellcheck disable=SC1090
  source "$STATE_DIR/state.env"
}

shared_start() {
  resolve_pv
  validate_include_paths
  [[ -n "$RUN" ]] || RUN="carher-${UID_VALUE}-workspace-$(date +%Y%m%d-%H%M%S)"
  [[ "$RUN" =~ ^[A-Za-z0-9._-]+$ ]] || die "--run has unsupported characters"
  [[ "$BACKUP_ROOT" == /Data/* ]] || die "shared-NAS backup root must be under /Data"

  STATE_DIR="/tmp/$RUN"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  cat > "$STATE_DIR/state.env" <<EOF
UID_VALUE=$(printf '%q' "$UID_VALUE")
PV=$(printf '%q' "$PV")
WORKER=$(printf '%q' "$WORKER")
BACKUP_ROOT=$(printf '%q' "$BACKUP_ROOT")
BACKUP_DIR=$(printf '%q' "$BACKUP_ROOT/$RUN")
RUN=$(printf '%q' "$RUN")
INCLUDE_PATHS=$(printf '%q' "$INCLUDE_PATHS")
BACKUP_MODE=shared-nas
EOF

  local command
  command=$(cat <<EOF
set -euo pipefail
base=/Data/$PV
backup=$BACKUP_ROOT/$RUN
test ! -e "\$backup"
mkdir -p "\$backup"
printf '%s\n' '$INCLUDE_PATHS' | tr ',' '\n' > "\$backup/include.roots"
: > "\$backup/moved.tsv"
while IFS= read -r root; do
  src="\$base/\$root"
  [ -d "\$src" ] || continue
  size_kb=\$(du -sk "\$src" | awk '{print \$1}')
  files=\$(find "\$src" -type f | wc -l)
  dest="\$backup/\$root"
  mkdir -p "\$(dirname "\$dest")"
  mv "\$src" "\$dest"
  mkdir -p "\$src"
  printf '%s\t%s\t%s\n' "\$root" "\$size_kb" "\$files" >> "\$backup/moved.tsv"
done < "\$backup/include.roots"
test -s "\$backup/moved.tsv" || { rmdir "\$backup"; echo 'no eligible roots found' >&2; exit 3; }
sync
touch "\$backup/move.done"
cat "\$backup/moved.tsv"
echo "SHARED_NAS_MOVE_COMPLETE backup=\$backup"
EOF
)
  remote "$WORKER" "$command"
  echo "STATE_DIR=$STATE_DIR"
}

shared_status() {
  load_state
  remote "$WORKER" "set -eu; backup=$BACKUP_DIR; echo BACKUP=\$backup; ls -l \"\$backup/move.done\" \"\$backup/moved.tsv\"; cat \"\$backup/moved.tsv\"; du -sh \"\$backup\"; du -sh /Data/$PV"
}

shared_finalize() {
  load_state
  remote "$WORKER" "set -euo pipefail; backup=$BACKUP_DIR; test -f \"\$backup/move.done\"; while IFS=\$'\\t' read -r root expected_kb expected_files; do actual_kb=\$(du -sk \"\$backup/\$root\" | awk '{print \$1}'); actual_files=\$(find \"\$backup/\$root\" -type f | wc -l); test \"\$actual_kb\" = \"\$expected_kb\"; test \"\$actual_files\" = \"\$expected_files\"; test -d /Data/$PV/\$root; done < \"\$backup/moved.tsv\"; touch \"\$backup/verified.done\"; echo VERIFIED_SHARED_NAS_BACKUP=\$backup"
}

start() {
  if [[ "$BACKUP_MODE" == "shared-nas" ]]; then
    shared_start
    return
  fi
  resolve_pv
  validate_include_paths
  [[ -n "$RUN" ]] || RUN="carher-${UID_VALUE}-exports-$(date +%Y%m%d-%H%M%S)"
  [[ "$RUN" =~ ^[A-Za-z0-9._-]+$ ]] || die "--run has unsupported characters"
  [[ -n "$PORT" ]] || PORT="$((32000 + UID_VALUE % 1000))"
  [[ "$PORT" =~ ^[0-9]{4,5}$ ]] || die "--port must be a TCP port"

  # Keep the state stable across interactive shells and macOS TMPDIR changes.
  STATE_DIR="/tmp/$RUN"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  cat > "$STATE_DIR/state.env" <<EOF
UID_VALUE=$(printf '%q' "$UID_VALUE")
PV=$(printf '%q' "$PV")
WORKER=$(printf '%q' "$WORKER")
BACKUP_HOST=$(printf '%q' "$BACKUP_HOST")
BACKUP_ROOT=$(printf '%q' "$BACKUP_ROOT")
RUN=$(printf '%q' "$RUN")
PORT=$(printf '%q' "$PORT")
WORKER_STATE=$(printf '%q' "/tmp/$RUN")
BACKUP_DIR=$(printf '%q' "$BACKUP_ROOT/$RUN")
WORKER_IP=$(printf '%q' "$WORKER_IP")
INCLUDE_PATHS=$(printf '%q' "$INCLUDE_PATHS")
EOF

  local source_command
  source_command=$(cat <<EOF
set -euo pipefail
base=/Data/$PV
state=/tmp/$RUN
mkdir -p "\$state"
printf '%s\\n' '$INCLUDE_PATHS' | tr ',' '\\n' > "\$state/include.roots"
: > "\$state/source.before.tsv"
while IFS= read -r root; do
  path="\$base/\$root"
  [ -d "\$path" ] || continue
  find "\$path" -mindepth 1 -printf '%P\\t%s\\t%T@\\n' | sed "s@^@\$root/@" >> "\$state/source.before.tsv"
done < "\$state/include.roots"
LC_ALL=C sort -o "\$state/source.before.tsv" "\$state/source.before.tsv"
cut -f1 "\$state/source.before.tsv" > "\$state/paths.before.txt"
du -sk "\$base" > "\$state/source.before.kb"
test -s "\$state/paths.before.txt" || { echo 'no safe files found; refusing to archive' >&2; exit 3; }
rm -f "\$state/sender.rc" "\$state/sender.done" "\$state/sender.log"
nohup bash -c 'set -o pipefail; tar -C "\$1" --no-recursion --null -T <(sed "s@^@./@" "\$2/paths.before.txt" | tr "\\n" "\\0") -cf - | nc -l -p "\$3"; rc=\${PIPESTATUS[0]}; printf "%s\\n" "\$rc" > "\$2/sender.rc"; touch "\$2/sender.done"; exit "\$rc"' _ "\$base" "\$state" "$PORT" >"\$state/sender.log" 2>&1 &
echo "sender_pid=\$! state=\$state port=$PORT"
EOF
)
  local receiver_command
  receiver_command=$(cat <<EOF
set -euo pipefail
backup=$BACKUP_ROOT/$RUN
mkdir -p "\$backup"
rm -f "\$backup/exports.tar" "\$backup/exports.tar.part" "\$backup/receiver.rc" "\$backup/receiver.done" "\$backup/receiver.log"
nohup bash -c 'set +e; rc=1; for attempt in \$(seq 1 180); do nc -w 120 "\$1" "\$2" > "\$3/exports.tar.part" && { rc=0; break; }; sleep 5; done; printf "%s\\n" "\$rc" > "\$3/receiver.rc"; if [ "\$rc" -eq 0 ]; then mv "\$3/exports.tar.part" "\$3/exports.tar"; fi; touch "\$3/receiver.done"; exit "\$rc"' _ "$WORKER_IP" "$PORT" "\$backup" >"\$backup/receiver.log" 2>&1 &
echo "receiver_pid=\$! backup=\$backup"
EOF
)
  # Either JumpServer PTY can linger after spawning remote nohup. Dispatch in
  # parallel so a stale session cannot prevent its peer from starting.
  remote "$BACKUP_HOST" "$receiver_command" >"$STATE_DIR/receiver.dispatch.log" 2>&1 &
  local receiver_dispatch_pid=$!
  remote "$WORKER" "$source_command" >"$STATE_DIR/sender.dispatch.log" 2>&1 &
  local sender_dispatch_pid=$!
  wait "$receiver_dispatch_pid" "$sender_dispatch_pid" || true

  echo "STARTED run=$RUN"
  echo "STATE_DIR=$STATE_DIR"
  echo "Run status with: $0 --status --state-dir $STATE_DIR"
  echo "Do not finalize until this reports READY_TO_FINALIZE."
}

status() {
  load_state
  if [[ "${BACKUP_MODE:-offhost}" == "shared-nas" ]]; then
    shared_status
    return
  fi
  local source_status backup_status
  source_status=$(cat <<EOF
set -eu
state=$WORKER_STATE
base=/Data/$PV
echo 'SOURCE'
for f in sender.rc sender.done source.before.kb; do [ -f "\$state/\$f" ] && { printf '%s=' "\$f"; cat "\$state/\$f"; } || echo "\$f=missing"; done
du -sk "\$base/workspace/exports" 2>/dev/null || true
EOF
)
  backup_status=$(cat <<EOF
set -eu
backup=$BACKUP_DIR
echo 'BACKUP'
for f in receiver.rc receiver.done; do [ -f "\$backup/\$f" ] && { printf '%s=' "\$f"; cat "\$backup/\$f"; } || echo "\$f=missing"; done
ls -lh "\$backup/exports.tar" "\$backup/exports.tar.part" 2>/dev/null || true
EOF
)
  remote "$WORKER" "$source_status"
  remote "$BACKUP_HOST" "$backup_status"
}

finalize() {
  load_state
  if [[ "${BACKUP_MODE:-offhost}" == "shared-nas" ]]; then
    shared_finalize
    return
  fi
  local source_verify archive_verify cleanup_command validation_port
  validation_port="$((PORT + 1))"
  source_verify=$(cat <<EOF
set -euo pipefail
base=/Data/$PV
state=$WORKER_STATE
test "\$(cat "\$state/sender.rc")" = 0
 : > "\$state/source.after.tsv"
while IFS= read -r root; do
  path="\$base/\$root"
  [ -d "\$path" ] || continue
  find "\$path" -mindepth 1 -printf '%P\\t%s\\t%T@\\n' | sed "s@^@\$root/@" >> "\$state/source.after.tsv"
done < "\$state/include.roots"
LC_ALL=C sort -o "\$state/source.after.tsv" "\$state/source.after.tsv"
cmp -s "\$state/source.before.tsv" "\$state/source.after.tsv" || { echo 'source changed during backup; retaining source' >&2; exit 4; }
rm -f "\$state/paths.before.copy" "\$state/path-list.rc"
nohup bash -c 'cat "\$1/paths.before.txt" | nc -l -p "\$2"; printf "%s\\n" "\${PIPESTATUS[0]}" > "\$1/path-list.rc"' _ "\$state" "$validation_port" > "\$state/path-list.log" 2>&1 &
echo "source verified; path-list server pid=\$! port=$validation_port"
EOF
)
  remote "$WORKER" "$source_verify"

  archive_verify=$(cat <<EOF
set -euo pipefail
backup=$BACKUP_DIR
test "\$(cat "\$backup/receiver.rc")" = 0
test -f "\$backup/exports.tar"
nc -w 120 "$WORKER_IP" "$validation_port" > "\$backup/source.paths.txt"
test -s "\$backup/source.paths.txt"
tar -tf "\$backup/exports.tar" > "\$backup/archive.paths.raw"
sed -e 's@^\./@@' -e '/^\$/d' "\$backup/archive.paths.raw" | LC_ALL=C sort -u > "\$backup/archive.paths.txt"
cmp -s "\$backup/source.paths.txt" "\$backup/archive.paths.txt" || { echo 'archive path list differs; retaining source' >&2; exit 5; }
sha256sum "\$backup/exports.tar" > "\$backup/exports.tar.sha256"
touch "\$backup/verified.done"
echo "backup verified at \$backup"
EOF
)
  remote "$BACKUP_HOST" "$archive_verify"

  cleanup_command=$(cat <<EOF
set -euo pipefail
base=/Data/$PV
state=$WORKER_STATE
test "\$(cat "\$state/path-list.rc")" = 0
echo 'backup verified; deleting only archived safe files'
while IFS= read -r rel; do
  if [[ "\$rel" == workspace/exports/* || "\$rel" == workspace/temp/* || "\$rel" == workspace/tmp/* || "\$rel" == workspace/artifacts/* ]]; then
    rm -f -- "\$base/\$rel"
  else
    echo "unexpected safe path refused: \$rel" >&2
    exit 6
  fi
done < "\$state/paths.before.txt"
find "\$base/workspace" -type d -empty -delete 2>/dev/null || true
sync
du -sk "\$base"
touch "\$state/cleanup.done"
echo "CLEANUP_COMPLETE backup=$BACKUP_DIR"
EOF
)
  remote "$WORKER" "$cleanup_command"
}

case "$ACTION" in
  inspect) inspect ;;
  start) start ;;
  status) status ;;
  finalize) finalize ;;
esac
