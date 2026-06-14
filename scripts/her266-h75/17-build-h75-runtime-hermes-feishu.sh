#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
WORK_DIR="$ROOT/runtime-hermes-feishu"
mkdir -p "$LOG_DIR" "$WORK_DIR"
exec > >(tee -a "$LOG_DIR/17-build-h75-runtime-hermes-feishu.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

BASE_IMAGE="${BASE_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-20260530}"
TARGET_IMAGE="${TARGET_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-20260530}"

echo "[runtime-feishu] base=$BASE_IMAGE"
echo "[runtime-feishu] target=$TARGET_IMAGE"

cat > "$WORK_DIR/Dockerfile" <<'DOCKER'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

RUN uv pip install --python /opt/hermes/venv/bin/python \
      "lark-oapi==1.5.3" \
      "aiohttp-socks==0.11.0" \
 && /opt/hermes/venv/bin/python - <<'PY'
import importlib.util

missing = [
    name
    for name in ("lark_oapi", "aiohttp_socks")
    if importlib.util.find_spec(name) is None
]
if missing:
    raise SystemExit(f"missing Hermes Feishu deps: {missing}")
PY

LABEL carher.patch.hermes_feishu_deps="lark-oapi-1.5.3-aiohttp-socks-0.11.0"
DOCKER

nerdctl build \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -t "$TARGET_IMAGE" \
  -f "$WORK_DIR/Dockerfile" \
  "$WORK_DIR"
nerdctl push "$TARGET_IMAGE"

nerdctl image inspect "$TARGET_IMAGE" --format '{{json .}}' > "$ROOT/h75-runtime-hermes-feishu.inspect.json"
cat > "$ROOT/h75-runtime-hermes-feishu.state.json" <<JSON
{
  "base_image": "$BASE_IMAGE",
  "target_image": "$TARGET_IMAGE",
  "patch": "install Hermes Feishu dependencies into /opt/hermes/venv",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "[runtime-feishu] done"
