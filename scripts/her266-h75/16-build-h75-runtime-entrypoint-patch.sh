#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
WORK_DIR="$ROOT/runtime-entrypoint-patch"
mkdir -p "$LOG_DIR" "$WORK_DIR"
exec > >(tee -a "$LOG_DIR/16-build-h75-runtime-entrypoint-patch.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

BASE_IMAGE="${BASE_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-20260530}"
TARGET_IMAGE="${TARGET_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-20260530}"

echo "[runtime-patch] base=$BASE_IMAGE"
echo "[runtime-patch] target=$TARGET_IMAGE"

cid="$(nerdctl create --entrypoint sh "$BASE_IMAGE" -lc true)"
cleanup() {
  nerdctl rm -f "$cid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

nerdctl cp "$cid":/entrypoint.sh "$WORK_DIR/entrypoint.sh"

python3 - "$WORK_DIR/entrypoint.sh" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
start_marker = '  echo "▶ seeding baked ACP adapters into OpenClaw data volume"\n'
end_marker = '  test -d "$acp_prefix/lib/node_modules/@agentclientprotocol/claude-agent-acp"\n'
if 'missing-only ACP adapter seed' in text:
    raise SystemExit("entrypoint already patched")
try:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
except ValueError as exc:
    raise SystemExit(f"ACP adapter seed block not found: {exc}")

replacement = r'''  local adapter_stamp="$acp_prefix/lib/node_modules/.carher-acp-adapters.source"
  local expected_stamp=""
  expected_stamp="$(cd "$baked_acp_adapters/node_modules" && find . -mindepth 1 -maxdepth 3 -printf '%P\t%y\t%s\t%l\n' | sort | sha256sum | awk '{print $1}')"

  if [ -f "$adapter_stamp" ] && [ "$(cat "$adapter_stamp" 2>/dev/null || true)" = "$expected_stamp" ] && [ -x "$acp_prefix/bin/claude-agent-acp" ] && [ -x "$acp_prefix/bin/codex-acp" ]; then
    echo "  ✓ baked ACP adapters already seeded ($expected_stamp)"
  else
    echo "▶ ensuring baked ACP adapters in OpenClaw data volume (missing-only ACP adapter seed)"
    mkdir -p "$acp_prefix/lib/node_modules" "$acp_prefix/bin"

    seed_acp_package_if_missing() {
      local src="$1"
      local dst="$2"
      local tmp=""
      [ -e "$src" ] || return 0
      if [ -e "$dst" ] || [ -L "$dst" ]; then
        return 0
      fi
      tmp="${dst}.carher-copy.$$"
      rm -rf "$tmp"
      mkdir -p "$(dirname "$dst")"
      cp -a "$src" "$tmp"
      mv "$tmp" "$dst"
    }

    for pkg in "$baked_acp_adapters/node_modules"/* "$baked_acp_adapters/node_modules"/.[!.]*; do
      [ -e "$pkg" ] || continue
      base="$(basename "$pkg")"
      [ "$base" = "." ] || [ "$base" = ".." ] && continue
      if [ -d "$pkg" ] && [[ "$base" == @* ]]; then
        mkdir -p "$acp_prefix/lib/node_modules/$base"
        for scoped_pkg in "$pkg"/*; do
          [ -e "$scoped_pkg" ] || continue
          seed_acp_package_if_missing "$scoped_pkg" "$acp_prefix/lib/node_modules/$base/$(basename "$scoped_pkg")"
        done
      else
        seed_acp_package_if_missing "$pkg" "$acp_prefix/lib/node_modules/$base"
      fi
    done
    printf '%s\n' "$expected_stamp" > "$adapter_stamp"
  fi
'''

path.write_text(text[:start] + replacement + text[end:])
PY

grep -q "missing-only ACP adapter seed" "$WORK_DIR/entrypoint.sh"

cat > "$WORK_DIR/Dockerfile" <<'DOCKER'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh
LABEL carher.patch.acp_seed="missing-only-v1"
DOCKER

nerdctl build \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -t "$TARGET_IMAGE" \
  -f "$WORK_DIR/Dockerfile" \
  "$WORK_DIR"
nerdctl push "$TARGET_IMAGE"

nerdctl image inspect "$TARGET_IMAGE" --format '{{json .}}' > "$ROOT/h75-runtime-entrypoint-patch.inspect.json"
cat > "$ROOT/h75-runtime-entrypoint-patch.state.json" <<JSON
{
  "base_image": "$BASE_IMAGE",
  "target_image": "$TARGET_IMAGE",
  "patch": "missing-only ACP adapter seed",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "[runtime-patch] done"
