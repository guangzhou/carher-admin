#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/Data/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/15-push-h75-from-s3-local-image.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

SOURCE_IMAGE="${SOURCE_IMAGE:-ghcr.io/buyitsydney/carher-runtime@sha256:b600887e7602dfdfd74128b80ea84e5f416107c4c7789a2bda53b41a18fc769b}"
TARGET_IMAGE="${TARGET_IMAGE:-cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-20260530}"
EXPECTED_IMAGE_ID="${EXPECTED_IMAGE_ID:-sha256:1068f31437d355252dc541755de7bb0eb20b8dd903e4595c062fd3ff58fe9e62}"
EXPECTED_RUNTIME_REF="${EXPECTED_RUNTIME_REF:-68c11a88d8dd}"
EXPECTED_OPENCLAW_OVERLAY_REF="${EXPECTED_OPENCLAW_OVERLAY_REF:-4f7012297075ce4c969a6f5c13eb98172250d657}"
EXPECTED_HERMES_REF="${EXPECTED_HERMES_REF:-f81ed4deb95752040c95de0b30204f8a8c14118c}"

if [[ -z "${DOCKER_CONFIG:-}" || ! -f "$DOCKER_CONFIG/config.json" ]]; then
  echo "set DOCKER_CONFIG to a directory containing ACR auth config.json" >&2
  exit 2
fi

echo "[s3-push] source=$SOURCE_IMAGE"
echo "[s3-push] target=$TARGET_IMAGE"

image_id="$(docker image inspect "$SOURCE_IMAGE" --format '{{.Id}}')"
platform="$(docker image inspect "$SOURCE_IMAGE" --format '{{.Os}}/{{.Architecture}}')"
echo "[s3-push] id=$image_id platform=$platform"
if [[ "$image_id" != "$EXPECTED_IMAGE_ID" ]]; then
  echo "unexpected source image id: $image_id" >&2
  exit 20
fi
if [[ "$platform" != "linux/amd64" ]]; then
  echo "unexpected source platform: $platform" >&2
  exit 21
fi

LABELS_JSON="$(docker image inspect "$SOURCE_IMAGE" --format '{{json .Config.Labels}}')"
LABELS_JSON="$LABELS_JSON" python3 - "$EXPECTED_RUNTIME_REF" "$EXPECTED_OPENCLAW_OVERLAY_REF" "$EXPECTED_HERMES_REF" <<'PY'
import json
import os
import sys

labels = json.loads(os.environ["LABELS_JSON"])
expected = {
    "carher.image.runtime_ref": sys.argv[1],
    "carher.image.openclaw_overlay_ref": sys.argv[2],
    "carher.image.hermes_ref": sys.argv[3],
}
for key, value in expected.items():
    actual = labels.get(key)
    if actual != value:
        raise SystemExit(f"label mismatch {key}: expected {value}, got {actual}")
print("[s3-push] labels match H75")
PY

docker tag "$SOURCE_IMAGE" "$TARGET_IMAGE"
docker push "$TARGET_IMAGE"

docker image inspect "$TARGET_IMAGE" --format '{{json .}}' > "$ROOT/h75-s3-target-image.inspect.json"
cat > "$ROOT/h75-s3-target-image.state.json" <<JSON
{
  "source_image": "$SOURCE_IMAGE",
  "target_image": "$TARGET_IMAGE",
  "source_image_id": "$image_id",
  "platform": "$platform",
  "runtime_ref": "$EXPECTED_RUNTIME_REF",
  "openclaw_overlay_ref": "$EXPECTED_OPENCLAW_OVERLAY_REF",
  "hermes_ref": "$EXPECTED_HERMES_REF",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "[s3-push] done"
