#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
WORK_DIR="$ROOT/runtime-dual-engine-command-bypass"
mkdir -p "$LOG_DIR" "$WORK_DIR"
exec > >(tee -a "$LOG_DIR/19-build-h75-runtime-dual-engine-command-bypass.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

BASE_IMAGE="${BASE_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-cmdbypass-20260530}"
TARGET_IMAGE="${TARGET_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-dualswitch-20260530}"

echo "[runtime-dualswitch] base=$BASE_IMAGE"
echo "[runtime-dualswitch] target=$TARGET_IMAGE"

cat > "$WORK_DIR/hermes-engine-swap-command-bypass.sh" <<'PATCH'
#!/usr/bin/env bash
set -euo pipefail

TARGET="${HERMES_FEISHU_PLATFORM_FILE:-/opt/hermes/source/gateway/platforms/feishu.py}"
if [ ! -f "$TARGET" ]; then
  echo "hermes-engine-swap-command-bypass.sh: target not found: $TARGET" >&2
  exit 2
fi

if [ "${CARHER_DISABLE_HERMES_ENGINE_SWAP_COMMAND_BYPASS_PATCH:-0}" = "1" ]; then
  echo "hermes-engine-swap-command-bypass.sh: disabled via CARHER_DISABLE_HERMES_ENGINE_SWAP_COMMAND_BYPASS_PATCH"
  exit 0
fi

python3 - "$TARGET" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
marker = "CARHER_HERMES_ENGINE_SWAP_NO_MENTION_BYPASS"
if marker in text:
    print(f"hermes-engine-swap-command-bypass.sh: already patched ({path})")
    raise SystemExit(0)

needle = '''        if require_mention and not self._mentions_self(message):
            return "group_policy_rejected"
'''
replacement = '''        carher_engine_swap_command = False  # CARHER_HERMES_ENGINE_SWAP_NO_MENTION_BYPASS
        if require_mention:
            try:
                import json as _carher_swap_admit_json
                import os as _carher_swap_admit_os
                import re as _carher_swap_admit_re
                _carher_swap_home = _carher_swap_admit_os.getenv("FEISHU_HOME_CHANNEL", "").strip()
                _carher_swap_chat = str(chat_id or "").strip()
                if _carher_swap_home and _carher_swap_chat == _carher_swap_home:
                    _carher_swap_raw = getattr(message, "content", "") or ""
                    _carher_swap_text = str(_carher_swap_raw or "")
                    try:
                        _carher_swap_parsed = _carher_swap_admit_json.loads(_carher_swap_raw)
                        if isinstance(_carher_swap_parsed, dict):
                            _carher_swap_texts = []
                            def _carher_swap_walk(value):
                                if isinstance(value, str):
                                    _carher_swap_texts.append(value)
                                elif isinstance(value, dict):
                                    for _v in value.values():
                                        _carher_swap_walk(_v)
                                elif isinstance(value, list):
                                    for _v in value:
                                        _carher_swap_walk(_v)
                            _carher_swap_walk(_carher_swap_parsed)
                            _carher_swap_text = " ".join(_carher_swap_texts)
                    except Exception:
                        pass
                    carher_engine_swap_command = bool(_carher_swap_admit_re.match(r"^/(?:hermes|openclaw)(?:\\s|$)", _carher_swap_text.strip(), _carher_swap_admit_re.I))
            except Exception:
                carher_engine_swap_command = False
        if require_mention and not self._mentions_self(message) and not carher_engine_swap_command:
            return "group_policy_rejected"
'''

if needle not in text:
    raise SystemExit("Hermes mention gate anchor not found")

backup = path.with_suffix(path.suffix + ".bak.engine-swap-command-bypass")
if not backup.exists():
    backup.write_text(text)
path.write_text(text.replace(needle, replacement, 1))
print(f"hermes-engine-swap-command-bypass.sh: patched {path}")
PY

/opt/hermes/venv/bin/python -m py_compile "$TARGET"
PATCH

chmod 0755 "$WORK_DIR/hermes-engine-swap-command-bypass.sh"

cat > "$WORK_DIR/Dockerfile" <<'DOCKER'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY hermes-engine-swap-command-bypass.sh /runtime-patches/hermes-engine-swap-command-bypass.sh
RUN chmod 0755 /runtime-patches/hermes-engine-swap-command-bypass.sh
LABEL carher.patch.hermes_engine_swap_no_mention="home-channel-/openclaw-/hermes-v1"
DOCKER

nerdctl build \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -t "$TARGET_IMAGE" \
  -f "$WORK_DIR/Dockerfile" \
  "$WORK_DIR"
nerdctl push "$TARGET_IMAGE"

nerdctl image inspect "$TARGET_IMAGE" --format '{{json .}}' > "$ROOT/h75-runtime-dual-engine-command-bypass.inspect.json"
cat > "$ROOT/h75-runtime-dual-engine-command-bypass.state.json" <<JSON
{
  "base_image": "$BASE_IMAGE",
  "target_image": "$TARGET_IMAGE",
  "patch": "allow explicit /hermes and /openclaw through both OpenClaw and Hermes Feishu group mention gates",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "[runtime-dualswitch] done"
