---
name: carher-aliyun-her-zerokey-rollout
description: Use when routing Aliyun CarHer `her` instances' GPT traffic to the 188 zerokey-pool (ChatGPT web-quota pool) through the Aliyun LiteLLM proxy — i.e. "让阿里云的 her 用上 zerokey-pool / 灰度 her 到 zerokey / 把 her 默认切 gpt 走 zerokey / 批量接入 zerokey 正式环境 / zerokey 容量看板". Covers the Direction-3 proxy chain (Aliyun litellm-proxy → 198 public ingress cc.auto-link.com.cn/pro → 198 zerokey-pool → 188 containers), per-key alias rollout, default-model switch, capacity monitoring, and the hard gotchas (prod≠canary DB, Cloudflare geo-block, per-key vs global alias, ClusterIP not svc-DNS). Scripts: scripts/prod-aliyun-her-zerokey.py, scripts/zerokey-prod-monitor.py. Background: docs/zerokey-fleet-pool-plan.md (v2.12/v2.13). Related: chatgpt-pool-aliyun-canary, litellm-key-provider-swap, chatgpt-web-to-codex-zerokey.
---

# Aliyun her → 188 zerokey-pool rollout

## What this is

Route Aliyun `her` (CarHer bot) GPT traffic onto the **188 zerokey-pool** (ChatGPT
web-quota → OpenAI API translation pool, **14 accounts on `10.68.13.188:8123–8136`** as of
2026-06-25, growing; onboarding adds 8137+).
zerokey **cannot run on Aliyun** (Cloudflare geo-blocks Aliyun IPs on chatgpt.com,
HTTP 403 `cf-mitigated: challenge`), so traffic is **proxy-chained** back to 188.

### The chain (Direction 3 — zero IT, proven 200)

```
Aliyun her
  → Aliyun litellm-proxy (carher ns)            # per-her vkey
    → model zerokey-pool, api_base https://cc.auto-link.com.cn/pro/v1   # 198 public ingress
      → 198 litellm-product zerokey-pool         # 198 self-heal fallback lives here
        → 188 zerokey containers :8123–8133
```

`spec.model=gpt` →(operator `config_gen.go`)→ litellm model `chatgpt-gpt-5.5` →
**per-key alias** `chatgpt-gpt-5.5 → zerokey-pool` (only that key) → the chain above.

## Hard rules / gotchas (read before touching anything)

1. **prod ≠ canary DB.** Aliyun `litellm-proxy` (secret `litellm-secrets`) and
   `litellm-proxy-canary` (secret `litellm-secrets-canary`) **do NOT share the key
   DB**. A `/key/update` done with the canary master key is invisible to prod.
   → To make a her effective **on prod**, set its alias with the **prod** master key
   (`litellm-secrets`). Verify with prod master key, not canary.
2. **Per-key alias, never global.** Set `aliases:{chatgpt-gpt-5.5→zerokey-pool}` on the
   her's own vkey. **Do NOT** add a global `router_settings.model_group_alias` — that
   reroutes every key's `chatgpt-gpt-5.5` (existing Aliyun chatgpt-acct pool) and breaks
   other users.
3. **Per-key fallback is unreliable.** Resilience comes from a **global**
   `router_settings.fallbacks: zerokey-pool → [chatgpt-gpt-5.5, wangsu-gpt-5.5]`. It is
   inert for keys that never call zerokey-pool, so it's safe for everyone else.
4. **198-side self-heal needs key scope.** The 198 zerokey-pool group has its own fallback
   chain; the 198 **link vkey** must be scoped to include every model in that chain or the
   self-heal 403s. **Updated 2026-06: the 198 chain is now `zerokey-pool → [deepseek-v4-pro]`**
   (chatgpt-gpt-5.5 + wangsu removed per user), so scope the link vkey to
   `[zerokey-pool, deepseek-v4-pro]`. See gotcha #9 for the single-deployment hard-fail risk
   this introduced. (Older `[chatgpt-gpt-5.5, wangsu-gpt-5.5]` wording below predates this.)
5. **STORE_MODEL_IN_DB=True** on both prod & canary → register zerokey-pool with
   `/model/new` (hot, no CM edit, no restart). A rolling restart still reloads DB models.
6. **k8s-work-226 is the control host** (has carher `kubectl`) but has **no in-cluster
   svc DNS** → reach LiteLLM via ClusterIP, not `*.svc`. Prod `litellm-proxy` =
   `192.168.35.175:4000`, canary = `192.168.83.72:4000` (re-check, IPs can change).
   `laoyang` etc. have **no kubectl**.
7. **Zero-downtime.** litellm-proxy has 2 replicas; `kubectl rollout restart` is
   zero-downtime (~350s incl. LiteLLM ~90s startup). Never `kubectl delete pod`.
8. **Capacity.** The 14 zerokey accounts are a "bounded overflow buffer, not primary";
   each has a **per-account hourly web-chat cap** (ChatGPT "limit of messages per hour").
   With ~51 hers defaulting to zerokey, peak hours saturate the pool → 429 across members.
   Mitigate by **growing the pool** (onboard more accounts) and watching fallback $/min.
