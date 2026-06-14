---
name: litellm-pro-gpt-products-convergence
version: 1.0.0
description: "Converge and verify 198 Pro LiteLLM GPT product models, cursor key allowlists, ChatGPT acct primary routing, and Wangsu fallback routing. Use when the user mentions 198 pro GPT 模型收敛, cursor key 模型权限, GPT 产品模型透传, Wangsu/OpenRouter leakage, fallback mock, or asks to run pro GPT regression reports."
metadata:
  requires:
    bins: ["python3", "bash", "kubectl"]
---

# 198 Pro GPT Products Convergence

> Narrow runbook for the 198 Pro LiteLLM GPT product-model convergence. It is script-first: do not hand-edit DB rows or ConfigMaps unless the scripted path is blocked and the user explicitly accepts the risk.

## Scope

Use this skill for:

- Converging `litellm-product` GPT product model routing and cursor key allowlists.
- Verifying `/v1/models` visibility, direct internal-model denial, normal ChatGPT acct routing, and fallback behavior.
- Investigating recent cursor traffic that unexpectedly hits Wangsu/OpenRouter/internal routes.
- Explaining `mock_testing_fallbacks=true` results, especially `MOCK_FALLBACK_NOT_TRIGGERED`.

Do not use this skill for Claude Max / CC Max routing, non-GPT model rollout, ChatGPT OAuth onboarding, or general LiteLLM upgrades. Use the broader LiteLLM ops skills for those.

## Target Matrix

Only these 9 GPT product models are user-visible and allowed on cursor keys:

| User model | Primary route | Fallback | Notes |
|---|---|---|---|
| `gpt-5.5` | `chatgpt-pool-gpt-5.5` | `wangsu-gpt-5.5` | Normal path must hit ChatGPT acct 5.5. |
| `chatgpt-gpt-5.5` | `chatgpt-pool-gpt-5.5` | `wangsu-gpt-5.5` | Same product capability as `gpt-5.5`. |
| `gpt-5.4` | `chatgpt-gpt-5.4` | `wangsu-gpt-5.4` | Normal path must hit ChatGPT acct 5.4. |
| `chatgpt-gpt-5.4` | `chatgpt-gpt-5.4` | `wangsu-gpt-5.4` | Normal path must hit ChatGPT acct 5.4. |
| `gpt-5.2` | `chatgpt-gpt-5.4` | `wangsu-gpt-5.4` | Historical product name carried by 5.4. |
| `gpt-5.3-codex` | product group `chatgpt-gpt-5.3-codex`, underlying acct model `chatgpt-gpt-5.3-codex-spark` | `wangsu7-gpt-5.3-codex` | Product name remains stable for users. |
| `chatgpt-gpt-5.3-codex` | product group `chatgpt-gpt-5.3-codex`, underlying acct model `chatgpt-gpt-5.3-codex-spark` | `wangsu7-gpt-5.3-codex` | Full codex acct model is not the normal-path target. |
| `gpt-5.4-mini` | `chatgpt-gpt-5.3-codex-spark` | `wangsu-gpt-5.4` | Wangsu has no dedicated mini; use closest GPT fallback. |
| `chatgpt-gpt-5.3-codex-spark` | `chatgpt-gpt-5.3-codex-spark` | `wangsu-gpt-5.4` | Wangsu has no spark; use closest GPT fallback. |

Hidden and direct-request-denied models include:

- GPT internal/fallback: `chatgpt-pool-gpt-5.5`, `wangsu-gpt-5.5`, `wangsu-gpt-5.4`, `wangsu7-gpt-5.3-codex`, all `openrouter-gpt-*`.
- Non-GPT: `gemini-3.1-pro-preview`, `glm-5`, `glm-5.1`, `minimax-m2.7`, `wangsu-gemini-*`, `wangsu-glm-*`.

## Core Scripts

Run from the repository root.

| Script | Purpose |
|---|---|
| `scripts/litellm-pro-gpt-products-converge.py` | Dry-run/apply/restore pro router + cursor key convergence. |
| `scripts/litellm-pro-gpt-products-regression.sh` | One-command pro strict regression; does not modify routing. |
| `scripts/litellm-dev-gpt-products-verify.py` | Unified dev/pro verifier; `--profile pro --strict --check-cursor-keys` is used by the runner. |

Reports are written to `reports/` as Markdown and JSON. Never paste virtual keys or master keys into reports, docs, or chat.

## Standard Workflow

1. **Static checks**

   ```bash
   python3 -m py_compile scripts/litellm-dev-gpt-products-verify.py scripts/litellm-pro-gpt-products-converge.py
   bash -n scripts/litellm-pro-gpt-products-regression.sh
   ```

2. **Dry-run convergence diff**

   ```bash
   python3 scripts/litellm-pro-gpt-products-converge.py --reports-dir reports
   ```

   Review the generated `reports/litellm-pro-gpt-products-converge-*.md`. The dry-run must show:

   - Config/router diff only for intended GPT aliases/fallbacks.
   - Cursor key diff targeting exactly 9 product models.
   - No product alias directly to Wangsu/OpenRouter.
   - Codex compatibility rows changing only `litellm_params.model` to `openai/chatgpt-gpt-5.3-codex-spark`, while preserving `api_key`, `api_base`, and `model_id`.

3. **Pre-apply strict regression**

   ```bash
   scripts/litellm-pro-gpt-products-regression.sh --reports-dir reports
   ```

   If primary routing or visibility already fails, treat the report as baseline evidence. Do not assume apply will fix unrelated upstream account failures.

