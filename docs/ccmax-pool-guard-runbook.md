# CCMax Pool Guard Runbook

This runbook covers the sidecar CCMax upstream quota guard deployed on
`cc-proxy`. It protects the Malaysia random pool by removing high-quota or
unhealthy accounts from local random selection before LiteLLM traffic reaches
them.

## Scope

Current sidecar deployment:

- Host: `cc-proxy`
- Guard state directory: `/Data/ccmax-pool-guard`
- Guarded random proxy directory: `/Data/claude-max-proxy-random-guarded`
- Guarded random proxy port: `127.0.0.1:3466`
- 198 sidecar tunnel: `10.68.13.198:3465 -> cc-proxy:127.0.0.1:3466`
- Existing production random proxy remains on `127.0.0.1:3462`
- Existing 198 tunnel remains pointed at the old random proxy on `10.68.13.198:3463`

The sidecar is intentionally not connected to 198 LiteLLM yet. It can be tested
without changing user traffic.

## Components

| Component | Path | Purpose |
|---|---|---|
| Pool guard script | `/Data/ccmax-pool-guard/ccmax-pool-guard.py` | Probe 5h/7d quota, render account state and active upstreams. |
| Guard config | `/Data/ccmax-pool-guard/config.json` | Per-account thresholds, proxy URL, RPM, concurrency limits. |
| State file | `/Data/ccmax-pool-guard/state.json` | Last known account state. Contains no OAuth token. |
| Active upstreams | `/Data/ccmax-pool-guard/active-upstreams.json` | Dynamic upstream list consumed by guarded random proxy. Contains no token. |
| Event log | `/Data/ccmax-pool-guard/events.jsonl` | State transitions for audit. |
| Guarded random proxy | `/Data/claude-max-proxy-random-guarded/proxy.py` | Reads active upstreams and enforces acct-level RPM/concurrency. |
| Guard timer | `ccmax-pool-guard.timer` | Runs quota guard every 5 minutes. |
| Guarded proxy service | `ccmax-proxy-random-guarded.service` | Sidecar proxy on `127.0.0.1:3466`. |
| 198 side tunnel | `ccmax-guarded-random-tunnel.service` | Non-production 198 tunnel on `10.68.13.198:3465`. |

Compatibility requirement: the guarded proxy `.env` must include the existing
production random proxy API key from `/Data/claude-max-proxy-random/.env`.
That is what makes the final production switch tunnel-only; LiteLLM model rows
can keep their existing encrypted API key.

## Thresholds

Default policy:

- Drain acct when `5h >= 70%`.
- Fast drain acct when `5h >= 75%`.
- Drain acct when `7d >= 90%`.
- Fast drain acct when `7d >= 95%`.
- Recover only when `5h < 40%`, `7d < 80%`, cooldown has passed, and health is stable.
- `401` or `403` becomes `HARD_DOWN`; do not auto-recover without manual token/account review.
- `429` becomes `DRAINED` until cooldown or reset.

## Check Current Status

```bash
scripts/jms ssh cc-proxy 'bash -s' <<'REMOTE'
set -euo pipefail
systemctl is-active ccmax-pool-guard.timer ccmax-proxy-random-guarded.service
curl -sS http://127.0.0.1:3466/health
python3 - <<'PY'
import json
for path in ["/Data/ccmax-pool-guard/state.json",
             "/Data/ccmax-pool-guard/active-upstreams.json"]:
    print("FILE", path)
    d=json.load(open(path))
    if "accounts" in d:
        for acct, st in d["accounts"].items():
            print(acct, {k: st.get(k) for k in [
                "state","status","h5","d7","drained_reason","cooldown_until","last_error"
            ]})
    else:
        print([u.get("acct") for u in d.get("upstreams", [])])
PY
REMOTE
```

## Run Guard Once

```bash
scripts/jms ssh cc-proxy 'systemctl start ccmax-pool-guard.service && tail -n 20 /Data/ccmax-pool-guard/guard.log'
```

Use dry-run with a temporary config when testing threshold behavior. Do not lower
real thresholds unless you intend to drain the active pool.

## Validate Guarded Proxy

Run from `cc-proxy` without printing the guarded proxy API key:

```bash
scripts/jms ssh cc-proxy 'bash -s' <<'REMOTE'
set -euo pipefail
set -a
. /Data/claude-max-proxy-random-guarded/.env
set +a
curl -sS -X POST http://127.0.0.1:3466/v1/messages \
  -H "x-api-key: $API_KEYS" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5","max_tokens":8,"messages":[{"role":"user","content":"reply OK"}]}' \
  -w '\nHTTP=%{http_code}\n'
REMOTE
```

## Empty Pool Behavior

When `active-upstreams.json` has no upstreams, guarded random returns:

```json
{"error":{"type":"ccmax_pool_empty"}}
```

This must be treated as fallback-able by LiteLLM before switching production
traffic to the guarded port.

## Switch 198 To Guarded Port

Do not switch until sidecar checks pass. The intended production switch is one
small tunnel-only change:

1. Create a new 198 tunnel service to `cc-proxy:127.0.0.1:3466`, or update the
   existing `ccmax-random-tunnel.service` to forward to remote port `3466`.
2. Keep local listen port `10.68.13.198:3463` unchanged so LiteLLM config does
   not change.
3. Restart only the tunnel service.
4. Verify `curl http://10.68.13.198:3463/health` shows
   `mode=guarded-random-forward`.
5. Send minimal Haiku/Sonnet/Opus requests through the existing LiteLLM key.
6. Check SpendLogs.

The sidecar tunnel already exists for pre-switch validation:

```bash
scripts/jms ssh AIYJY-litellm \
  'curl -sS http://10.68.13.198:3465/health &&
   systemctl is-active ccmax-guarded-random-tunnel.service'
```

This avoids changing LiteLLM ConfigMap, DB model entries, or user key aliases.

## Rollback

Rollback is tunnel-only:

1. Restore `ccmax-random-tunnel.service` to remote port `3462`.
2. Restart `ccmax-random-tunnel.service`.
3. Verify `curl http://10.68.13.198:3463/health` shows `mode=random-forward`.
4. Leave the sidecar guard running or stop it with:

```bash
scripts/jms ssh cc-proxy 'systemctl disable --now ccmax-pool-guard.timer ccmax-proxy-random-guarded.service'
```

## Add A Future Account

1. Onboard the account and create a per-account proxy service as usual.
2. Add the acct entry to `/Data/ccmax-pool-guard/config.json`.
3. Keep `enabled=false` until Haiku/Sonnet/Opus pass through the per-account
   proxy.
4. Set `enabled=true`.
5. Start `ccmax-pool-guard.service`.
6. Confirm the acct appears in `active-upstreams.json`.

Never write OAuth tokens or proxy API keys into this repo, docs, or chat output.
