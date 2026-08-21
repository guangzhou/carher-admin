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
      - "scripts/litellm-wa-flush-affinity.py"
      - "scripts/litellm-wa-probe.py"
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

## Pool Weight Rebalance & Traffic Migration

用户要"把某几个号权重调高/其余调低"、或反映"改了权重但流量看着不对"时用本节。

### 权重怎么设（source of truth = state.json desired_weight）

`quota-rebalance.py`（JSZX-AI-03，cron `*/5`）的 weight-align 逻辑：每 tick 对 online（非 paused）
号，把 `state.json` 里各号的 `desired_weight`（dw）PATCH 进 live router entry
（`/model/{mid}/update` → `litellm_params.weight`）。主循环**纯搬运** dw，不重算。

- 改权重 = 改 `state.json` 的 `desired_weight`（原子写：tmp+rename；先备份 `state.json.bak.<ts>`）。
  `dw=100` → 高权；`dw=1` 或 `dw=None` → LiteLLM router 默认 = weight 1。
- **paused / manual_offline / scale0 的号被 weight-align 跳过** → 无论 dw 设多高，**0 流量**。
  先跑 `chatgpt-acct-quota.sh` 确认目标号都是 `take=✅ ONLINE`，死号(如手机墙/token_dead)要么剔除、
  要么先修活，别指望给死号设 weight=100 能分到量。
- 校验 live 权重：`GET /model/info`，逐号读 `litellm_params.weight`，只看
  `model_info.id.startswith("chatgpt-")`（`sa/acct-*` 是独立 standby 家族，PATCH 会 404，别混入）。

### 关键陷阱：改完 weight 流量不动 = WA session-affinity 架空了 weight

198 pro 池挂了 WA hook（live config: `callbacks: weighted_affinity.proxy_handler_instance`
+ `optional_pre_call_checks: [encrypted_content_affinity]` + `deployment_affinity_ttl_seconds: 120`）。
它在 router 选路**之前**按 `prompt_cache_key`/会话指纹把请求**钉在"上次服务它的 deployment"上**
（Redis `weighted_affinity:v2:*`，120s 滑动 TTL，会话持续活跃就一直粘）。

**所以 weight（simple-shuffle）只决定新会话/冷启会话落哪**；存量长会话（Codex 多轮 agent）
继续压在改权重之前的老号上 → 你看到的"流量不对"。2026-08-21 实测：81..91 设 weight=100 后，
weight=1 的老热号 acct-108 仍独吞 321 req/10min，高权 8 号成功份额仅 12.2%。

### 让 weight 立刻生效：flush WA 亲和 pin

```bash
POD=$(kubectl -n litellm-product get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
kubectl -n litellm-product cp scripts/litellm-wa-flush-affinity.py $POD:/tmp/wa-flush.py -c litellm
kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-flush.py            # ① DRY-RUN 看 pin 分布/偏斜
kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-flush.py --apply     # ② flush 全部 v2 pin
kubectl -n litellm-product exec $POD -c litellm -- rm -f /tmp/wa-flush.py               # 用完删
```

- **只删 `weighted_affinity:v2:*`（会话 pin）**；默认保留 `weighted_affinity:fail:v1:*`
  （避开坏号的保护键，180s 自动过期，删了会把量灌向正在 429 的号）；其它 Redis key
  （预算/spend/router 状态，dbsize 大头）**绝不碰**，永远不 FLUSHALL。
- flush 后存量会话失去 pin → 下个请求按 weight 重路由并重建 pin。实测高权份额 12.2% → 80.1%
  （3min），继续向理论份额收敛（`w/Σw`，如 8 号各 100、其余各 1 → 每高权号 ~12%）。
- `--model-group <g>` 只 flush 某组；`--include-fail-marks` 少用（确认坏号已修好才连保护键一起清）。

### 验收（两条独立证据）

1. **路由层**：`wa-flush.py` 的 dry-run（重建后）top pinned deployment 应变成高权号。
2. **数据层**：数 acct pod 真实请求（脚本 docstring 附带 one-liner，或）：
   `kubectl -n litellm-product logs <acct-pod> --since=3m | grep -c 'POST /responses.*200 OK'`
   高权号成功数应显著超过 weight-1 号；死号(paused)恒为 0。
   注意区分：某高权号成功=0 但**全是同一 model_group 429**（如 `chatgpt-gpt-5.6-luna`），
   是**该 model group 上游限流**（独立问题），不是号死——flush 后它拿到其它健康 group 的量即恢复。

## Common Mistakes

- Do not answer current upstream quota from 188 docker state. 187/188 are legacy or rollback paths for ChatGPT acct serving.
- Do not include Aliyun in a "198 only" upstream quota answer.
- Do not treat all `OFFLINE` as quota exhaustion. `manual_offline` means no automatic resume.
- Do not assume `paused` accounts are stale; they may intentionally skip probes until reset.
- Do not use SpendLogs to infer upstream quota. SpendLogs are downstream consumption after LiteLLM routing and fallback.
- Do not hide command output in the final response. Summarize the actual acct groups and notable rows.
- Do not conclude "weight didn't take effect" from traffic alone — WA session-affinity pins existing sessions regardless of weight. Check the router weight via `/model/info` first; if weights are correct but traffic is skewed, flush the affinity pins (see Pool Weight Rebalance).
- Do not `FLUSHALL` or delete non-`weighted_affinity:v2:*` Redis keys when migrating traffic. Budgets/spend/router state share the same Redis DB.
- Do not set a high weight on a `paused`/`OFFLINE`/scaled-0 acct expecting traffic; weight-align skips them and they receive zero.
