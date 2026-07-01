---
name: chatgpt-web-to-codex-zerokey
description: Use when bridging a ChatGPT Pro web-chat quota into an OpenAI-compatible API on server 188 (10.68.13.188:8123+) via zerokey — i.e. Codex/VS Code/any OpenAI client should keep working when web chat quota is available on a separate pool from Codex-native backend. Covers deploy/containerize, multi-account onboarding (add-account.sh), 198 registration (litellm-register-zerokey.py), raw vs vscode ToolCompiler, Codex wire_api=responses via LiteLLM, session capture/refresh, known traps, and planned Agent bridge (docs/zerokey-codex-agent-bridge-design.md). For mail.com OTP see chatgpt-login-session. Index: docs/zerokey-codex-artifacts.md.
---

# ChatGPT Web → Codex/OpenAI API bridge (zerokey on 188)

## Overview

Bridge a ChatGPT **web-chat** quota to an OpenAI-compatible API so Codex / VS Code
/ any OpenAI client keeps working when the Codex 5h/7d quota is spent but web chat
is fine. Standalone Docker stack on `188` / `10.68.13.188:8123`. **Does not touch**
K8s, carher-admin, operator, or any bot pipeline — it is a local tool stack.

Mechanism: `zerokey` (Node) replays a captured browser `POST /backend-api/f/conversation`
(headers incl. `openai-sentinel-proof-token` + `cookie`/`cf_clearance` + `authorization`,
plus body) against chatgpt.com's web backend, exposing `/v1/chat/completions`.

Repo bundle: `scripts/chatgpt-onboard/zerokey-codex/` (install.sh, zerokey-patch/,
capture/, ops/). Full design + runbook: `docs/chatgpt-web-to-codex-zerokey.md`.
**Artifact index:** `docs/zerokey-codex-artifacts.md`. On-host runbook:
`~/zerokey-codex/ops/README.md`.

## Hard rules

- **Capture MUST run on 188.** `cf_clearance` is bound to the egress IP; capturing
  elsewhere yields a session chatgpt.com rejects. zerokey also runs on 188.
- **Never break the VS Code path.** Model/raw changes live in new `routes/raw.js` +
  a top-of-handler branch; `Bearer vscode` still goes through the original ToolCompiler.
- **Don't kill the serving session on a failed refresh.** `refresh.sh` validates the
  new capture and only then atomically swaps `state/users.json` + restarts; on failure
  it keeps the old session and alerts.
- **Don't burn OTP codes blindly.** Each capture run emails a fresh code; coordinate
  in real time and write it to `state/out/otp.txt`. Old codes expire (~10 min).

## Two modes (selected by Authorization header)

| Header | Path | Behavior |
|---|---|---|
| `Bearer vscode` (default) | ToolCompiler | VS Code tool grammar injected; stateful. Unchanged upstream. |
| `Bearer raw` / `codex` / `openai` / `plain` | raw passthrough | No tool injection; stateless full-history per call; stream + non-stream. |

## Models

Per-request `model` reaches the web backend on both paths. `GET /v1/models` lists
the real slugs (`gpt-5-5-pro/thinking/instant`, `gpt-5-4-pro`, `o3`, `o3-pro`,
`gpt-4-5`, `research`, …) + aliases (`gpt-4o→gpt-5-mini`, etc.). Default via
`ZK_DEFAULT_MODEL` (compose: `gpt-5-5`). Slugs come from `GET /backend-api/models`.

## Deploy / manage

```bash
# first install (clones upstream, overlays patches, builds dir layout)
scripts/chatgpt-onboard/zerokey-codex/install.sh        # → ~/zerokey-codex
# put secrets/{mail_pw,chatgpt_pw}.txt, build capture + server images
(cd ~/zerokey-codex/capture && docker build -t zerokey-capture:latest .)
(cd ~/zerokey-codex/zerokey && docker compose build)
# capture a session (see below) → then:
(cd ~/zerokey-codex/zerokey && docker compose up -d)    # restart:always, :8123
curl -s localhost:8123/v1/models | head
```

Codex client (`~/.codex/config.toml`) — **2026+ requires `wire_api = "responses"`**;
zerokey only has `/v1/chat/completions`, so **route through 198 LiteLLM**, not direct 188:

