#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PY_SCRIPT="$SCRIPT_DIR/sync-feishu-allowfrom-fleet.py"
ASSET="${ASSET:-k8s-work-226}"
REMOTE_SCRIPT="${REMOTE_SCRIPT:-/tmp/sync-feishu-allowfrom-fleet.py}"
JMS_TIMEOUT="${JMS_TIMEOUT:-7200}"

usage() {
  cat <<'EOF'
Usage:
  scripts/sync-feishu-allowfrom-fleet-runner.sh audit [script args...]
  scripts/sync-feishu-allowfrom-fleet-runner.sh canary [script args...]
  scripts/sync-feishu-allowfrom-fleet-runner.sh batch [script args...]

Modes:
  audit   Read-only fleet audit. No config or pod changes.
  canary  Repair and restart carher-268 only.
  batch   Repair all eligible Hers in waves of 10; failures are skipped and reported.

Examples:
  scripts/sync-feishu-allowfrom-fleet-runner.sh audit --targets 7 8 11 268
  scripts/sync-feishu-allowfrom-fleet-runner.sh canary
  scripts/sync-feishu-allowfrom-fleet-runner.sh batch

The runner always uses JumpServer TTY mode and executes on k8s-work-226, where
kubectl and the shared /Data NAS mount are available.
EOF
}

mode="${1:-}"
case "$mode" in
  audit|canary|batch) shift ;;
  -h|--help|"") usage; exit 0 ;;
  *) echo "ERROR: unknown mode: $mode" >&2; usage >&2; exit 2 ;;
esac

case "$mode" in
  audit) args=("$@") ;;
  canary) args=(--targets 268 --wave-size 1 --apply --restart "$@") ;;
  batch) args=(--wave-size 10 --apply --restart "$@") ;;
esac

cd "$REPO_DIR"
bash scripts/push226.sh "$PY_SCRIPT" "$REMOTE_SCRIPT"

remote_command="python3"
printf -v quoted ' %q' "$REMOTE_SCRIPT" "${args[@]}"
remote_command+="$quoted"

scripts/jms ssh --tty --timeout "$JMS_TIMEOUT" "$ASSET" "$remote_command"
