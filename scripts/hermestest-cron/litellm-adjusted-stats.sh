#!/bin/bash
# Run adjusted LiteLLM stats without relying on the agent to assemble messages.
# Usage:
#   scripts/litellm-adjusted-stats.sh daily [--dry-run] [--date YYYY-MM-DD] [--chat oc_xxx]
#   scripts/litellm-adjusted-stats.sh balance [--dry-run] [--chat oc_xxx]
#   scripts/litellm-adjusted-stats.sh both [--dry-run] [--date YYYY-MM-DD] [--chat oc_xxx]

set -euo pipefail

ROOT="/data/.openclaw/workspace"
MODE="${1:-both}"
if [[ $# -gt 0 ]]; then shift; fi

DRY_RUN=false
DATE=""
CHAT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --date) DATE="$2"; shift 2 ;;
    --date=*) DATE="${1#--date=}"; shift ;;
    --chat) CHAT="$2"; shift 2 ;;
    --chat=*) CHAT="${1#--chat=}"; shift ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

run_daily() {
  # Daily company report uses raw actual spend: per-key /global/spend/logs, merged by person.
  local cmd=(node "$ROOT/scripts/litellm-company-daily-actual-report.js")
  [[ "$DRY_RUN" == true ]] && cmd+=(--dry-run)
  [[ -n "$DATE" ]] && cmd+=(--date="$DATE")
  [[ -n "$CHAT" ]] && cmd+=(--chat="$CHAT")
  NODE_PATH=/app/node_modules "${cmd[@]}"
}

run_balance() {
  local cmd=(bash "$ROOT/scripts/litellm-claude-balance-report.sh")
  [[ "$DRY_RUN" == true ]] && cmd+=(--dry-run)
  [[ -n "$CHAT" ]] && cmd+=(--chat "$CHAT")
  "${cmd[@]}"
}

case "$MODE" in
  daily) run_daily ;;
  balance) run_balance ;;
  both)
    run_daily
    printf '\n---\n'
    run_balance
    ;;
  *) echo "mode must be one of: daily, balance, both" >&2; exit 1 ;;
esac