```toml
model = "zerokey-gpt-5.5"   # or zerokey-timothy-gpt-5.5
model_provider = "litellm_pro"

[model_providers.litellm_pro]
base_url = "https://cc.auto-link.com.cn/pro/v1"   # or http://10.68.13.198:30402/v1
env_key = "LITELLM_API_KEY"
wire_api = "responses"
requires_openai_auth = false
```

Live cm must set `use_chat_completions_api: true` on each zerokey model (Codex
`/v1/responses` → LiteLLM → zerokey chat/completions). Register/repair:
`ops/litellm-register-zerokey.py --apply --sync-manifest` on 198.

Direct 188 + `wire_api = "chat"` only works on **legacy** Codex builds.

## Wire into 198 LiteLLM Pro (litellm-product)

zerokey is also exposed as upstream models in the 198 LiteLLM Pro proxy (K3s, ns
`litellm-product`, NodePort 30402, `jms ssh AIYJY-litellm`). 198 reaches 188 over
the internal network directly. Model entries in ConfigMap `litellm-config`
`model_list` (mirror the openrouter-on-188 pattern), inserted before `router_settings:`:

```yaml
- model_name: zerokey-gpt-5.5      # + -5.5-thinking / -5.5-pro / zerokey-o3
  litellm_params:
    model: openai/gpt-5-5          # web slug; openai/ provider
    api_base: http://10.68.13.188:8123/v1
    api_key: raw                   # literal -> Bearer raw -> raw passthrough
    use_chat_completions_api: true # Codex wire_api=responses bridge
    input_cost_per_token: 0
    output_cost_per_token: 0
```

- **cm is JSON-in-JSON**: never `kubectl apply` the stale manifest. Prefer
  `ops/litellm-register-zerokey.py --apply --sync-manifest` (idempotent, backs up cm).
  Or manual: `kubectl get cm …` → string-splice → `kubectl replace` →
  `kubectl rollout restart deployment/litellm-proxy` (4 replicas RollingUpdate).
- Verify Codex path via NodePort (master key from `litellm-secrets`):
  `curl -s -X POST -H "Authorization: Bearer $MK" localhost:30402/v1/responses \
    -d '{"model":"zerokey-gpt-5.5","input":"hi"}'`
  and `/v1/chat/completions` for non-Codex clients.
- per-user access needs the `zerokey-*` names in that key's `models` allowlist via
  `/key/update` (see `litellm-pro-ops`); master key works already.
- Capacity: all names on one port share that port's web session — personal/low
  concurrency only; high concurrency gets web-side rate-limited.

## Multi-account onboarding

Each account = own port + container + profile + `~/zerokey-codex-accounts/<id>/`.

```bash
cd ~/zerokey-codex/ops
./add-account.sh <zk_id> <email> '<mail_pw>' '<gpt_pw>' <port>   # e.g. acct50 ... 8144
```

- **Credential store (188 only):** `/Data/chatgpt-auth/acct-N/.creds` holds
  `email=`, `mail_pw=`, `chatgpt_pw=`, `mail_provider=`. **198 has NO creds** — all live
  on 188 (`jms ssh JSZX-AI-03`). Batch onboarding reads these, never hard-code secrets.
- **Only `mail.com` accounts auto-onboard.** `OTP_AUTO_ONLY=1` auto-reads the ChatGPT
  OTP from mail.com webmail (`OTP_AUTO_MAX`s window). qq/hotmail/outlook have **no auto
  OTP reader** → manual OTP only. Filter candidates on `mail_provider=mailcom`.
- **acct↔port↔acct-N map:** `scripts/zerokey_acct_port_map.py` (live, has `chatgpt_acct`
  column). Pool members 8123-8136 = 14 accounts; next free port 8137+.

### Onboarding is flaky — verify each, don't blind-batch (diagnosis discipline)

- **Failure mode (seen 2026-06-25, acct-7):** mail.com login OK, but ChatGPT's **fresh
  OTP email never arrives** within `OTP_AUTO_MAX` (only a stale prior-day code in inbox).
  Capture then falls to the SSO path, lands on `accounts.google.com` sign-in, `composer
  not found`, exit 124.
  - **Hypothesis:** OpenAI **rate-limits OTP sends** after repeated login attempts on the
    same account (acct-7 had a prior-day attempt). Falsification: a never-attempted account
    gets its OTP promptly. (Untested across the batch — treat onboarding as best-effort.)
  - Longer `OTP_AUTO_MAX` does **not** fix a rate-limited/SSO account; also check the
    mail.com **Spam** folder (the auto-reader only scans Inbox).
