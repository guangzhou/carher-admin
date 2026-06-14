#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/11-verify-h75-image.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

ACR_IMAGE="${ACR_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-20260530}"
EXPECTED_RUNTIME_REF="${EXPECTED_RUNTIME_REF:-68c11a88d8dd}"
EXPECTED_OPENCLAW_OVERLAY_REF="${EXPECTED_OPENCLAW_OVERLAY_REF:-4f7012297075ce4c969a6f5c13eb98172250d657}"
EXPECTED_HERMES_REF="${EXPECTED_HERMES_REF:-f81ed4deb95752040c95de0b30204f8a8c14118c}"

echo "[verify-image] image=$ACR_IMAGE"
nerdctl pull "$ACR_IMAGE"
nerdctl image inspect "$ACR_IMAGE" --format '{{.Os}}/{{.Architecture}} {{.ID}}'
LABELS_JSON="$(nerdctl image inspect "$ACR_IMAGE" --format '{{json .Config.Labels}}')"
LABELS_JSON="$LABELS_JSON" python3 - "$EXPECTED_RUNTIME_REF" "$EXPECTED_OPENCLAW_OVERLAY_REF" "$EXPECTED_HERMES_REF" <<'PY'
import os
import json
import sys

labels = json.loads(os.environ["LABELS_JSON"])
expected = {
    "carher.image.runtime_ref": sys.argv[1],
    "carher.image.openclaw_overlay_ref": sys.argv[2],
    "carher.image.hermes_ref": sys.argv[3],
    "carher.image.target_platform": "linux/amd64",
    "carher.image.no_engine_source_compile": "true",
    "carher.image.no_runtime_install": "true",
}
for key, value in expected.items():
    actual = labels.get(key)
    if actual != value:
        raise SystemExit(f"label mismatch {key}: expected {value}, got {actual}")
print("[verify-image] labels match H75")
PY
TMP_DIR="$(mktemp -d "$ROOT/h75-image-check.XXXXXX")"
CID=""
cleanup() {
  if [[ -n "$CID" ]]; then
    nerdctl rm -f "$CID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

CID="$(nerdctl create --entrypoint sh "$ACR_IMAGE" -lc true)"
copy_from_image() {
  local src="$1"
  local dest="$2"
  nerdctl cp "$CID:$src" "$TMP_DIR/$dest"
}

copy_from_image /entrypoint.sh entrypoint.sh
copy_from_image /etc/carher-release.json carher-release.json
copy_from_image /opt/carher/image-info.json image-info.json
copy_from_image /opt/node22/bin/node node
copy_from_image /opt/openclaw/lib/node_modules/openclaw/dist/index.js openclaw-index.js
copy_from_image /opt/carher-runtime/vendor/dify-workflow/bin/dify-bootstrap-init dify-bootstrap-init
copy_from_image /opt/carher-runtime/vendor/dify-workflow/bin/her-workflow-dify-creator her-workflow-dify-creator
copy_from_image /opt/carher-runtime/vendor/dify-workflow/bin/her-workflow-dify-mcp her-workflow-dify-mcp

test -x "$TMP_DIR/entrypoint.sh"
test -f "$TMP_DIR/carher-release.json"
test -f "$TMP_DIR/image-info.json"
test -x "$TMP_DIR/node"
test -f "$TMP_DIR/openclaw-index.js"
test -x "$TMP_DIR/dify-bootstrap-init"
test -x "$TMP_DIR/her-workflow-dify-creator"
test -x "$TMP_DIR/her-workflow-dify-mcp"
grep -q "install_dify_workflow_tools" "$TMP_DIR/entrypoint.sh"
grep -q "gateway run" "$TMP_DIR/entrypoint.sh"
cat "$TMP_DIR/carher-release.json"
echo "{\"acr_image\":\"$ACR_IMAGE\",\"verified_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$ROOT/h75-runtime-image.verify.json"
echo "[verify-image] done"
