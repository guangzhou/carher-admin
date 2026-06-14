#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/root/her266-h75-rollout}"
LOG_DIR="$ROOT/logs"
WORK_DIR="$ROOT/runtime-chatid-fallback"
mkdir -p "$LOG_DIR" "$WORK_DIR"
exec > >(tee -a "$LOG_DIR/20-build-h75-runtime-chatid-fallback.$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1

BASE_IMAGE="${BASE_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-dualswitch-20260530}"
TARGET_IMAGE="${TARGET_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-b600887-acpfast-feishu-dualswitch-chatidfix-20260530}"

echo "[runtime-chatidfix] base=$BASE_IMAGE"
echo "[runtime-chatidfix] target=$TARGET_IMAGE"

cat > "$WORK_DIR/patch-chatid-fallback.js" <<'NODE'
const fs = require("fs");

function replaceOnce(text, needle, replacement, label) {
  if (!text.includes(needle)) {
    throw new Error(`${label} anchor not found`);
  }
  return text.replace(needle, replacement);
}

function patchOpenClawEngineSwap(path) {
  let code = fs.readFileSync(path, "utf8");
  const marker = "CARHER_ENGINE_SWAP_CHATID_FALLBACK_V1";
  if (code.includes(marker)) {
    console.log(`patch-chatid-fallback: already patched ${path}`);
    return;
  }
  const backup = `${path}.bak.chatid-fallback`;
  if (!fs.existsSync(backup)) {
    fs.writeFileSync(backup, code);
  }

  const oldExtractor = `function extractFeishuChatId(value: unknown): string {
  if (typeof value !== "string") {return "";}
  const normalized = normalizeChatId(value);
  if (normalized.startsWith("oc_")) {return normalized;}
  const match = value.match(/oc_[0-9a-f]+/i);
  return match?.[0] ?? "";
}
`;
  const newExtractor = `function extractFeishuChatId(value: unknown, depth = 0): string {
  // ${marker}: Feishu hook payloads differ between OpenClaw, Hermes, and Lark SDK versions.
  if (typeof value === "string") {
    const normalized = normalizeChatId(value);
    if (normalized.startsWith("oc_")) {return normalized;}
    const match = value.match(/oc_[0-9a-f]+/i);
    return match?.[0] ?? "";
  }
  if (!value || typeof value !== "object" || depth > 4) {return "";}
  if (Array.isArray(value)) {
    for (const item of value) {
      const chatId = extractFeishuChatId(item, depth + 1);
      if (chatId) {return chatId;}
    }
    return "";
  }
  const record = value as Record<string, unknown>;
  for (const key of [
    "chat_id",
    "chatId",
    "channel_id",
    "channelId",
    "conversationId",
    "sessionKey",
    "channel",
    "id",
  ]) {
    const chatId = extractFeishuChatId(record[key], depth + 1);
    if (chatId) {return chatId;}
  }
  for (const key of ["message", "raw", "rawMessage", "event", "metadata", "source", "context", "payload"]) {
    const chatId = extractFeishuChatId(record[key], depth + 1);
    if (chatId) {return chatId;}
  }
  return "";
}
`;
  code = replaceOnce(code, oldExtractor, newExtractor, "OpenClaw chat id extractor");

  const oldCandidates = `    ctx?.channelId,
    event?.chatId,
    event?.chat_id,
    event?.conversationId,
    event?.sessionKey,
    event?.channel,
    event?.message?.chat_id,
    event?.message?.chatId,
`;
  const newCandidates = `    ctx?.channelId,
    ctx?.channel_id,
    ctx?.message?.chat_id,
    ctx?.message?.chatId,
    ctx?.message?.channel_id,
    ctx?.message?.channelId,
    event?.chatId,
    event?.chat_id,
    event?.channelId,
    event?.channel_id,
    event?.conversationId,
    event?.sessionKey,
    event?.channel,
    event?.message?.chat_id,
    event?.message?.chatId,
    event?.message?.channel_id,
    event?.message?.channelId,
`;
  code = replaceOnce(code, oldCandidates, newCandidates, "OpenClaw chat id candidates");

  code = replaceOnce(
    code,
    `            const chatId = resolveFeishuChatId(event, ctx, senderId);
            const chatType = resolveFeishuChatType(event, ctx);
`,
    `            let chatId = resolveFeishuChatId(event, ctx, senderId);
            const chatType = resolveFeishuChatType(event, ctx);
`,
    "OpenClaw chatId const",
  );

  code = replaceOnce(
    code,
    `            if (command.kind === "memory") {
              if (!chatId.startsWith("oc_")) {
`,
    `            if (command.kind === "memory") {
              if (!chatId) {chatId = resolveConfiguredHomeChatId("");}
              if (!chatId.startsWith("oc_")) {
`,
    "OpenClaw memory fallback",
  );

  code = replaceOnce(
    code,
    `            if (!chatId.startsWith("oc_")) {
              return { handled: true, text: "❌ 切换失败:无法识别飞书 chat_id。" };
            }
`,
    `            if (!chatId) {chatId = resolveConfiguredHomeChatId("");}
            if (!chatId.startsWith("oc_")) {
              log(
                "warn",
                \`chat id unresolved for engine switch: target=\${target} sender=\${senderId} eventKeys=\${Object.keys(event ?? {}).join(",")} ctxKeys=\${Object.keys(ctx ?? {}).join(",")}\`,
              );
              return { handled: true, text: "❌ 切换失败:无法识别飞书 chat_id。" };
            }
`,
    "OpenClaw switch fallback",
  );

  fs.writeFileSync(path, code);
  console.log(`patch-chatid-fallback: patched ${path}`);
}

function patchHermesSwapScript(path) {
  let code = fs.readFileSync(path, "utf8");
  const marker = "CARHER_HERMES_SWAP_CHATID_FALLBACK_V1";
  if (code.includes(marker)) {
    console.log(`patch-chatid-fallback: already patched ${path}`);
    return;
  }
  const backup = `${path}.bak.chatid-fallback`;
  if (!fs.existsSync(backup)) {
    fs.writeFileSync(backup, code);
  }

  const adminAnchor = `                    _carher_swap_admin_open_id = _carher_swap_os.getenv("FEISHU_ADMIN_OPEN_ID", "").strip()
                    _carher_swap_sender_id = ""
`;
  const helper = `                    def _carher_swap_extract_chat_id(_carher_swap_value, _carher_swap_depth=0):
                        # ${marker}: Hermes/Lark SDK payloads may expose chat id under channel_id or raw nested fields.
                        try:
                            if _carher_swap_value is None or _carher_swap_depth > 4:
                                return ""
                            if isinstance(_carher_swap_value, str):
                                _carher_swap_text = _carher_swap_value.strip()
                                if _carher_swap_text.lower().startswith("feishu:"):
                                    _carher_swap_text = _carher_swap_text.split(":", 1)[1].strip()
                                if _carher_swap_text.startswith("oc_"):
                                    return _carher_swap_text
                                import re as _carher_swap_chat_re
                                _carher_swap_match = _carher_swap_chat_re.search(r"oc_[0-9a-f]+", _carher_swap_value, _carher_swap_chat_re.I)
                                return _carher_swap_match.group(0) if _carher_swap_match else ""
                            if isinstance(_carher_swap_value, dict):
                                for _carher_swap_key in ("chat_id", "chatId", "channel_id", "channelId", "conversationId", "sessionKey", "channel", "id"):
                                    _carher_swap_found = _carher_swap_extract_chat_id(_carher_swap_value.get(_carher_swap_key), _carher_swap_depth + 1)
                                    if _carher_swap_found:
                                        return _carher_swap_found
                                for _carher_swap_key in ("message", "raw", "rawMessage", "event", "metadata", "source", "context", "payload"):
                                    _carher_swap_found = _carher_swap_extract_chat_id(_carher_swap_value.get(_carher_swap_key), _carher_swap_depth + 1)
                                    if _carher_swap_found:
                                        return _carher_swap_found
                            if isinstance(_carher_swap_value, (list, tuple)):
                                for _carher_swap_item in _carher_swap_value:
                                    _carher_swap_found = _carher_swap_extract_chat_id(_carher_swap_item, _carher_swap_depth + 1)
                                    if _carher_swap_found:
                                        return _carher_swap_found
                            for _carher_swap_attr in ("chat_id", "chatId", "channel_id", "channelId", "conversationId", "sessionKey", "channel", "id", "message", "raw", "metadata"):
                                if hasattr(_carher_swap_value, _carher_swap_attr):
                                    _carher_swap_found = _carher_swap_extract_chat_id(getattr(_carher_swap_value, _carher_swap_attr, None), _carher_swap_depth + 1)
                                    if _carher_swap_found:
                                        return _carher_swap_found
                        except Exception:
                            return ""
                        return ""
                    def _carher_swap_resolve_chat_id(_carher_swap_message):
                        _carher_swap_found = _carher_swap_extract_chat_id(_carher_swap_message)
                        if _carher_swap_found:
                            return _carher_swap_found
                        return _carher_swap_extract_chat_id(_carher_swap_os.getenv("FEISHU_HOME_CHANNEL", ""))
                    _carher_swap_admin_open_id = _carher_swap_os.getenv("FEISHU_ADMIN_OPEN_ID", "").strip()
                    _carher_swap_sender_id = ""
`;
  code = replaceOnce(code, adminAnchor, helper, "Hermes chat id helper");
  code = code.replaceAll(
    `_carher_swap_chat = getattr(message, "chat_id", None)`,
    `_carher_swap_chat = _carher_swap_resolve_chat_id(message)`,
  );
  fs.writeFileSync(path, code);
  console.log(`patch-chatid-fallback: patched ${path}`);
}

function patchHermesMentionBypass(path) {
  let code = fs.readFileSync(path, "utf8");
  const marker = "CARHER_HERMES_MENTION_CHATID_FALLBACK_V1";
  if (code.includes(marker)) {
    console.log(`patch-chatid-fallback: already patched ${path}`);
    return;
  }
  const backup = `${path}.bak.chatid-fallback`;
  if (!fs.existsSync(backup)) {
    fs.writeFileSync(backup, code);
  }
  code = replaceOnce(
    code,
    `                _carher_swap_chat = str(chat_id or "").strip()
                if _carher_swap_home and _carher_swap_chat == _carher_swap_home:
`,
    `                _carher_swap_chat = str(chat_id or "").strip()
                if not _carher_swap_chat:
                    _carher_swap_chat = _carher_swap_home  # ${marker}
                if _carher_swap_home and _carher_swap_chat == _carher_swap_home:
`,
    "Hermes mention bypass fallback",
  );
  fs.writeFileSync(path, code);
  console.log(`patch-chatid-fallback: patched ${path}`);
}

const [openclawPath, hermesSwapPath, hermesBypassPath] = process.argv.slice(2);
patchOpenClawEngineSwap(openclawPath);
patchHermesSwapScript(hermesSwapPath);
patchHermesMentionBypass(hermesBypassPath);
NODE

cat > "$WORK_DIR/Dockerfile" <<'DOCKER'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY patch-chatid-fallback.js /tmp/patch-chatid-fallback.js
RUN node /tmp/patch-chatid-fallback.js \
      /openclaw-plugins/carher-engine-swap/index.ts \
      /runtime-patches/hermes-engine-swap.sh \
      /runtime-patches/hermes-engine-swap-command-bypass.sh \
 && bash -n /runtime-patches/hermes-engine-swap.sh \
 && bash -n /runtime-patches/hermes-engine-swap-command-bypass.sh \
 && rm -f /tmp/patch-chatid-fallback.js
LABEL carher.patch.engine_swap_chatid_fallback="openclaw-hermes-feishu-chatid-v1"
DOCKER

nerdctl build \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  -t "$TARGET_IMAGE" \
  -f "$WORK_DIR/Dockerfile" \
  "$WORK_DIR"
nerdctl push "$TARGET_IMAGE"

nerdctl image inspect "$TARGET_IMAGE" --format '{{json .}}' > "$ROOT/h75-runtime-chatid-fallback.inspect.json"
cat > "$ROOT/h75-runtime-chatid-fallback.state.json" <<JSON
{
  "base_image": "$BASE_IMAGE",
  "target_image": "$TARGET_IMAGE",
  "patch": "make /hermes and /openclaw resolve Feishu chat_id from channelId/raw payloads and FEISHU_HOME_CHANNEL",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
echo "[runtime-chatidfix] done"
