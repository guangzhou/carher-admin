#!/usr/bin/env bash
set -euo pipefail

JMS="${JMS:-./scripts/jms}"
BUILD_NODE="${BUILD_NODE:-k8s-work-227}"
REMOTE_ROOT="${REMOTE_ROOT:-/root/her266-h75-rollout}"
RUNTIME_SESSION="${RUNTIME_SESSION:-her266-h75-runtime}"
OPERATOR_SESSION="${OPERATOR_SESSION:-her266-h75-operator}"
FORCE_RESTART="${FORCE_RESTART:-0}"
PACK_ONLY="${PACK_ONLY:-0}"

SCRIPT_TAR="${SCRIPT_TAR:-/tmp/her266-h75-scripts.tar.gz}"
OPERATOR_TAR="${OPERATOR_TAR:-/tmp/operator-go-her266-h75.tar.gz}"
OPERATOR_PATCH="${OPERATOR_PATCH:-scripts/her266-h75/operator-h75-profile.patch}"
OPERATOR_PROFILE_TEST="${OPERATOR_PROFILE_TEST:-operator-go/internal/controller/reconciler_runtime_profile_test.go}"

if [[ ! -x "$JMS" ]]; then
  echo "run this from the carher-admin repo root or set JMS=/path/to/scripts/jms" >&2
  exit 2
fi
if [[ ! -f "$OPERATOR_PATCH" ]]; then
  echo "missing operator patch: $OPERATOR_PATCH" >&2
  exit 2
fi

tar -czf "$SCRIPT_TAR" scripts/her266-h75

OPERATOR_TMP="$(mktemp -d)"
git archive --format=tar HEAD operator-go | tar -xf - -C "$OPERATOR_TMP"
git -C "$OPERATOR_TMP" apply --recount "$PWD/$OPERATOR_PATCH"
if [[ -f "$OPERATOR_PROFILE_TEST" ]]; then
  cp "$OPERATOR_PROFILE_TEST" "$OPERATOR_TMP/$OPERATOR_PROFILE_TEST"
fi
tar -czf "$OPERATOR_TAR" -C "$OPERATOR_TMP" operator-go

if [[ "$PACK_ONLY" = 1 ]]; then
  echo "packed scripts:  $SCRIPT_TAR"
  echo "packed operator: $OPERATOR_TAR"
  exit 0
fi

"$JMS" ssh "$BUILD_NODE" "mkdir -p '$REMOTE_ROOT' '$REMOTE_ROOT/logs'"
"$JMS" scp "$SCRIPT_TAR" "$BUILD_NODE:/tmp/her266-h75-scripts.tar.gz"
"$JMS" scp "$OPERATOR_TAR" "$BUILD_NODE:/tmp/operator-go-her266-h75.tar.gz"
"$JMS" ssh "$BUILD_NODE" "tar -xzf /tmp/her266-h75-scripts.tar.gz -C '$REMOTE_ROOT' --strip-components=2 && chmod +x '$REMOTE_ROOT'/*.sh"

"$JMS" ssh "$BUILD_NODE" "if tmux has-session -t '$RUNTIME_SESSION' 2>/dev/null; then if [ '$FORCE_RESTART' = 1 ]; then tmux kill-session -t '$RUNTIME_SESSION'; else echo 'runtime session already exists'; exit 10; fi; fi; tmux new-session -d -s '$RUNTIME_SESSION' 'cd $REMOTE_ROOT && ./10-build-h75-runtime-from-source.sh && ./11-verify-h75-image.sh'"
"$JMS" ssh "$BUILD_NODE" "if tmux has-session -t '$OPERATOR_SESSION' 2>/dev/null; then if [ '$FORCE_RESTART' = 1 ]; then tmux kill-session -t '$OPERATOR_SESSION'; else echo 'operator session already exists'; exit 11; fi; fi; tmux new-session -d -s '$OPERATOR_SESSION' 'cd $REMOTE_ROOT && ./12-build-operator-image.sh'"

cat <<EOF
started:
  runtime:  $BUILD_NODE tmux session $RUNTIME_SESSION
  operator: $BUILD_NODE tmux session $OPERATOR_SESSION

status:
  $JMS ssh $BUILD_NODE 'tmux ls; tail -n 120 $REMOTE_ROOT/logs/*.log'
EOF