- **Run a single onboard first** to confirm the flow works in the current env before a
  background batch; batch with continue-on-fail + per-account health check, then register
  only the healthy ports.

### 198 registration — model_id MUST be readable `acct-N`

- Per-account models: add `zerokey-<account>-*` with `api_base: http://10.68.13.188:<port>/v1`,
  then `litellm-register-zerokey.py --apply --sync-manifest`.
- **Load-balanced `zerokey-pool` group:** each member is one CM `model_list` block that
  differs only by `api_base` port. **Always set `model_info.id: acct-N`** (the account
  behind that port, per `zerokey_acct_port_map.py`). Without `model_info` LiteLLM auto-hashes
  the id (e.g. `1dd9af…`), which is unreadable in `x-litellm-model-id` **and in 429
  `cooldown_list`**. Edit the CM (add `model_info.id`) → `kubectl rollout restart
  deployment/litellm-proxy` (4 replicas, ~90s startup each; zero-downtime). Verified
  2026-06-25: 8123→acct-39, 8124→acct-36, 8125→acct-48, 8126→acct-45, 8127→acct-46,
  8128→acct-47, 8129→acct-40, 8130→acct-41, 8131→acct-42, 8132→acct-43, 8133→acct-44,
  8134→acct-37, 8135→acct-32, 8136→acct-34.

Login/OTP skill: `.codex/skills/chatgpt-login-session/SKILL.md`.

## codex (198 OAuth) + zerokey (188 web) coexist on the SAME account

The same ChatGPT account can run **both** the 198 chatgpt-acct codex pool (OAuth,
`/v1/responses`) **and** the 188 zerokey web-chat pool simultaneously — they consume **two
independent quota buckets** (Codex 5h/7d vs web-chat hourly). This is the **intended
dual-bucket design**, not a conflict: onboard Codex-quota-exhausted accounts into zerokey to
harvest their still-fresh web bucket.

- **Verified 2026-06-25:** acct-36 had traffic on **both** sides in the same 48h window
  (codex `chatgpt-acct-36-gpt-5.5` 2167 calls + zerokey port 8124 50 calls).
- The `auth.json 互踢` rule applies only to **two OAuth holders** of the same account, NOT
  web-cookie + OAuth. So a `0/0` or `1/1` 198 codex deployment for a candidate is **not** a
  blocker for adding it to zerokey.

## Agent capabilities (current vs planned)

- **Shipped:** Codex `wire_api=responses` → 198 LiteLLM → zerokey **raw** → web chat
  quota. Good for chat, Q&A, code generation in prose.
- **Not shipped:** Full Codex Agent (`apply_patch`, `shell` loop). LiteLLM drops
  Responses-only tools; raw skips ToolCompiler. Plan: local
  `zerokey-codex-responses-bridge` + `Bearer vscode`/`codex` — see
  `docs/zerokey-codex-agent-bridge-design.md`.
