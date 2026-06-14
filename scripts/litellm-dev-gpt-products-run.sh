#!/usr/bin/env bash
# Apply litellm-dev GPT product routing config, then run the full verification report.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_SCRIPT="$SCRIPT_DIR/litellm-dev-gpt-products-config.py"
VERIFY_SCRIPT="$SCRIPT_DIR/litellm-dev-gpt-products-verify.py"

DRY_RUN=0
SKIP_CONFIG=0
NO_RESTART=0
REPORTS_DIR="reports"

usage() {
  cat <<'EOF'
Usage:
  scripts/litellm-dev-gpt-products-run.sh
  scripts/litellm-dev-gpt-products-run.sh --dry-run
  scripts/litellm-dev-gpt-products-run.sh --skip-config
  scripts/litellm-dev-gpt-products-run.sh --reports-dir reports/dev-gpt

Options:
  --dry-run       Show config diff only; do not apply or verify.
  --skip-config   Do not apply config; verify the current dev runtime state.
  --no-restart    Apply config without restarting litellm-dev proxy.
  --reports-dir   Local report output directory for verification.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-config|--verify-only)
      SKIP_CONFIG=1
      shift
      ;;
    --no-restart)
      NO_RESTART=1
      shift
      ;;
    --reports-dir)
      REPORTS_DIR="${2:?missing --reports-dir value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$DRY_RUN" == 1 ]]; then
  exec python3 "$CONFIG_SCRIPT"
fi

if [[ "$SKIP_CONFIG" == 0 ]]; then
  CONFIG_ARGS=(--apply)
  if [[ "$NO_RESTART" == 1 ]]; then
    CONFIG_ARGS+=(--no-restart)
  fi
  python3 "$CONFIG_SCRIPT" "${CONFIG_ARGS[@]}"
fi

python3 "$VERIFY_SCRIPT" --reports-dir "$REPORTS_DIR"