4. **Apply convergence**

   ```bash
   python3 scripts/litellm-pro-gpt-products-converge.py --apply --reports-dir reports
   ```

   The script backs up ConfigMap, remote manifest, cursor key DB rows, and codex compatibility model rows under `/root/litellm-product-manifests/backups/`. Keep the backup directory from the report.

5. **Rollout status**

   ```bash
   scripts/jms ssh AIYJY-litellm \
     "kubectl -n litellm-product rollout status deployment/litellm-proxy --timeout=120s && \
      kubectl get pods -n litellm-product -l app=litellm-proxy -o wide"
   ```

   Do not manually delete serving pods.

6. **Post-apply strict regression**

   ```bash
   scripts/litellm-pro-gpt-products-regression.sh --reports-dir reports
   ```

   Report `PASS`, `FAIL`, and `BLOCKED` exactly as produced. Do not upgrade a `BLOCKED` result to `PASS` by inference.

7. **Optional short-window traffic observation**

   Query SpendLogs for cursor-key routes only when the user asks about live traffic after apply. Aggregate by route; do not print key aliases or tokens unless explicitly needed and safe.

## Acceptance Criteria

Treat convergence as operationally successful only when:

- Cursor audit: 366 cursor keys, or current cursor-key count, all have exactly the 9 product models.
- Cursor audit: 0 keys with hidden models, 0 keys with extra models, 0 keys with aliases, 0 direct internal aliases.
- `/v1/models` with a temporary test key exposes only the 9 product models.
- Direct requests to internal/fallback/non-GPT models return `403 key_model_access_denied` or model not found.
- Normal path for all 9 products returns HTTP 200 and SpendLogs show ChatGPT acct routes, not Wangsu/OpenRouter.
- Codex product groups keep the user-requested model group but the actual `model` contains `gpt-5.3-codex-spark`.
- Temporary test key cleanup returns remaining count 0.
- Config checksum after regression is unchanged from immediately after apply.

Fallback acceptance is separate:

- The live ConfigMap must contain all 9 expected fallback rules.
- A real fallback-triggered request, or a working test mechanism, must show attempted fallback and final SpendLogs on the expected Wangsu/Wangsu7 target.
- If `mock_testing_fallbacks=true` does not propagate and the request completes on primary with `attempted_fallbacks=0`, mark fallback verification as `BLOCKED`, not `FAIL` and not `PASS`.

## Interpreting Fallback Mock Results

`MOCK_FALLBACK_NOT_TRIGGERED` means the test failed to force fallback. It does not prove fallback is broken.

Evidence pattern:

- Request body included `mock_testing_fallbacks=true`.
- HTTP response is 200.
- `x-litellm-attempted-fallbacks` is absent or `0`.
- SpendLogs show the expected ChatGPT acct primary.
- Reason text says request-level mock fallback was not propagated.

Correct conclusion:

- Primary route: still verified if normal-path evidence is ChatGPT acct.
- Fallback config: verify separately from live ConfigMap.
- Fallback execution: `BLOCKED` until a real failure or a working mock path proves the final Wangsu/Wangsu7 target.

## Restore

Use restore when apply created a regression or the user asks to roll back:

```bash
python3 scripts/litellm-pro-gpt-products-converge.py \
  --restore /root/litellm-product-manifests/backups/<backup-dir> \
  --reports-dir reports
```

After restore, run rollout status and strict regression again. Restore must bring back ConfigMap, cursor key rows, and codex compatibility model rows from the backup.

## Safety Rules

- Never include `sk-` keys, master keys, OAuth tokens, API keys, cookies, or account identifiers in skills, docs, diagrams, reports, commits, or chat output.
- Prefer `/key/update` through LiteLLM admin API for cursor key updates. If API cannot clear aliases correctly, stop and report; do not silently switch to SQL writes.
- Do not put Wangsu/OpenRouter/internal models into cursor key allowlists.
- Do not add product-model key-level aliases to Wangsu/OpenRouter. Router fallback is the only path to fallback targets.
- Do not infer causality. For "X caused Y", use: hypothesis, falsification condition, data path.
- Do not rely only on ConfigMap because pro uses `store_model_in_db=true`; check `/model/info`, ConfigMap, and SpendLogs.

## Known Baseline From 2026-06-05 Apply

Latest known state after the scripted apply:

- Router/key convergence apply report: `reports/litellm-pro-gpt-products-converge-20260605T155142Z.md`.
- Strict regression report: `reports/litellm-pro-gpt-products-20260605T155541Z.md`.
- Visibility, cursor key audit, direct internal denial, normal ChatGPT acct path, and cleanup were `PASS`.
- Fallback mock verification was `BLOCKED` because `mock_testing_fallbacks=true` did not trigger fallback on current pro `/v1/responses`; all mock requests completed on ChatGPT primary with `attempted_fallbacks=0`.

When answering status questions, cite the newest report by timestamp and avoid using older reports as current evidence.

## Completion Response

Summarize in this order:

1. Whether pro config/key convergence was applied or only dry-run.
2. Report paths and remote backup directory if apply/restore ran.
3. Visibility/cursor audit/direct denial status.
4. Normal path status and representative SpendLogs route.
5. Fallback status, explicitly distinguishing configured fallback from verified fallback execution.
6. Any unresolved `BLOCKED` item and the exact evidence needed to unblock it.