9. **Single-fallback hard-fail (the 429 the user hit, 2026-06-25).** 198 zerokey-pool
   fallback is `[deepseek-v4-pro]` (chatgpt-gpt-5.5 + wangsu were removed per user — "不要
   再 fallback wangsu"). **`deepseek-v4-pro` is a single `openrouter/` deployment**, so when
   *all* zerokey members 429 AND deepseek hits LiteLLM cooldown, the group is empty →
   `RateLimitError … No deployments available for selected model. Passed model=deepseek-v4-pro`
   with `cooldown_list=[openrouter/deepseek-v4-pro, acct-36, acct-48, …]` → **hard error to
   the her/user** (no further tier). Resilience options (no wangsu):
   - give `deepseek-v4-pro` **≥2 deployments** (2nd openrouter key / official deepseek) so
     one cooldown can't empty the group;
   - relax its cooldown (`allowed_fails` ↑ / `cooldown_time` ↓) and raise rpm/tpm;
   - and/or grow zerokey capacity to lower the 429 rate.
   The `acct-N` cooldown_list (gotcha above) is what makes this diagnosable at a glance.

## Prereqs

- JMS access: `./scripts/jms ssh k8s-work-226` (Aliyun control), `AIYJY-litellm` (198).
- A **198 link vkey** scoped to `[zerokey-pool, chatgpt-gpt-5.5, wangsu-gpt-5.5]`.
  Mint on 198: `POST /key/generate` with `models=[…]`, `key_alias=canary-aliyun-zerokey-link-*`.
  Pass it as the `LINK_KEY` env to the rollout script. **Never** write the key value into
  repo/docs/skill.
- Confirm 198 already has global fallback `zerokey-pool→[chatgpt-gpt-5.5, wangsu-gpt-5.5]`
  (added by `scripts/prod-patch-key-primary-zerokey.py`); if missing, add there (198 CM).

## Rollout workflow (prod, batch)

Script: `scripts/prod-aliyun-her-zerokey.py` — idempotent, reversible, secrets via env.
Run on `k8s-work-226`. `TARGETS` = comma list of her numeric ids; default = the 51 already
live. Order matters: A/C are additive (default still sonnet, no traffic); do B before D so
the fallback net is live at cutover.

```
Task Progress:
- [ ] A register : hot-add zerokey-pool deployment (no restart)
- [ ] B fallback : add global fallback + zero-downtime rolling restart
- [ ] C keys     : per-key alias on every target her's vkey
- [ ] D switch   : flip spec.model=gpt (cutover)
- [ ] verify     : target → zerokey; a non-target → unaffected
```

```bash
# scp once
./scripts/jms scp scripts/prod-aliyun-her-zerokey.py k8s-work-226:/tmp/pz.py
T="2,3,5,…,269"   # target her ids

# A) register zerokey-pool on prod litellm-proxy (hot)
./scripts/jms ssh k8s-work-226 "TARGETS=$T LINK_KEY=sk-… python3 /tmp/pz.py register --apply"
# B) global fallback + rolling restart (~350s, zero-downtime)
./scripts/jms ssh k8s-work-226 "python3 /tmp/pz.py fallback --apply"
# C) per-key alias on all targets (PROD master key — see gotcha #1)
./scripts/jms ssh k8s-work-226 "TARGETS=$T python3 /tmp/pz.py keys --apply"
# pre-cutover sanity: target her's key sends chatgpt-gpt-5.5 → must land zerokey
./scripts/jms ssh k8s-work-226 "TARGETS=$T python3 /tmp/pz.py verify"   # expect x-litellm-model-id=zerokey-pool-198-link
# D) cutover: default model → gpt
./scripts/jms ssh k8s-work-226 "TARGETS=$T python3 /tmp/pz.py switch --apply"
```

Always run each subcommand **without `--apply` first** (dry-run) to see the diff.

### Single her / canary variant

- Single her, keep default (manual `/model gpt` test): do A+B+C, skip D; user switches in-bot.
- Canary (`litellm-proxy-canary`): same steps but canary master key + canary ClusterIP, and
  set `her.spec.litellmUrl=http://litellm-proxy-canary.carher.svc:4000`. Remember gotcha #1
  when later promoting to prod (must re-alias with prod master key).

## Verify

- Target: `pz.py verify` → `x-litellm-model-id=zerokey-pool-198-link`, text `pong`.
- Target live config: `cm carher-<N>-user-config` →
  `litellm.baseUrl=http://litellm-proxy.carher.svc:4000`, primary `litellm/chatgpt-gpt-5.5`.
- Non-target isolation: pick a her NOT in TARGETS → its key `aliases={}`, and
  `chatgpt-gpt-5.5` routes to `chatgpt-acct-*/chatgpt-gpt-5.5` (original Aliyun pool).

## Monitor capacity

Script: `scripts/zerokey-prod-monitor.py` (run on k8s-work-226; installed at
`~/zerokey-prod-monitor.py`). State file `~/.zerokey-prod-monitor.json` enables deltas.

```bash
./scripts/jms ssh k8s-work-226 'python3 ~/zerokey-prod-monitor.py'        # text; run twice for $/min
./scripts/jms ssh k8s-work-226 'python3 ~/zerokey-prod-monitor.py --json' # JSON → re-render canvas
```

Signals (`/global/spend/models`, cumulative — read **deltas** not absolutes):
- `openai/zerokey-pool` $/min rising = zerokey carrying load (good).
- **`openrouter/deepseek-v4-pro` calls/$ rising = zerokey saturation overflow** (429s on
  zerokey spilling to the fallback). This is now the current fallback (see gotcha #4/#9), not
  wangsu. If deepseek itself errors/cooldowns while zerokey is also 429 → users see the hard
  `No deployments available` error → escalate capacity / add deepseek redundancy.
- No explicit 429 count via REST; fallback calls/$ is the clean proxy. Cross-check 429s in
  198 `LiteLLM_SpendLogs` (now attributable per `acct-N` via the readable model_id).

Visual dashboard (static snapshot, paste `--json` to refresh):
`~/.cursor/projects/Users-Liuguoxian-codes-carher-admin/canvases/zerokey-prod-capacity.canvas.tsx`.

### Per-account (per-ChatGPT-account) consumption

`zerokey-prod-monitor.py` only sees the **pool aggregate** (Aliyun litellm-proxy records
one `zerokey-pool`, api_base = `cc.auto-link.com.cn/pro`). The 11 accounts can only be
split on the **198 litellm-product DB**, where the real deployments live and differ only
by `api_base` (port). Two key facts:

- **`spend`=0 for every zerokey row** (cost_per_token hardcoded 0 — web quota, not paid
  API). $ per account is meaningless. Real consumption = **calls + tokens**.
- zerokey does **not** report prompt tokens (`prompt_tokens`=0); `total_tokens` ≈ output.

Script: `scripts/zerokey-account-usage.py` (runs from local Mac via `jms ssh
AIYJY-litellm` → `kubectl exec litellm-db-0 -- psql`; uses **base64-over-ssh**, `jms scp`
is broken in this env). Port→account map is embedded.

```bash
python3 scripts/zerokey-account-usage.py             # last 24h, per-account calls/tokens
python3 scripts/zerokey-account-usage.py --hours 5   # match chatgpt-acct 5h window
python3 scripts/zerokey-account-usage.py --json
```

Underlying query (group by port = account):

```sql
SELECT regexp_replace(api_base,'.*:(\d+)/.*','\1') AS port,
       COUNT(*) calls, SUM(total_tokens) tokens
FROM "LiteLLM_SpendLogs"
WHERE api_base LIKE '%10.68.13.188:81%' AND "startTime" > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY calls DESC;
```

Port→account (live map: `scripts/zerokey_acct_port_map.py`): 8123 kristine · 8124 timothy ·
8125 zyq · 8126 owp · 8127 hgg · 8128 dvo · 8129 elise · 8130 herbert · 8131 olga ·
8132 tania · 8133 iheyv · 8134 acct37 · 8135 acct32 · 8136 acct34.

**Each `zerokey-pool` CM member now carries `model_info.id=acct-N`** (the underlying
chatgpt_acct), so `x-litellm-model-id` **and** 429 `cooldown_list` read as `acct-39` etc.
instead of an opaque hash (set 2026-06-25; mapping 8123→acct-39, 8124→acct-36, 8125→acct-48,
8126→acct-45, 8127→acct-46, 8128→acct-47, 8129→acct-40, 8130→acct-41, 8131→acct-42,
8132→acct-43, 8133→acct-44, 8134→acct-37, 8135→acct-32, 8136→acct-34). When adding members,
always set the new block's `model_info.id` to its `acct-N`, then rollout restart proxy.

**Per-account remaining quota** (web 5h/7d %) is NOT in LiteLLM — it lives on the ChatGPT
side. Unlike the chatgpt-acct pool (which has a quota-rebalance probe → `state.json`,
`chatgpt_acct_quota_view.py`), the zerokey pool has **no such probe**; the only LiteLLM
signal of an account hitting its limit is its port's calls going flat while sibling ports
take over (429 → router removes it).

## Rollback (idempotent, each step independent)

```bash
TARGETS=$T python3 pz.py switch   --rollback --apply   # her → sonnet (fastest stop-bleed)
TARGETS=$T python3 pz.py keys     --rollback --apply   # clear per-key aliases
python3 pz.py fallback --rollback --apply              # remove global fallback + rolling restart
python3 pz.py register --rollback --apply              # delete zerokey-pool deployment
```

## Current state (live)

- prod: **51 her** default gpt→zerokey-pool (50 in batch v2.13 + her-1000).
- canary: her-1000 left canary for prod; an orphan zerokey-pool + stale alias remain on
  canary (harmless).
- See `docs/zerokey-fleet-pool-plan.md` v2.12 (canary pilot) / v2.13 (prod 50 + her-1000).
