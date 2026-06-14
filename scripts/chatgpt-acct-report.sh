#!/usr/bin/env bash
# chatgpt-acct-report.sh — one entrypoint for ChatGPT Pro upstream quota and downstream spend.
#
# Usage:
#   ./scripts/chatgpt-acct-report.sh                         # upstream quota, retry 3
#   ./scripts/chatgpt-acct-report.sh upstream --all-accounts
#   ./scripts/chatgpt-acct-report.sh downstream 24h
#   ./scripts/chatgpt-acct-report.sh all 7d --raw
#
# The underlying scripts are read-only probes. Keep this wrapper thin so fixes
# stay in the source scripts instead of being duplicated here.

set -euo pipefail

MODE="upstream"
WINDOW="7d"
RAW=""
RETRY="${USAGE_RETRY:-3}"
HTTP_TIMEOUT="${USAGE_HTTP_TIMEOUT:-10}"
USAGE_ALL=""
SKIP_ALIYUN=""

usage() {
  cat <<'EOF'
chatgpt-acct-report.sh — one entrypoint for ChatGPT Pro upstream quota and downstream spend.

Usage:
  ./scripts/chatgpt-acct-report.sh                         # upstream quota, retry 3
  ./scripts/chatgpt-acct-report.sh upstream --all-accounts
  ./scripts/chatgpt-acct-report.sh downstream 24h
  ./scripts/chatgpt-acct-report.sh all 7d --raw

Modes:
  upstream|usage|quota|capacity   ChatGPT Pro 5h/week quota waterline
  downstream|spend|traffic        LiteLLM calls/spend/tokens
  all|both|full                   Run both probes
EOF
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    upstream|usage|quota|capacity) MODE="upstream"; shift ;;
    downstream|spend|traffic)      MODE="downstream"; shift ;;
    all|both|full)                 MODE="all"; shift ;;
  esac
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --raw) RAW="--raw"; shift ;;
    --retry) RETRY="$2"; shift 2 ;;
    --timeout) HTTP_TIMEOUT="$2"; shift 2 ;;
    --all-accounts|--all) USAGE_ALL="--all"; shift ;;
    --skip-aliyun) SKIP_ALIYUN="--skip-aliyun"; shift ;;
    -h|--help) usage; exit 0 ;;
    [0-9]*m|[0-9]*h|[0-9]*d) WINDOW="$1"; shift ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USAGE_SCRIPT="$SCRIPT_DIR/chatgpt-acct-usage.sh"
SPEND_SCRIPT="$SCRIPT_DIR/chatgpt-acct-spend.sh"

for f in "$USAGE_SCRIPT" "$SPEND_SCRIPT"; do
  if [[ ! -x "$f" ]]; then
    echo "missing executable script: $f" >&2
    exit 1
  fi
done

run_upstream() {
  echo "### ChatGPT Pro upstream quota"
  local args=(--retry "$RETRY" --timeout "$HTTP_TIMEOUT")
  [[ -n "$USAGE_ALL" ]] && args+=("$USAGE_ALL")
  [[ -n "$SKIP_ALIYUN" ]] && args+=("$SKIP_ALIYUN")
  "$USAGE_SCRIPT" "${args[@]}"
}

run_downstream() {
  echo "### ChatGPT Pro downstream spend (window=$WINDOW)"
  local args=(both "$WINDOW")
  [[ -n "$RAW" ]] && args+=("$RAW")
  "$SPEND_SCRIPT" "${args[@]}"
}

case "$MODE" in
  upstream)
    run_upstream
    ;;
  downstream)
    run_downstream
    ;;
  all)
    run_upstream
    echo
    run_downstream
    ;;
esac
