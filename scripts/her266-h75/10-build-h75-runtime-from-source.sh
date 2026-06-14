#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
SRC_DIR="${SRC_DIR:-$ROOT/src}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR" "$SRC_DIR"
exec > >(tee -a "$LOG_DIR/10-build-h75-runtime-from-source.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

TARGET="${TARGET:-ack-her-266-h75}"
TARGET_IMAGE="${TARGET_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-20260530}"

CARHER_REPO="${CARHER_REPO:-git@github.com:buyitsydney/CarHer.git}"
CARHER_REF="${CARHER_REF:-4f7012297075ce4c969a6f5c13eb98172250d657}"
CARHER_SRC="${CARHER_SRC:-}"

RUNTIME_REPO="${RUNTIME_REPO:-git@github.com:buyitsydney/carher-runtime.git}"
RUNTIME_REF="${RUNTIME_REF:-68c11a88d8dd}"
RUNTIME_SRC="${RUNTIME_SRC:-}"

# H75 live image records this ref. carher-runtime/release/engine-base-images.json
# can override this if it records a different canonical Hermes repository.
HERMES_REPO="${HERMES_REPO:-git@github.com:buyitsydney/carher-hermes.git}"
HERMES_REF="${HERMES_REF:-f81ed4deb95752040c95de0b30204f8a8c14118c}"
HERMES_SRC="${HERMES_SRC:-}"

OPENCLAW_BASE_IMAGE="${OPENCLAW_BASE_IMAGE:-ghcr.io/openclaw/openclaw@sha256:17a04e767f3097d08b0f31ecd753c5743f0e9c7e3ee613820f1e1d57d84efa4d}"

default_source_for() {
  local name="$1"
  local candidate
  case "$name" in
    CarHer)
      for candidate in /root/carher /root/CarHer /Data/carher /Data/CarHer; do
        [[ -d "$candidate/.git" ]] && { printf '%s\n' "$candidate"; return 0; }
      done
      ;;
    carher-runtime)
      for candidate in /root/carher-runtime /Data/carher-runtime /root/runtime/carher-runtime; do
        [[ -d "$candidate/.git" ]] && { printf '%s\n' "$candidate"; return 0; }
      done
      ;;
    carher-hermes)
      for candidate in /root/carher-hermes /Data/carher-hermes /root/hermes/carher-hermes; do
        [[ -d "$candidate/.git" ]] && { printf '%s\n' "$candidate"; return 0; }
      done
      ;;
  esac
  return 1
}

copy_source_dir() {
  local name="$1"
  local src="$2"
  local dir="$SRC_DIR/$name"
  if [[ -z "$src" ]]; then
    return 1
  fi
  if [[ ! -d "$src/.git" ]]; then
    echo "[source] $name source override is not a git checkout: $src" >&2
    exit 18
  fi
  echo "[source] use existing checkout $src -> $dir"
  rm -rf "$dir"
  mkdir -p "$(dirname "$dir")"
  cp -a "$src" "$dir"
  return 0
}

checkout_ref() {
  local name="$1"
  local repo="$2"
  local ref="$3"
  local source_override="${4:-}"
  local dir="$SRC_DIR/$name"
  if [[ -z "$source_override" ]]; then
    source_override="$(default_source_for "$name" || true)"
  fi
  if copy_source_dir "$name" "$source_override"; then
    :
  elif [[ -d "$dir/.git" ]]; then
    echo "[source] reuse existing checkout $dir"
  elif [[ -n "$repo" ]]; then
    echo "[source] clone $repo -> $dir"
    git clone "$repo" "$dir"
  else
    echo "[source] no repo or source override for $name" >&2
    exit 19
  fi
  echo "[source] checkout $name@$ref"
  if ! git -C "$dir" cat-file -e "$ref^{commit}" 2>/dev/null; then
    if [[ -z "$repo" ]]; then
      echo "[source] $name ref $ref is not present and repo is not set" >&2
      exit 21
    fi
    git -C "$dir" fetch --all --tags --prune
  fi
  git -C "$dir" reset --hard >/dev/null
  git -C "$dir" checkout --detach "$ref"
  git -C "$dir" rev-parse HEAD
}

json_value() {
  local file="$1"
  local expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json, sys
path, expr = sys.argv[1], sys.argv[2].split(".")
try:
    data = json.load(open(path))
    for key in expr:
        if not key:
            continue
        data = data[key]
    print(data)
except Exception:
    pass
PY
}

