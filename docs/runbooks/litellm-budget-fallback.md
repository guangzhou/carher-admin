# LiteLLM Budget Fallback Runbook

## Purpose

For an explicitly enabled LiteLLM virtual key, switch all allowed public
generation model names to the isolated zero-cost GPT-5.3 group when the key
reaches 98% of its periodic monetary budget. Restore the saved configuration
after the original budget reset time.

## Safety properties

- The client keeps the same API key and requested public model name.
- The fallback target is `chatgpt-budget-fallback-gpt-5.3`.
- Its members mirror the live `zerokey-pool-gpt-5.3-codex` deployments, but
  are registered under isolated IDs with all monetary cost fields set to zero.
- The isolated group has no paid router fallback.
- Policies are disabled by default and enabled one key at a time.
- Plaintext API keys are not stored in the admin database.
- Configuration conflicts enter `MANUAL_HOLD`; the controller does not overwrite
  external changes.

## Pre-deployment checks

Inspect the manifest:

```bash
python -m pytest backend/tests/test_budget_fallback_model.py -v
```

Run the idempotent production sync, then query `/v1/model/info` with the master
key and verify
every `chatgpt-budget-fallback-gpt-5.3` row has:

```json
{
  "input_cost_per_token": 0,
  "output_cost_per_token": 0,
  "cache_read_input_token_cost": 0
}
```

Inspect the running router configuration and confirm there is no fallback whose
source is `chatgpt-budget-fallback-gpt-5.3`.

Send one minimal `/v1/chat/completions` request to the isolated group and verify
the selected model ID starts with `budget-fallback/zk-`. LiteLLM's generic
`/health?model=` probe is not reliable for these Responses-backed zerokey
members, so the Admin health check uses this same minimal real request.

Do not enable any policy if either check fails.

```bash
LITELLM_BASE=https://cc.auto-link.com.cn/pro \
LITELLM_MK="$LITELLM_MASTER_KEY" \
python scripts/litellm-budget-fallback-sync.py sync --apply --probe
```

The `carher` namespace LiteLLM ConfigMap must not declare this group. The
authoritative source and target groups live in the separate `litellm-product`
cluster and are managed through LiteLLM's database-backed model API.

## Deployment boundaries

For local UI-only verification without a kubeconfig, set
`CARHER_ADMIN_SKIP_K8S=1`. This skips K8s sync and metrics workers but still
starts the database, budget-fallback worker, API, and built frontend.

The repository has two independent deployment pipelines:

1. Build and deploy `carher-admin` on build server `47.84.112.136` using the
   repository's normal nerdctl/admin deployment procedure. Do not use GitHub
   Actions for this image.
2. Apply the LiteLLM manifest through the `litellm-product` deployment process.
   Do not mix this with the CarHer bot image pipeline.

Use `kubectl apply` or `kubectl set image` followed by
`kubectl rollout status`; do not manually delete serving pods.

## One-key canary

1. Choose a disposable or designated non-admin virtual key with positive
   `max_budget`, a `budget_duration`, and a future `budget_reset_at`.
2. Open the Admin `预算路由` page.
3. Verify the header reports `5.3 零成本通道就绪`.
4. Open the key detail and enable `预算用尽后切换 5.3`.
5. Observe the row until its utilization reaches 98%, or use `立即切到 5.3`
   for a controlled canary.
6. Verify the state becomes `5.3 兜底` and the Key remains visible.
7. Send a request using the same client API key and original public model name.
8. Inspect SpendLogs: token/request audit data should appear, the selected model
   should be the isolated fallback group, and monetary spend must not increase.
9. At reset time, verify the state returns to `正常`, spend starts at zero, and
   the original models and aliases are restored.

## API operations

All paths are under `/api/litellm/budget-fallback` and require Admin auth.

```text
GET  /keys
GET  /keys/{key_id}/events
POST /keys/{key_id}/enable       {"key_id":"<hash>"}
POST /keys/{key_id}/disable      {"restore":true}
POST /keys/{key_id}/fallback     {"reason":"canary"}
POST /keys/{key_id}/restore      {"reason":"rollback"}
POST /keys/{key_id}/recapture    {"reason":"accept reviewed config"}
POST /keys/{key_id}/pause
POST /keys/{key_id}/resume
```

`key_id` is the LiteLLM token hash/identifier, never the plaintext API key.

## MANUAL_HOLD recovery

1. Read `last_error` and the event timeline.
2. Inspect the live LiteLLM key configuration.
3. If the external change is intentional, use `重新采集配置` only after verifying
   the current key has a positive periodic budget and the desired paid routing.
4. If the external change is accidental and the saved snapshot is authoritative,
   use `恢复主模型`.
5. Resume automatic control only after the state and live routing match.

Never recapture an active fallback configuration as the paid baseline.

## Rollback

1. For every enabled policy in fallback, call disable with `restore=true`.
2. Confirm all policies are disabled and no key remains in `FALLBACK_PENDING`,
   `FALLBACK_5_3`, or `RESTORING`.
3. Roll back `carher-admin` through its build-server deployment pipeline.
4. Roll back the LiteLLM manifest through the separate `litellm-product`
   pipeline.
5. Verify native LiteLLM budget rejection still applies to keys after rollback.
