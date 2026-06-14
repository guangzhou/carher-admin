#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
WORK_DIR="$ROOT/runtime-engine-command-bypass"
mkdir -p "$LOG_DIR" "$WORK_DIR"
exec > >(tee -a "$LOG_DIR/18-build-h75-runtime-engine-command-bypass.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

BASE_IMAGE="${BASE_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-20260530}"
TARGET_IMAGE="${TARGET_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-cmdbypass-20260530}"

echo "[runtime-cmdbypass] base=$BASE_IMAGE"
echo "[runtime-cmdbypass] target=$TARGET_IMAGE"

cat > "$WORK_DIR/apply-engine-swap-command-bypass.sh" <<'PATCH'
#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ] || [ ! -f "$TARGET" ]; then
  echo "apply-engine-swap-command-bypass.sh: target not found: $TARGET" >&2
  exit 2
fi

if [ "${CARHER_DISABLE_ENGINE_SWAP_COMMAND_BYPASS_PATCH:-0}" = "1" ]; then
  echo "apply-engine-swap-command-bypass.sh: disabled via CARHER_DISABLE_ENGINE_SWAP_COMMAND_BYPASS_PATCH"
  exit 0
fi

node - "$TARGET" <<'NODE'
const fs = require("fs");

const target = process.argv[2];
const marker = "CARHER_ENGINE_SWAP_NO_MENTION_BYPASS";
let code = fs.readFileSync(target, "utf8");

if (code.includes(marker)) {
  console.log(`apply-engine-swap-command-bypass.sh: already patched (${target})`);
  process.exit(0);
}

const backup = `${target}.bak.engine-swap-command-bypass`;
if (!fs.existsSync(backup)) {
  fs.writeFileSync(backup, code);
}

const needle = `    if (requireMention && !(0, mention_1.mentionedBot)(ctx)) {\n        // Check if @all mention should bypass the mention requirement`;
const replacement = `    const carherEngineSwapCommand = typeof ctx.content === "string" && /^\\/(?:hermes|openclaw)(?:\\s|$)/i.test(ctx.content.trim()); // ${marker}\n    if (requireMention && !(0, mention_1.mentionedBot)(ctx) && !carherEngineSwapCommand) {\n        // Check if @all mention should bypass the mention requirement`;

if (!code.includes(needle)) {
  throw new Error("mention gate anchor not found");
}

code = code.replace(needle, replacement);
fs.writeFileSync(target, code);
NODE

if ! CHECK_OUTPUT=$(node --check "$TARGET" 2>&1); then
  echo "$CHECK_OUTPUT" >&2
  echo "apply-engine-swap-command-bypass.sh: node --check failed after patch, restoring backup" >&2
  cp "$TARGET.bak.engine-swap-command-bypass" "$TARGET"
  exit 4
fi

echo "apply-engine-swap-command-bypass.sh: patched $TARGET"
PATCH

chmod 0755 "$WORK_DIR/apply-engine-swap-command-bypass.sh"

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
marker = "CARHER_ENGINE_SWAP_COMMAND_BYPASS_ENTRYPOINT"
if marker in text:
    print("entrypoint already patched")
    raise SystemExit(0)

var_anchor = '      LARK_OUTBOUND="$LARK_PKG/src/messaging/outbound/outbound.js"\n'
var_replacement = var_anchor + '      LARK_GATE="$LARK_PKG/src/messaging/inbound/gate.js"\n'
if var_anchor not in text:
    raise SystemExit("LARK_OUTBOUND anchor not found")
text = text.replace(var_anchor, var_replacement, 1)

call_anchor = '''      if [ -f "$LARK_REPLY_MODE" ]; then
        bash "$CARHER_PATCHES_DIR/apply-reply-card-default.sh" "$LARK_REPLY_MODE" || \\
          echo "  ✗ reply-card default patch failed" >&2
      else
        echo "  ⚠ $LARK_REPLY_MODE not found — reply-card patch skipped"
      fi
'''

call_block = call_anchor + f'''
      # {marker}: let explicit /hermes and /openclaw reach engine-swap
      # even in the owner home group when the message does not @ the bot.
      if [ -f "$CARHER_PATCHES_DIR/apply-engine-swap-command-bypass.sh" ] && [ -f "$LARK_GATE" ]; then
        bash "$CARHER_PATCHES_DIR/apply-engine-swap-command-bypass.sh" "$LARK_GATE" || \\
          echo "  ✗ engine-swap command bypass patch failed" >&2
      fi
'''

if call_anchor not in text:
    raise SystemExit("reply-card patch call anchor not found")
text = text.replace(call_anchor, call_block, 1)
path.write_text(text)
PY

grep -q "CARHER_ENGINE_SWAP_COMMAND_BYPASS_ENTRYPOINT" "$WORK_DIR/entrypoint.sh"

cat > "$WORK_DIR/Dockerfile" <<'DOCKER'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY apply-engine-swap-command-bypass.sh /carher-patches/apply-engine-swap-command-bypass.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /carher-patches/apply-engine-swap-command-bypass.sh /entrypoint.sh
LABEL carher.patch.engine_swap_no_mention="openclaw-lark-/hermes-/openclaw-v1"
DOCKER

nerdctl build \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -t "$TARGET_IMAGE" \
  -f "$WORK_DIR/Dockerfile" \
  "$WORK_DIR"
nerdctl push "$TARGET_IMAGE"

nerdctl image inspect "$TARGET_IMAGE" --format '{{json .}}' > "$ROOT/h75-runtime-engine-command-bypass.inspect.json"
cat > "$ROOT/h75-runtime-engine-command-bypass.state.json" <<JSON
{
  "base_image": "$BASE_IMAGE",
  "target_image": "$TARGET_IMAGE",
  "patch": "allow explicit /hermes and /openclaw through OpenClaw Feishu group mention gate",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "[runtime-cmdbypass] done"