echo "[h75-build] target=$TARGET"
echo "[h75-build] image=$TARGET_IMAGE"
echo "[h75-build] refs runtime=$RUNTIME_REF openclaw=$CARHER_REF hermes=$HERMES_REF"

checkout_ref carher-runtime "$RUNTIME_REPO" "$RUNTIME_REF" "$RUNTIME_SRC"
checkout_ref CarHer "$CARHER_REPO" "$CARHER_REF" "$CARHER_SRC"

ENGINE_LOCK="$SRC_DIR/carher-runtime/release/engine-base-images.json"
if [[ -f "$ENGINE_LOCK" ]]; then
  detected_repo="$(json_value "$ENGINE_LOCK" "hermes.repository")"
  if [[ -n "$detected_repo" && -z "$HERMES_REPO" ]]; then
    HERMES_REPO="$detected_repo"
  fi
  echo "[h75-build] engine lock: $ENGINE_LOCK"
  echo "[h75-build] lock openclaw repo=$(json_value "$ENGINE_LOCK" "openclaw.repository")"
  echo "[h75-build] lock hermes repo=$(json_value "$ENGINE_LOCK" "hermes.repository")"
fi

checkout_ref carher-hermes "$HERMES_REPO" "$HERMES_REF" "$HERMES_SRC"

cd "$SRC_DIR/carher-runtime"

run_runtime_builder() {
  if [[ -x scripts/build-runtime-image.sh ]]; then
    scripts/build-runtime-image.sh --target "$TARGET" --image "$TARGET_IMAGE" --push
    return
  fi
  if [[ -x scripts/docker-release.sh ]]; then
    scripts/docker-release.sh --target "$TARGET" --image "$TARGET_IMAGE" --push
    return
  fi
  if [[ -x scripts/release-docker.sh ]]; then
    scripts/release-docker.sh --target "$TARGET" --image "$TARGET_IMAGE" --push
    return
  fi

  local dockerfile=""
  for candidate in docker/Dockerfile.runtime docker/Dockerfile.cicd-dual Dockerfile.runtime Dockerfile.cicd-dual; do
    if [[ -f "$candidate" ]]; then
      dockerfile="$candidate"
      break
    fi
  done
  if [[ -z "$dockerfile" ]]; then
    echo "[h75-build] no known runtime build script or Dockerfile found" >&2
    find . -maxdepth 3 -type f \( -name 'Dockerfile*' -o -path './scripts/*.sh' \) | sort >&2
    exit 20
  fi

  echo "[h75-build] fallback nerdctl build using $dockerfile"
  nerdctl build \
    --build-arg "TARGET=$TARGET" \
    --build-arg "TARGET_PLATFORM=linux/amd64" \
    --build-arg "OPENCLAW_BASE_IMAGE=$OPENCLAW_BASE_IMAGE" \
    --build-arg "OPENCLAW_REF=$CARHER_REF" \
    --build-arg "OPENCLAW_OVERLAY_REF=$CARHER_REF" \
    --build-arg "RUNTIME_REF=$RUNTIME_REF" \
    --build-arg "HERMES_REF=$HERMES_REF" \
    --build-context "carher-runtime=$SRC_DIR/carher-runtime" \
    --build-context "runtime=$SRC_DIR/carher-runtime" \
    --build-context "CarHer=$SRC_DIR/CarHer" \
    --build-context "carher-openclaw=$SRC_DIR/CarHer" \
    --build-context "openclaw-overlay=$SRC_DIR/CarHer" \
    --build-context "carher-hermes=$SRC_DIR/carher-hermes" \
    -t "$TARGET_IMAGE" \
    -f "$dockerfile" .
  nerdctl push "$TARGET_IMAGE"
}

run_runtime_builder

nerdctl image inspect "$TARGET_IMAGE" --format '{{json .}}' > "$ROOT/h75-runtime-image.inspect.json"
cat > "$ROOT/h75-runtime-image.state.json" <<JSON
{
  "target": "$TARGET",
  "acr_image": "$TARGET_IMAGE",
  "runtime_ref": "$RUNTIME_REF",
  "openclaw_overlay_ref": "$CARHER_REF",
  "hermes_ref": "$HERMES_REF",
  "openclaw_base_image": "$OPENCLAW_BASE_IMAGE",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "[h75-build] done"
