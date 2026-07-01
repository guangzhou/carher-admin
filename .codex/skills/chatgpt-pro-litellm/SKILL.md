---
name: chatgpt-pro-litellm
version: 1.0.0
description: >-
  ChatGPT Pro / Codex subscription account pool operations for CarHer 198
  LiteLLM: checking upstream quota, onboarding new acct-N into 198 K3s,
  reading downstream spend from 198 and Aliyun, and avoiding legacy 187/188
  probe paths. Use when the user mentions ChatGPT upstream quota, acct pool,
  198 acct, chatgpt-gpt-5.x, quota consumption, subscription expiry,
  sub_until, 阿里云 ChatGPT 消费, or 198 prod ChatGPT pool.
metadata:
  requires:
    bins: ["python3", "kubectl", "curl"]
    repo_files:
      - "scripts/chatgpt-acct-quota.sh"
      - "scripts/chatgpt_acct_quota_view.py"
      - "scripts/chatgpt-acct-spend.sh"
      - "scripts/quota-rebalance.py"
      - "scripts/oauth-fetch-acct.sh"
      - "scripts/onboard-chatgpt-acct.sh"
      - "k8s/chatgpt-acct-26-33.yaml"
---

# ChatGPT Pro LiteLLM 198 Pool

## Current Facts

- Production ChatGPT account pool is on `AIYJY-litellm` / `10.68.13.198`, K3s namespace `litellm-product`.
- Active acct pods expose `http://chatgpt-acct-N.litellm-product.svc.cluster.local:4000`.
- The quota scheduler still runs from `JSZX-AI-03`, but it reads auth from 198 K3s pods and writes state to `/home/cltx/.chatgpt-quota/state/state.json`.
- For upstream quota status, `state.json` is the default source of truth. Do not default to the legacy direct probe script.
- Aliyun ChatGPT accounts are a separate carher-bot spend source. Do not mix Aliyun accounts into the 198 upstream quota answer unless the user asks for downstream spend across both.

## Primary Commands

### Upstream Quota On 198 Only

Use this first whenever the user asks for ChatGPT upstream quota, 5h/7d usage,
online/offline status, or subscription expiry:

```bash
./scripts/chatgpt-acct-quota.sh           # complete list only
./scripts/chatgpt-acct-quota.sh --summary # complete list plus grouped counts
./scripts/chatgpt-acct-quota.sh --json    # raw state.json
```

Required workflow:

- Always run `./scripts/chatgpt-acct-quota.sh` for normal upstream quota checks.
- Paste the script's table output verbatim by default.
- Do not rebuild this table with ad hoc `jms`, `kubectl`, `python`, or heredoc commands.
- Use `--summary` only when the user explicitly asks for grouped counts.
- Use `--json` only for debugging or script changes.

The quota script prefers the repository wrapper `scripts/jms` over any `jms`
binary from `PATH`, so it does not accidentally use a stale local JumpServer
entrypoint.
The shell wrapper streams `scripts/chatgpt_acct_quota_view.py` to JSZX-AI-03;
keep rendering/email-resolution logic in that Python script instead of adding
large heredocs back into the shell wrapper.
It resolves account emails at runtime from readable `.creds` files or 198 pod
`auth.json` claims; do not hard-code real account emails in the skill.

This reads:

```text
JSZX-AI-03:/home/cltx/.chatgpt-quota/state/state.json
```

Expected table columns include:

```text
acct, email, take, status, tier, 5h%, 5h_reset, 7d%, 7d_reset, next_reset, restore, sub_until, sub_left, cause
```

Interpretation:

- `email`: account email decoded at runtime; `—` means the current readable
  sources do not expose it.
- `take=✅`: the router can send traffic to this acct now; this follows
  `paused/manual_offline` in `state.json`, not a local 95% quota threshold.
- `ONLINE`: not paused and not manually offline.
- `PAUSED`: quota pause, normally auto-recovers at reset.
- `OFFLINE`: `manual_offline`, usually OAuth/token/manual intervention; does not count as usable capacity.
- `5h%` / `5h_reset`: 5-hour quota usage and reset countdown.
- `7d%` / `7d_reset`: 7-day quota usage and reset countdown.
- `next_reset`: nearest future reset among the 5h and weekly quota windows.
- `sub_until` and `sub_left`: subscription active-until datetime and days remaining from quota state.

When reporting results, default to a single complete list using the script's
table output verbatim. Do not split into separate summaries unless the user
explicitly asks for a summary or grouped counts.

### Downstream Spend

Use this when the user asks which acct actually consumed traffic in LiteLLM,
or asks to include Aliyun:

```bash
./scripts/chatgpt-acct-spend.sh prod 24h
./scripts/chatgpt-acct-spend.sh aliyun 24h
./scripts/chatgpt-acct-spend.sh both 24h
```

The spend script reads `LiteLLM_SpendLogs`, not ChatGPT upstream quota. It
supports:

- `prod`: 198 `litellm-product` spend, team IDE/Codex traffic.
- `dev`: 198 `litellm-dev` spend.
- `aliyun`: ACK `carher` namespace spend for carher bot accounts.
- `both`: prod then Aliyun.

Aliyun query behavior:

- Auto-checks local kubectl access to namespace `carher`.
- If unavailable, tries `scripts/jms proxy` via configured assets.
- Use `ALIYUN_PROXY_ASSETS='k8s-work-227'` when `laoyang` is unavailable.
- Use `ALIYUN_AUTO_TUNNEL=0` to disable auto tunnel attempts.

### Legacy Raw Probe

`scripts/chatgpt-acct-usage.sh` used to probe multiple old sources directly.
It is no longer the default for upstream quota because it was built around
187/188 docker, Malaysia SSH, and Aliyun pod discovery.

Only use the legacy raw probe when you explicitly need fields not present in
quota state, such as `additional_rate_limits`, raw `limit_window_seconds`,
or raw `credits`:

```bash
./scripts/chatgpt-acct-usage.sh --legacy-raw --all --json
```

If a normal quota request accidentally reaches this script without
`--legacy-raw`, it should redirect to `chatgpt-acct-quota.sh`.

## Onboarding New 198 Acct

For a new subscription acct, use the current 198 path:

```bash
./scripts/oauth-fetch-acct.sh 27
./scripts/onboard-chatgpt-acct.sh 27 /tmp/auth-acct-27.json
```

Facts from the 2026-06-15 acct-26..33 run:

- OAuth device auth is fetched from 198 host with `Originator: codex_cli_rs`.
- K3s namespace is `litellm-product`.
- Image is from 198 local registry `127.0.0.1:5000`.
- Auth mount is `/chatgpt-auth/auth.json`.
- `gpt-5.3-codex` route must use upstream `openai/chatgpt-gpt-5.3-codex-spark`; keep client-facing `model_name=chatgpt-gpt-5.3-codex`.
- Do not register `chatgpt-gpt-5.4-pro`; ChatGPT subscription accounts reject it.

## Common Mistakes

- Do not answer current upstream quota from 188 docker state. 187/188 are legacy or rollback paths for ChatGPT acct serving.
- Do not include Aliyun in a "198 only" upstream quota answer.
- Do not treat all `OFFLINE` as quota exhaustion. `manual_offline` means no automatic resume.
- Do not assume `paused` accounts are stale; they may intentionally skip probes until reset.
- Do not use SpendLogs to infer upstream quota. SpendLogs are downstream consumption after LiteLLM routing and fallback.
- Do not hide command output in the final response. Summarize the actual acct groups and notable rows.
