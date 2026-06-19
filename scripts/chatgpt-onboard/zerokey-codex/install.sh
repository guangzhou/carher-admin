#!/usr/bin/env bash
# install.sh — bootstrap the zerokey-codex ChatGPT-web→API bridge on a host
# (designed for the JP-exit server 188 / 10.68.13.188).
#
# It clones upstream zerokey, overlays our patches (raw passthrough + per-request
# model + real /v1/models + headless launcher + Dockerfiles + ops), and lays out
# the persistent state dirs. It does NOT capture a session — run the capture
# step afterwards (see ops/README.md) to produce state/users.json.
#
# Usage:
#   ./install.sh [TARGET_DIR]      # default: ~/zerokey-codex
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$HOME/zerokey-codex}"
UPSTREAM="https://github.com/downloaddoctor/zerokey.git"

echo "[install] target: $TARGET"
mkdir -p "$TARGET"/{state/out/screenshots,state/profile,secrets,capture,ops,logs}

# 1) upstream zerokey
if [[ ! -d "$TARGET/zerokey/.git" ]]; then
  echo "[install] cloning upstream zerokey…"
  git clone --depth 1 "$UPSTREAM" "$TARGET/zerokey"
else
  echo "[install] upstream already present, skipping clone"
fi

# 2) overlay our patches onto the clone
echo "[install] applying patches…"
cp "$HERE/zerokey-patch/routes/raw.js"          "$TARGET/zerokey/routes/raw.js"
cp "$HERE/zerokey-patch/routes/chatgpt.js"      "$TARGET/zerokey/routes/chatgpt.js"
cp "$HERE/zerokey-patch/core/chatgpt/api.js"    "$TARGET/zerokey/core/chatgpt/api.js"
cp "$HERE/zerokey-patch/config/constants.js"    "$TARGET/zerokey/config/constants.js"
cp "$HERE/zerokey-patch/zerokey-serve-codex.js" "$TARGET/zerokey/zerokey-serve-codex.js"
cp "$HERE/zerokey-patch/Dockerfile"             "$TARGET/zerokey/Dockerfile"
cp "$HERE/zerokey-patch/.dockerignore"          "$TARGET/zerokey/.dockerignore"
cp "$HERE/zerokey-patch/docker-compose.yml"     "$TARGET/zerokey/docker-compose.yml"

# 3) capture image + ops
cp "$HERE/capture/Dockerfile"                   "$TARGET/capture/Dockerfile"
cp "$HERE/capture/zerokey-web-capture.py"       "$TARGET/capture/zerokey-web-capture.py"
cp "$HERE/ops/refresh.sh"                        "$TARGET/ops/refresh.sh"
cp "$HERE/ops/capture-manual.sh"                 "$TARGET/ops/capture-manual.sh"
cp "$HERE/ops/add-account.sh"                    "$TARGET/ops/add-account.sh"
cp "$HERE/ops/litellm-register-zerokey.py"        "$TARGET/ops/litellm-register-zerokey.py"
cp "$HERE/ops/docker-compose.account.yml"        "$TARGET/ops/docker-compose.account.yml"
cp "$HERE/ops/README.md"                         "$TARGET/ops/README.md"
chmod +x "$TARGET/ops/"*.sh

# 4) install node deps into the clone (so the image can vendor them)
if [[ ! -d "$TARGET/zerokey/node_modules" ]]; then
  echo "[install] npm install (in clone)…"
  (cd "$TARGET/zerokey" && npm install --omit=dev --no-audit --no-fund)
fi

cat <<EOF

[install] done. Next:
  1) put credentials:
       echo '<webmail-pw>'  > $TARGET/secrets/mail_pw.txt
       echo '<chatgpt-pw>'  > $TARGET/secrets/chatgpt_pw.txt
  2) build images:
       (cd $TARGET/capture && docker build -t zerokey-capture:latest .)
       (cd $TARGET/zerokey && docker compose build)
  3) capture a session (writes state/users.json), then:
       (cd $TARGET/zerokey && docker compose up -d)
  See $TARGET/ops/README.md for capture + refresh details.
EOF
