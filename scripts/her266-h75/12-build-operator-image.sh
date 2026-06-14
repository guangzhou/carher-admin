#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/12-build-operator-image.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

SRC_TAR="${SRC_TAR:-/tmp/operator-go-her266-h75.tar.gz}"
OPERATOR_IMAGE="${OPERATOR_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:her266-h75-profile-20260530-r11}"

rm -rf "$ROOT/operator-src"
mkdir -p "$ROOT/operator-src"
tar -xzf "$SRC_TAR" -C "$ROOT/operator-src"

cd "$ROOT/operator-src/operator-go"
echo "[operator] gofmt + focused profile tests"
nerdctl run --rm -v "$PWD:/src" -w /src golang:1.23-alpine sh -lc '/usr/local/go/bin/go mod tidy && /usr/local/go/bin/go fmt ./... && /usr/local/go/bin/go test ./internal/controller -run "TestRuntimeProfile"'

cd "$ROOT/operator-src"
tar -czf "$ROOT/operator-go-formatted.tar.gz" operator-go

echo "[operator] build $OPERATOR_IMAGE"
nerdctl build -t "$OPERATOR_IMAGE" -f operator-go/Dockerfile operator-go
nerdctl push "$OPERATOR_IMAGE"
nerdctl image inspect "$OPERATOR_IMAGE" --format '{{json .}}' > "$ROOT/operator-image.inspect.json"
echo "{\"operator_image\":\"$OPERATOR_IMAGE\",\"completed_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$ROOT/operator-image.state.json"
echo "[operator] done"
