---
name: chatgpt-web-to-codex-zerokey
description: Use when bridging a ChatGPT Pro web-chat quota into an OpenAI-compatible API on server 188 (10.68.13.188:8123+) via zerokey — i.e. Codex/VS Code/any OpenAI client should keep working after the Codex 5h/7d quota is exhausted but web chat still works. Covers deploy/containerize, multi-account onboarding (add-account.sh), raw-passthrough vs VS Code mode, per-request model selection, capturing/refreshing the web session, wiring zerokey models into the 198 LiteLLM Pro proxy (litellm-product), and the known traps (xvfb-run PID1 hang, anonymous-after-OTP, raw.js stream-default vs OpenAI spec). For mail.com OTP automation see chatgpt-login-session skill.
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
On-host runbook: `~/zerokey-codex/ops/README.md`.

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
./add-account.sh timothy timothy_mossey871@mail.com '<mail_pw>' '<gpt_pw>' 8124
```

Uses `OTP_AUTO_ONLY=1` + enhanced mail.com OTP in `zerokey-web-capture.py`.
Login/OTP skill: `.codex/skills/chatgpt-login-session/SKILL.md`.

198 LiteLLM: add `zerokey-<account>-*` models with `api_base: http://10.68.13.188:<port>/v1`.

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

## Verify

- `curl localhost:8123/health` → healthy.
- raw: `curl -s localhost:8123/v1/chat/completions -H 'Authorization: Bearer raw' -d '{"model":"gpt-5-mini","stream":false,"messages":[{"role":"user","content":"2+2?"}]}'`.
- vscode unchanged: `Bearer vscode` still returns with injected grammar.
- 198 LiteLLM Pro: `zerokey-gpt-5.5` listed in `/v1/models` and both `stream:true`
  and `stream:false` return content through NodePort 30402.
