#!/usr/bin/env bash
# Run the 198 Pro GPT product model regression without changing production routing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFY_SCRIPT="$SCRIPT_DIR/litellm-dev-gpt-products-verify.py"

REPORTS_DIR="reports"

usage() {
  cat <<'EOF'
Usage:
  scripts/litellm-pro-gpt-products-regression.sh
  scripts/litellm-pro-gpt-products-regression.sh --reports-dir reports/pro-gpt

Options:
  --reports-dir   Local report output directory for verification.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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

python3 "$VERIFY_SCRIPT" --profile pro --strict --check-cursor-keys --reports-dir "$REPORTS_DIR"
