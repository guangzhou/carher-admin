---
name: litellm-key-image-route
description: >-
  Use when a single 198 Pro LiteLLM Cursor/Codex virtual key should route normal
  text/code requests to a local model while image requests must bypass to a
  vision-capable upstream, or when debugging slow image requests caused by
  local text-only models such as DeepSeek V4-Flash.
---

# LiteLLM Per-Key Image Route

## Scope

Use this for one-key gated routing on 198 Pro (`AIYJY-litellm`,
`litellm-product`). The pattern keeps a key's normal aliases intact, but adds a
LiteLLM pre-call hook that detects image input and rewrites only that request to
a vision-capable model group.

Verified shape:

```text
Cursor key -> gpt-5.5
  text request  -> per-key alias -> local-deepseek-v4-flash-responses
  image request -> hook rewrite  -> chatgpt-gpt-5.5
```

## Safety Rules

- Gate by `key_alias`; never make this global unless explicitly requested.
- Do not write real `sk-*` keys, cookies, or API keys into skills, manifests, or
  command history snippets.
- Test in canary first for new key/model combinations. Canary must use a
  separate Deployment/Service and must not be attached to the production
  NodePort or public gateway.
- Keep per-key aliases unchanged unless the task is specifically to change
  aliases. This hook solves request-level image routing, not model-list policy.
- Back up live ConfigMaps/Deployment and local manifests before production
  apply.

## Script

Use the repository script:

```bash
scripts/litellm-key-image-route.sh canary \
  --key-alias cursor-baiyu-thga \
  --image-model chatgpt-gpt-5.5

scripts/litellm-key-image-route.sh deploy \
  --key-alias cursor-baiyu-thga \
  --image-model chatgpt-gpt-5.5

scripts/litellm-key-image-route.sh status \
  --key-alias cursor-baiyu-thga

scripts/litellm-key-image-route.sh cleanup-canary
```

The script defaults to:

- asset: `AIYJY-litellm`
- namespace: `litellm-product`
- live Deployment: `litellm-proxy`
- hook module: `baiyu_image_route.py`
- callback name: `baiyu_image_route.baiyu_image_route`
- text models: `gpt-5.5,chatgpt-gpt-5.5,local-deepseek-v4-flash-responses,local-deepseek-v4-flash`

## Validation

After deploy, run a text request and an image request with the target virtual
key, then verify SpendLogs:

```bash
scripts/jms ssh AIYJY-litellm 'bash -s' <<'REMOTE'
set -euo pipefail
NS=litellm-product
KEY_ALIAS=cursor-baiyu-thga
TOKEN=$(kubectl exec litellm-db-0 -n "$NS" -- psql -U litellm -d litellm -At -c \
  "SELECT token FROM \"LiteLLM_VerificationToken\" WHERE key_alias='${KEY_ALIAS}';" | tr -d '[:space:]')
kubectl exec litellm-db-0 -n "$NS" -- psql -U litellm -d litellm -c "
SELECT to_char(sl.\"startTime\" AT TIME ZONE 'Asia/Shanghai','HH24:MI:SS') AS t,
       sl.model, sl.model_group, sl.custom_llm_provider, sl.status,
       sl.request_duration_ms, sl.prompt_tokens, sl.completion_tokens
FROM \"LiteLLM_SpendLogs\" sl
WHERE sl.api_key='${TOKEN}'
  AND sl.\"startTime\" > NOW() - INTERVAL '15 minutes'
ORDER BY sl.\"startTime\" DESC
LIMIT 10;"
REMOTE
```

Expected:

- Text: `model_group=local-deepseek-v4-flash-responses`
- Image: `model_group=chatgpt-gpt-5.5`

## Rollback

For canary, delete temporary resources:

```bash
scripts/litellm-key-image-route.sh cleanup-canary
```

For production, use the backup directory printed by `deploy`:

```bash
scripts/jms ssh AIYJY-litellm 'bash -s' <<'REMOTE'
set -euo pipefail
NS=litellm-product
BDIR=/root/litellm-product-manifests/backups/key-image-route-YYYYMMDD-HHMMSS
kubectl apply -f "$BDIR/litellm-callbacks.live.yaml"
kubectl apply -f "$BDIR/litellm-config.live.yaml"
kubectl apply -f "$BDIR/litellm-proxy.live.yaml"
kubectl -n "$NS" rollout status deploy/litellm-proxy --timeout=600s
REMOTE
```

Also restore the three manifest files from the same backup directory if the
rollback should persist across future `kubectl apply` runs.

## Failure Modes

- If image requests still hit the local model, check the key alias in
  `LiteLLM_VerificationToken`; hook gating uses `key_alias`, not the visible
  `sk-*` value.
- If image requests fail on upstream schema, inspect whether the client sent
  Responses history items such as `reasoning`. The simple hook only routes; it
  does not normalize history.
- If replaying SpendLogs payloads, first validate `data:image/...;base64,...`.
  Logged payloads may be truncated or redacted and are not always valid replay
  fixtures.
