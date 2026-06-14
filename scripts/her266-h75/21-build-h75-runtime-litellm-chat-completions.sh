#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
WORK_DIR="$ROOT/runtime-litellm-chat-completions"
mkdir -p "$LOG_DIR" "$WORK_DIR"
exec > >(tee -a "$LOG_DIR/21-build-h75-runtime-litellm-chat-completions.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

BASE_IMAGE="${BASE_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-20260530}"
TARGET_IMAGE="${TARGET_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-litellmchat-20260530}"

echo "[runtime-litellm-chat] base=$BASE_IMAGE"
echo "[runtime-litellm-chat] target=$TARGET_IMAGE"

cid="$(nerdctl create --entrypoint sh "$BASE_IMAGE" -lc true)"
cleanup() {
  nerdctl rm -f "$cid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

nerdctl cp "$cid":/entrypoint.sh "$WORK_DIR/entrypoint.sh"
nerdctl cp "$cid":/opt/carher-runtime/templates/hermes-config.carher-pro.yaml "$WORK_DIR/hermes-config.carher-pro.yaml"

python3 - "$WORK_DIR/entrypoint.sh" "$WORK_DIR/hermes-config.carher-pro.yaml" <<'PY'
from pathlib import Path
import sys

entrypoint = Path(sys.argv[1])
template = Path(sys.argv[2])

template_text = template.read_text()
if "api_mode: \"codex_responses\"" not in template_text:
    raise SystemExit("template no longer contains codex_responses; check if patch is still needed")
template_text = template_text.replace("api_mode: \"codex_responses\"", "api_mode: \"chat_completions\"")
template_text = template_text.replace("transport: \"codex_responses\"", "transport: \"chat_completions\"")
template.write_text(template_text)

entrypoint_text = entrypoint.read_text()
old = entrypoint_text
entrypoint_text = entrypoint_text.replace(
    'top_level_model_key(text, "api_mode") == "codex_responses"',
    'top_level_model_key(text, "api_mode") == "chat_completions"',
)
entrypoint_text = entrypoint_text.replace(
    'provider_key(text, "chatgpt-pro", "api_mode") == "codex_responses"',
    'provider_key(text, "chatgpt-pro", "api_mode") == "chat_completions"',
)
entrypoint_text = entrypoint_text.replace(
    'provider_key(text, "chatgpt-pro", "transport") == "codex_responses"',
    'provider_key(text, "chatgpt-pro", "transport") == "chat_completions"',
)
entrypoint_text = entrypoint_text.replace(
    'custom_provider_key(text, "chatgpt-pro", "api_mode") == "codex_responses"',
    'custom_provider_key(text, "chatgpt-pro", "api_mode") == "chat_completions"',
)
entrypoint_text = entrypoint_text.replace(
    'custom_provider_key(text, "chatgpt-pro", "transport") == "codex_responses"',
    'custom_provider_key(text, "chatgpt-pro", "transport") == "chat_completions"',
)
if entrypoint_text == old:
    raise SystemExit("entrypoint route match checks were not patched")
entrypoint.write_text(entrypoint_text)
PY

grep -q 'api_mode: "chat_completions"' "$WORK_DIR/hermes-config.carher-pro.yaml"
grep -q 'provider_key(text, "chatgpt-pro", "transport") == "chat_completions"' "$WORK_DIR/entrypoint.sh"

cat > "$WORK_DIR/Dockerfile" <<'DOCKER'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY entrypoint.sh /entrypoint.sh
COPY hermes-config.carher-pro.yaml /opt/carher-runtime/templates/hermes-config.carher-pro.yaml
RUN chmod 0755 /entrypoint.sh
LABEL carher.patch.hermes_litellm_transport="chat-completions-v1"
DOCKER

nerdctl build \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -t "$TARGET_IMAGE" \
  -f "$WORK_DIR/Dockerfile" \
  "$WORK_DIR"
nerdctl push "$TARGET_IMAGE"

nerdctl image inspect "$TARGET_IMAGE" --format '{{json .}}' > "$ROOT/h75-runtime-litellm-chat-completions.inspect.json"
cat > "$ROOT/h75-runtime-litellm-chat-completions.state.json" <<JSON
{
  "base_image": "$BASE_IMAGE",
  "target_image": "$TARGET_IMAGE",
  "patch": "Hermes chatgpt-pro uses LiteLLM chat_completions transport",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "[runtime-litellm-chat] done"