- **Alternative (MCP):** [gpt2agent](https://github.com/robotlearning123/gpt2agent)
  exposes web `agent-mode` as MCP tools alongside Codex (different architecture).

## Scripts quick reference

| Script | Role |
|--------|------|
| `install.sh` | First deploy on 188 |
| `ops/add-account.sh` | New account + port + container |
| `ops/refresh.sh` | Cron refresh (per-account copy under accounts dir) |
| `ops/capture-manual.sh` | OTP re-seed |
| `ops/litellm-register-zerokey.py` | 198 cm + manifest (8 models, both accounts) |
| `capture/zerokey-web-capture.py` | Login + OTP + capture |

## Capture / refresh

- Auto: `ops/refresh.sh` reuses `state/profile` (no OTP while login alive), validates,
  swaps, restarts, alerts on failure. Cron every 6h with `ZK_ALERT_WEBHOOK`.
- Manual (OTP): `ops/capture-manual.sh`, then `echo <code> > ~/zerokey-codex/state/out/otp.txt`.
  The script runs ~90s of flaky mail.com auto-fetch first, then prints
  `>>> OTP_WAIT_FILE` and reads the file (it clears the file on entry — write after the prompt).

## Known traps (diagnosis discipline: hypothesis → falsification → data)

1. **capture container hangs, python never starts.** Data: process tree is only
   `/bin/sh /usr/bin/xvfb-run` (PID 1) + `Xvfb`, no python, empty logs. Cause:
   xvfb-run as PID 1 hangs before exec'ing python; `bash -lc "single cmd"` also
   exec-optimizes to PID 1. Fix: `ENTRYPOINT ["bash","-lc","xvfb-run -a python /capture/zerokey-web-capture.py; exit $?"]`
   (trailing `; exit $?` keeps bash PID 1, xvfb-run a child).

2. **OTP succeeds but chatgpt.com lands anonymous (RESOLVED).** Data (initial): OTP
   accepted at `auth.openai.com/email-verification`, but chatgpt.com showed
   `login_btn=2, composer=1` and the profile probed `ANON` — the session cookie lands
   *late* after the OAuth callback, and the script judged anonymous before it settled.
   Fix: after the post-OTP settle, retry-reload chatgpt.com up to 4× (with clear_cf +
   sleep) while `not is_logged_in`; this lets the cookie land. After fix:
   `post-OTP login state=True` → captured 24 headers → profile probes `LOGGED_IN` →
   unattended refresh works (`reusing persisted session`, no OTP, ~26s). Cron every 6h.
   If the profile EVER fully expires, refresh writes `state/REFRESH_STALE` (+ alert) →
   re-seed via `ops/capture-manual.sh` + one OTP.

3. **mail.com auto-OTP unreliable** (skeleton stall). File fallback `state/out/otp.txt` exists.

4. **cf_clearance/sentinel are short-lived + IP-bound** → must capture & serve on 188; refresh periodically.

5. **Non-stream via LiteLLM → "Empty or invalid response from LLM endpoint" (RESOLVED).**
   Data: direct-to-188 with explicit `stream:false` returned proper `chat.completion`
   JSON (falsifies "raw can't do non-stream"). Cause: `routes/raw.js` defaulted
   `stream = true`, but the OpenAI spec treats an absent `stream` as non-stream, and
   LiteLLM's OpenAI SDK omits `stream` on non-stream calls → zerokey returned SSE →
   LiteLLM couldn't parse it. Fix: default `stream = false` in `routes/raw.js`
   (explicit `stream:true` still streams), then `docker compose up -d --build`.

## Per-account consumption / spend

zerokey deployments set `input/output_cost_per_token: 0` (web quota, not paid API), so
LiteLLM **`spend`=0 for every zerokey row** — $ tells you nothing. Real consumption =
**calls + tokens** (zerokey also doesn't report prompt tokens; `total_tokens` ≈ output).

The pool accounts (14 as of 2026-06-25: ports 8123-8136, growing) share one model_name
`zerokey-pool`, distinguished only by `api_base` (port) — and now by `model_info.id=acct-N`.
Per-account split is on the **198 litellm-product DB** (`LiteLLM_SpendLogs`, group by
`api_base`). `/global/spend/models` only gives the pool aggregate. There is **no per-account
quota probe** for zerokey (unlike chatgpt-acct's `state.json`); a saturated account shows up
as its port's calls going flat while siblings take over (429 → router removes / cooldown,
now shown as `acct-N` in the `cooldown_list`).

```bash
python3 scripts/zerokey-account-usage.py [--hours N] [--json]   # per-port calls/tokens, with name map
```

For Aliyun her → zerokey-pool routing/rollout/capacity see skill
`carher-aliyun-her-zerokey-rollout`.

## Verify

- `curl localhost:8123/health` → healthy.
- raw: `curl -s localhost:8123/v1/chat/completions -H 'Authorization: Bearer raw' -d '{"model":"gpt-5-mini","stream":false,"messages":[{"role":"user","content":"2+2?"}]}'`.
- vscode unchanged: `Bearer vscode` still returns with injected grammar.
- 198 LiteLLM Pro: `zerokey-gpt-5.5` listed in `/v1/models` and both `stream:true`
  and `stream:false` return content through NodePort 30402.
