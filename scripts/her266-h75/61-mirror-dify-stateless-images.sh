#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${ROOT:-}" ]]; then
  if [[ -w /root ]]; then
    ROOT="/root/her266-h75-rollout"
  else
    ROOT="/tmp/her266-h75-rollout"
  fi
fi
LOG_DIR="$ROOT/logs"
TAG_SUFFIX="${TAG_SUFFIX:-20260530}"
TARGET_REPOSITORY="${TARGET_REPOSITORY:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher}"
STATE_FILE="$ROOT/dify-stateless-ha-images.state.json"
MAPPING_FILE="$ROOT/dify-stateless-ha-images.tsv"
DRY_RUN="${DRY_RUN:-0}"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/61-mirror-dify-stateless-images.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

cat >"$MAPPING_FILE" <<EOF
docker.io/langgenius/dify-api:1.4.2	$TARGET_REPOSITORY:dify-api-1.4.2-$TAG_SUFFIX
docker.io/langgenius/dify-web:1.4.2	$TARGET_REPOSITORY:dify-web-1.4.2-$TAG_SUFFIX
docker.io/python:3.12-slim	$TARGET_REPOSITORY:dify-python-3.12-slim-$TAG_SUFFIX
docker.io/bitnami/kubectl:latest	$TARGET_REPOSITORY:dify-bitnami-kubectl-latest-$TAG_SUFFIX
docker.io/nginx:latest	$TARGET_REPOSITORY:dify-nginx-latest-$TAG_SUFFIX
EOF

echo "[dify-images] target repository: $TARGET_REPOSITORY"
echo "[dify-images] mapping file: $MAPPING_FILE"

if [[ "$DRY_RUN" = 1 ]]; then
  cat "$MAPPING_FILE"
  exit 0
fi

while IFS=$'\t' read -r source target; do
  [[ -z "$source" || -z "$target" ]] && continue
  echo "[dify-images] pull $source"
  nerdctl pull --platform linux/amd64 "$source"
  echo "[dify-images] tag $target"
  nerdctl tag "$source" "$target"
  echo "[dify-images] push $target"
  nerdctl push "$target"
done <"$MAPPING_FILE"

python3 - "$MAPPING_FILE" "$STATE_FILE" <<'PY'
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

mapping = []
for line in Path(sys.argv[1]).read_text().splitlines():
    if not line.strip():
        continue
    source, target = line.split("\t", 1)
    inspect = subprocess.check_output(
        ["nerdctl", "image", "inspect", target, "--format", "{{json .}}"],
        text=True,
    )
    image = json.loads(inspect)
    mapping.append(
        {
            "source": source,
            "target": target,
            "id": image.get("ID") or image.get("Id"),
            "platform": f"{image.get('Os', 'linux')}/{image.get('Architecture', 'amd64')}",
        }
    )

Path(sys.argv[2]).write_text(
    json.dumps(
        {
            "purpose": "Dify stateless HA image mirror for ACK VPC pulls",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "images": mapping,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n"
)
PY

echo "[dify-images] done"
