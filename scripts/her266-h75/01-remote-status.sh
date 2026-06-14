#!/usr/bin/env bash
set -euo pipefail

JMS="${JMS:-./scripts/jms}"
BUILD_NODE="${BUILD_NODE:-k8s-work-227}"
REMOTE_ROOT="${REMOTE_ROOT:-/root/her266-h75-rollout}"

"$JMS" ssh "$BUILD_NODE" "tmux ls 2>/dev/null || true; printf '\n-- state --\n'; ls -la '$REMOTE_ROOT'/*.json 2>/dev/null || true; printf '\n-- logs --\n'; for f in '$REMOTE_ROOT'/logs/*.log; do [ -f \"\$f\" ] && { echo \"### \$f\"; tail -n 80 \"\$f\"; }; done"
