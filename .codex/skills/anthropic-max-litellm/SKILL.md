---
name: anthropic-max-litellm
description: >-
  Use when building, expanding, migrating, or operating the CarHer Claude Max /
  CC Max account pool: onboarding Claude Pro/Max/Team accounts with sessionKey,
  Gmail, Outlook, mail.com, or 171mail flows; managing sk-ant-oat tokens under
  /Data/anthropic-auth; deploying or moving claude-max-proxy; wiring 198 prod
  LiteLLM claude-max-* models to the pool; checking upstream quota; planning
  a Malaysia egress pool on 47.250.197.56; or routing CC Max through the
  10.68.13.224 Astrill host. Also use when the user mentions
  Claude Code Max, CC Max, Anthropic OAuth, sk-ant-oat, sk-ant-sid02,
  add-cc-account, claude-max-proxy, or patch-litellm-claude-max.
---

# Anthropic Max / CC Max LiteLLM

## Scope

This skill covers the account-pool lifecycle, not ordinary Codex client setup.
For configuring Codex CLI or granting an existing virtual key access to the
already-running pool, use `codex-ccmax-litellm`.

Current production path:

```text
account/sessionKey
  -> scripts/anthropic-onboard/add-cc-account-*.sh
  -> /Data/anthropic-auth/acct-N/.env with ANTHROPIC_OAUTH_TOKEN
  -> claude-max-proxy v3 reads ACCT_TOKENS
  -> 198 prod LiteLLM claude-max-* entries
  -> api.anthropic.com /v1/messages?beta=true
```

Do not resurrect the old OAuth-direct LiteLLM provider path for Opus/Sonnet.
`sk-ant-oat` direct `/v1/messages` is model-allowlisted and normally only
Haiku works. Production uses `claude-max-proxy.py` v3 transparent forwarding.

ACK isolated test path, used before production routing:

```text
sessionKey Secret in ACK carher namespace
  -> isolated ccmax-onboard Job
  -> ccmax-acct-N-oauth + ccmax-proxy-acct-N-tokens Secrets
  -> isolated claude-max-proxy Deployment/ClusterIP Service
  -> manual in-cluster Haiku/Sonnet/Opus probes
  -> only then decide whether to patch 198 prod LiteLLM
```

## Safety Rules

- Never put real `sk-ant-oat*`, `sk-ant-sid*`, cookies, account emails, mailbox passwords, TOTP secrets, OAuth callback links, or virtual keys in chat, docs, skills, or commits.
- Store credentials only on the target runtime host under `/Data/anthropic-auth/acct-N/` with mode `600`.
- For ACK onboarding, store account fields only in Kubernetes Secrets. Do not put them in repo files, manifests, shell history summaries, or final responses.
- Do not open a public proxy or pool endpoint. Allow only explicit upstream callers such as 198, K8s NAT, or a controlled private network.
- Before any causal conclusion, use the repo Diagnosis Discipline: hypothesis, falsification condition, and data path.
- For 198 prod LiteLLM changes, work through JumpServer asset `AIYJY-litellm`; do not assume direct network access to `10.68.13.198` from cloud hosts.

## Reference Map

Load only what the task needs:

- `docs/claude-max-cli-proxy.md` - current v3 proxy architecture, identity injection, 198 LiteLLM wiring, fallback, operations.
- `docs/cc_max_litellm.md` - historical OAuth-direct research and quota economics; not the current serving path.
- `docs/198-cc-max-routing-comparison.md` - 198 prod key alias model and route verification.
- `scripts/anthropic-onboard/add-cc-account-sessionkey.sh` - preferred Max 20x finished-account onboarding.
- `scripts/anthropic-onboard/claude-max-proxy.py` - current transparent proxy server.
- `scripts/anthropic-onboard/docker-compose.claude-max-proxy.yml` - Docker deployment template.
- `scripts/anthropic-onboard/patch-litellm-claude-max.py` - add global `claude-max-*` model entries to 198 prod.
- `scripts/anthropic-onboard/claude-max-grant-key.sh` - grant/revoke per-key CC Max access.
- `scripts/anthropic-onboard/cc-max-upstream-status.sh` - quota and upstream account status for the active runtime host; defaults to 198 `AIYJY-litellm`.

## Onboarding Flow Selection

Never guess the flow from the seller name. Choose by credential shape and only
store seller secrets in remote `0600` files such as
`/Data/anthropic-auth/acct-N/.creds`; do not print them, commit them, or put
them in this skill.

| Credential shape | `.creds` keys | Primary flow | Fallback |
|---|---|---|
| SessionKey finished account, usually final field is `sk-ant-sid02-*` | `email`, optional `mail_pw`, `session_key`, `mail_provider=sessionkey` | `cc-oauth-sessionkey.py` / `add-cc-account-sessionkey.sh acct-N` | If the sessionKey is truncated, recopy and compare redacted length/prefix/suffix. |
| 171mail relay/query-token account | `email`, optional `mail_pw`, `relay_token`, `mail_provider=171mail` | `cc-oauth-171mail.py` / `add-cc-account-171mail.sh acct-N` | If remote relay fetch stalls but local browser works, use manual Claude `code#state` callback. |
| Gmail + password + TOTP | `email`, `gmail_pw`, `gmail_totp`, `mail_provider=gmail` | `cc-oauth-gmail-v3.py` | If Claude says Max/Pro is required, redeem gift/upgrade first; OAuth cannot make a free account Max. |
| Outlook/live mailbox | `email`, `mail_pw`, `mail_provider=outlook` | `cc-oauth-outlook.py` | Check passwordless prompts and Junk folder. |
| mail.com / 1and1 mailbox | `email`, `mail_pw`, `mail_provider=mailcom` | `cc-oauth-mailcom-v3.py` | Use as fallback when 171mail relay is unavailable but webmail password works. |

Decision rules:

- If a usable `session_key` exists, prefer it. It skips mailbox and relay
  automation.
- If the seller gives `b.171mail.com` and a "查询令牌", use 171mail even when
  a mailbox password is also present. The relay token can be old
  `sk-ant-sid02-*` style or newer hex style.
- If the login reaches Claude but says Max/Pro is required, stop onboarding and
  ask for gift/redeem/upgrade completion. Do not wire a non-Max account into
  LiteLLM.
- If browser automation reaches Claude authorization but cannot finish the
  mailbox/relay part, switch to manual callback instead of repeatedly
  re-debugging the same website.

Expected output:

```text
/Data/anthropic-auth/acct-N/.env
ANTHROPIC_OAUTH_TOKEN=sk-ant-oat...
```

For any `sk-ant-sid02-*` copied from a seller line, verify the copied value
before blaming the account. A single missing middle segment makes the cookie
look invalid and the OAuth page stays on "Log in". Compare length and redacted
prefix/suffix against the source; do not print the full value. In one ACK run,
the bad Secret was length 119 while the source was length 131, and the first
different character was in the middle of the token. After rewriting the Secret
with the full value, the same account completed OAuth and passed Haiku/Sonnet/
Opus through the proxy.

After onboarding, run:

```bash
./scripts/anthropic-onboard/cc-max-upstream-status.sh
```

Use `cc-plan-verify.py` when the user asks whether a seller account is real
Max/Team/Pro or whether multiple buyers share one session.

## ACK Isolated Onboarding Test Pool

Use this path when the user asks to add a CC Max account to Aliyun ACK or wants
to verify a new K8s node/egress before touching production 198 LiteLLM.

Environment facts from the acct-15 test run:

- Namespace: `carher`
- Preferred test node: `ap-southeast-1.172.16.16.122`
- Build host: `k8s-work-227`; build with `nerdctl`, not local Mac
- Registry: `cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher`
- Pull secrets: `acr-vpc-secret` and `acr-secret`
- ACK access may require `scripts/jms proxy laoyang 16443 172.16.1.163 6443`

Resource pattern:

```text
Secret/ccmax-acct-N-session
  keys: email, mail_pw, relay_token, session_key, mail_provider=sessionkey

ServiceAccount, Role, RoleBinding/ccmax-onboard-secret-writer
  allow get/update/patch only on:
    Secret/ccmax-acct-N-oauth
    Secret/ccmax-proxy-acct-N-tokens

Job/ccmax-acct-N-onboard
  reads session_key from Secret
  runs claude setup-token under tmux
  runs cc-oauth-sessionkey.py via patchright
  writes:
    ANTHROPIC_OAUTH_TOKEN
    ACCT_TOKENS=acct-N::<token>

Deployment/ccmax-proxy-acct-N-test
Service/ccmax-proxy-acct-N-test
  ClusterIP only, PORT=3456, env-only ACCT_TOKENS, /health probes
```

Implementation notes:

- Build the onboard image with `claude` CLI, Python, patchright/Playwright,
  `kubectl`, `tmux`, `cc-oauth-sessionkey.py`, and a wrapper that reads
  secrets from env. Do not pass sensitive values as command arguments.
- `claude setup-token` should run in tmux or another pseudo-terminal style
  session; a simple pipe/FIFO can fail to emit the OAuth URL.
- The wrapper may log account label, step status, redacted token prefix/suffix,
  token length, and probe status only.
- Pre-create output Secrets with placeholder values, then grant the Job only
  `get/update/patch` on those resource names. Kubernetes `create` cannot be
  safely constrained by `resourceNames`.
- Fix the proxy to ClusterIP only. Do not add Ingress, NodePort, Cloudflare, or
  public access during isolated validation.
- Do not create or keep a proxy Deployment with placeholder `ACCT_TOKENS`.
  Wait until the onboard Job has written a real token.
- Do not run `patch-litellm-claude-max.py` or `claude-max-grant-key.sh` during
  isolated validation.

Recommended checks:

```bash
kubectl -n carher wait --for=condition=complete job/ccmax-acct-N-onboard --timeout=900s
kubectl -n carher logs job/ccmax-acct-N-onboard | sed -E 's/sk-ant-[A-Za-z0-9_-]+/[REDACTED]/g'
kubectl -n carher get secret ccmax-acct-N-oauth ccmax-proxy-acct-N-tokens
kubectl -n carher rollout status deploy/ccmax-proxy-acct-N-test
kubectl -n carher get pod -l app=ccmax-proxy-acct-N-test -o wide
```

Probe from an existing in-cluster pod rather than pulling a public debug image:

```bash
kubectl -n carher exec <running-carher-pod> -c carher -- \
  curl -sS http://ccmax-proxy-acct-N-test:3456/health

kubectl -n carher exec <running-carher-pod> -c carher -- \
  curl -sS -X POST http://ccmax-proxy-acct-N-test:3456/v1/messages \
    -H 'content-type: application/json' \
    -H 'anthropic-version: 2023-06-01' \
    -d '{"model":"claude-haiku-4-5","max_tokens":10,"messages":[{"role":"user","content":"reply OK"}]}'
```

After Haiku passes, probe Sonnet and Opus through the same proxy path. Direct
OAuth calls may be allowlisted, but the transparent proxy identity path is the
serving path that should make Sonnet/Opus work.

## Current Serving Architecture

Production currently routes through a transparent proxy, not through one
LiteLLM container per account:

```text
198 prod LiteLLM
  -> claude-max-opus / claude-max-sonnet / claude-max-haiku
  -> claude-max-proxy :3456
  -> Anthropic API with Claude Code CLI identification
```

`claude-max-proxy.py` injects the Claude Code identity markers:

- URL query `?beta=true`
- `anthropic-beta` including `claude-code-20250219`
- `x-app: cli` and Claude CLI style user agent
- two leading `system` blocks, including the billing header and SDK-agent intro

This is why Opus/Sonnet work despite OAuth direct-call allowlisting.

## Upstream Quota Probe: 5h And 7d

Use this when the user asks whether CC Max is near limit, in fallback, or being
shared by other buyers.

Primary command:

```bash
./scripts/anthropic-onboard/cc-max-upstream-status.sh
./scripts/anthropic-onboard/cc-max-upstream-status.sh --watch 60
./scripts/anthropic-onboard/cc-max-upstream-status.sh --json
```

What it actually does:

- Uploads `scripts/anthropic-onboard/cc-max-upstream-status.py` to the active
  runtime host and runs it there. The wrapper defaults to 198
  `AIYJY-litellm`; set `CC_MAX_QUOTA_ASSET=<asset>` only for legacy 188 or
  another active egress host.
- Reads `ANTHROPIC_OAUTH_TOKEN` from `/Data/anthropic-auth/acct-*/.env` by
  default on that runtime host. It does not automatically inspect ACK/K8s
  Secrets or unrelated runtime hosts.
- Sends one tiny Haiku probe per account to
  `https://api.anthropic.com/v1/messages?beta=true`.
- Uses Claude Code identity headers and system preamble, including
  `claude-code-20250219`, `x-app: cli`, and a Claude CLI user agent.
- Parses Anthropic response headers:
  `anthropic-ratelimit-unified-5h-utilization`,
  `anthropic-ratelimit-unified-7d-utilization`,
  `anthropic-ratelimit-unified-fallback`,
  `anthropic-ratelimit-unified-5h-reset`,
  `anthropic-ratelimit-unified-7d-reset`.

Output meaning:

| Field | Meaning |
|---|---|
| `5h` | Rolling 5-hour utilization. Main short-window pressure signal. |
| `7d` | Rolling 7-day utilization. Long-window / weekly pool pressure signal. |
| `fallback` | `ON` means Anthropic reports fallback available after 5h pressure. |
| `5h reset` / `7d reset` | Relative and local absolute reset time parsed from Anthropic headers. |

Thresholds:

- `5h > 50%`: fallback zone. Watch growth; high-context users can push it to
  100% quickly.
- `5h > 80%`: red line. Stop or revoke heavy CC Max users temporarily, or route
  them back to Wangsu/default paid upstream.
- `7d > 70%`: long-window capacity risk; adding accounts may be safer than
  waiting for reset.
- HTTP `401` / `403`: token invalid, revoked, expired, or the `.env` file is not
  the intended token. This is not a quota signal.

Important caveats:

- Each run consumes a negligible Haiku request per account, but do not poll
  faster than 30 seconds. `--watch` enforces a 30s minimum.
- The script's default scope is the active 198 runtime auth directory. For ACK
  isolated accounts such as `ccmax-proxy-acct-N-test`, either add that account
  to a controlled auth directory and run with `--auth-dir`, or make a separate
  K8s-safe probe that reads the OAuth Secret without printing it.
- `sk-ant-oat` direct Opus/Sonnet 429 is not a reliable plan/quota check because
  OAuth direct calls are model-allowlisted. Use the v3 transparent proxy path
  for Sonnet/Opus functional checks, and use the Haiku header probe for unified
  5h/7d utilization.
- If the script reports fewer accounts than expected, first verify you are on
  the intended runtime host (`AIYJY-litellm` by default, or
  `CC_MAX_QUOTA_ASSET=<asset>` for another backend). Then check which `.env`
  files exist under that host's `/Data/anthropic-auth`.

Multi-buyer diagnosis:

```text
our LiteLLM SpendLogs prompt/completion tokens over 30-60m
  vs
upstream 5h utilization delta over the same window
```

If upstream utilization grows much faster than our SpendLogs can explain, the
seller account is likely shared by other buyers. OAuth tokens are independent,
but the `unified-5h/7d-utilization` bucket is account/workspace level.

## Deploy Or Move The Proxy

Use the existing files instead of rewriting the server:

```text
scripts/anthropic-onboard/claude-max-proxy.py
scripts/anthropic-onboard/claude-max-proxy.Dockerfile
scripts/anthropic-onboard/docker-compose.claude-max-proxy.yml
```

For the existing 188 deployment, the runtime directory is:

```text
/Data/claude-max-proxy/
```

The `.env` format is:

```text
ACCT_TOKENS=acct-1::sk-ant-oat...,acct-2::sk-ant-oat...
```

Never print the token values. When adding an account, edit the runtime `.env`
on the target host and restart the compose service.

## 198 LiteLLM Wiring

Global model entries are added with:

```bash
./scripts/anthropic-onboard/patch-litellm-claude-max.py prod
```

This patches 198 prod `litellm-product` with:

```text
claude-max-opus   -> anthropic/claude-opus-4-7   -> claude-max-proxy
claude-max-sonnet -> anthropic/claude-sonnet-4-6 -> claude-max-proxy
claude-max-haiku  -> anthropic/claude-haiku-4-5  -> claude-max-proxy
```

After config changes on 198:

```bash
scripts/jms ssh AIYJY-litellm \
  'kubectl -n litellm-product rollout restart deploy/litellm-proxy &&
   kubectl -n litellm-product rollout status deploy/litellm-proxy'
```

Per-key access uses:

```bash
./scripts/anthropic-onboard/claude-max-grant-key.sh <key_alias> --alias
./scripts/anthropic-onboard/claude-max-grant-key.sh <key_alias> --revoke
```

## Malaysia Egress Variant

Use this path when the user wants one CC Max account to egress from one
Malaysia public IP and be consumed by 198 prod LiteLLM without Cloudflare.

Serving shape:

```text
198 LiteLLM
  -> 10.68.13.198:<local-tunnel-port>
  -> SSH tunnel over 47.250.197.56:22
  -> cc-proxy:127.0.0.1:<remote-proxy-port>
  -> claude-max-proxy with UPSTREAM_SOURCE_IP=<private-egress-ip>
  -> Anthropic sees the paired Malaysia public IP
```

Known host facts:

- `cc-proxy` is Alibaba Cloud Malaysia `ap-southeast-3`.
- `eth0 10.0.0.79 -> 47.250.197.56`
- `eth1 10.0.0.80 -> 47.250.210.109`
- `eth2 10.0.0.81 -> 47.250.129.207`
- Default route currently prefers `eth2`, so explicit binding or policy routing is required for `47.250.197.56` egress.
- `cc-proxy` cannot directly reach `10.68.13.198`; 198 must initiate access.
- Public IP reachability does not imply the proxy port is open. In one run,
  198 could reach `47.250.197.56:22` but not `:3456`; use SSH local
  forwarding over port 22 unless the Alibaba security group has explicitly
  allowed the proxy port from `58.241.5.230/32`.

### Malaysia Account SOP

Allocate one account to one runtime port pair and one egress IP. Keep a small
table in the session notes; never write real emails, session keys, OAuth tokens,
or proxy API keys into the skill.

Known allocation from 2026-06-06:

| Account | cc-proxy port | 198 tunnel port | Egress private IP | Egress public IP | 198 model names |
|---|---:|---:|---|---|---|
| `acct-16` | `3456` | `3457` | `10.0.0.79` | `47.250.197.56` | `claude-max-my-{haiku,sonnet,opus}` |
| `acct-17` | `3458` | `3459` | `10.0.0.80` | `47.250.210.109` | `claude-max-my-{haiku,sonnet,opus}` |
| `acct-18` | `3460` | `3461` | `10.0.0.81` | `47.250.129.207` | `claude-max-my-{haiku,sonnet,opus}` |
| `acct-16..18 random` | `3462` | `3463` | per selected backend | per selected backend | `claude-max-my-random-*` |

All three Malaysia public IPs are currently allocated. Add more accounts only
after adding another egress IP/interface or intentionally sharing an existing
egress IP.

#### 1. Onboard the account on cc-proxy

Choose the flow from "Onboarding Flow Selection" above. Store the seller line
only in a root/user-only `.creds` file on `cc-proxy`, then remove any local temp
file. Expected files:

```text
/Data/anthropic-auth/acct-N/.creds   # mode 600, contains session_key or relay_token
/Data/anthropic-auth/acct-N/.env     # mode 600, contains ANTHROPIC_OAUTH_TOKEN
```

171mail details:

- Open `https://b.171mail.com/#/home/code?type=claude&token=<query-token>`
  first. Newer pages can render the Claude magic-link directly in an input
  value with a link icon.
- If no result renders, fill the query token into
  `https://b.171mail.com/#/home/code` and click "获取验证码".
- Treat either result as valid: a Claude `magic-link` URL or a 6-digit Claude
  email verification code.
- If the result appears locally but not from `cc-proxy`, use the manual
  callback path. This is an environment/relay-site issue, not necessarily a bad
  account.

Manual callback fallback:

1. Start `claude setup-token` on `cc-proxy` and give the OAuth URL to the user.
2. User opens the URL in the already-logged-in Claude browser and clicks
   `Authorize`.
3. User returns the callback `code#state`.
4. Feed that value back to the `tmux` `claude setup-token` prompt.
5. Capture `sk-ant-oat`, write `.env`, and continue proxy/tunnel/LiteLLM setup.

The working pattern from `acct-16`:

- Create a dedicated runtime user such as `ccmax16`.
- Install only the needed runtime on `cc-proxy`: `nodejs`, `npm`,
  `python3-venv`, `xvfb`, Chromium/patchright browser deps, and Claude Code CLI.
- Run `claude setup-token` under `tmux`.
- Run `cc-oauth-sessionkey.py` with `PLAYWRIGHT_BROWSERS_PATH` and `xvfb-run`.
- Feed the callback code back to `claude setup-token`.
- Write only `ANTHROPIC_OAUTH_TOKEN` to `.env`.
- Run a tiny Haiku direct OAuth probe and capture only status, token length,
  and 5h/7d utilization headers. Do not print token values.

Browser dependency failure to remember:

```text
libatk-1.0.so.0 missing -> install Playwright/Chromium system deps, then rerun.
```

#### 2. Run one claude-max-proxy per account

Copy `scripts/anthropic-onboard/claude-max-proxy.py` to a per-account runtime
directory such as:

```text
/Data/claude-max-proxy-acct-N/proxy.py
/Data/claude-max-proxy-acct-N/.env
```

The proxy `.env` must contain only runtime secrets and must stay mode `600`:

```text
ACCT_TOKENS=acct-N::<oauth-token>
PORT=<remote-proxy-port>
API_KEYS=<random-proxy-key>
UPSTREAM_SOURCE_IP=<private-egress-ip>
```

The stock proxy does not have a source-IP option. Patch only the remote runtime
copy, not the repository file, to pass `source_address=(UPSTREAM_SOURCE_IP, 0)`
into `http.client.HTTPSConnection`. Add `source_ip` to `/health` so verification
can prove the intended binding:

```text
GET /health -> {"ok": true, "accounts": ["acct-N"], "source_ip": "10.0.0.xx"}
```

Create one systemd unit per account, for example:

```text
/etc/systemd/system/ccmax-proxy-acctN.service
```

The service should run as the dedicated account user, read the `.env`, bind the
chosen `PORT`, restart automatically, and avoid printing secrets.

Verify on `cc-proxy`:

```bash
curl -fsS http://127.0.0.1:<remote-proxy-port>/health
python3 - <<'PY'
import http.client, ssl
c = http.client.HTTPSConnection(
    "ifconfig.me", timeout=12, context=ssl.create_default_context(),
    source_address=("<private-egress-ip>", 0),
)
c.request("GET", "/ip", headers={"User-Agent": "ccmax-check"})
print(c.getresponse().read().decode().strip())
PY
```

Then send tiny `/v1/messages` probes through the local proxy for Haiku, Sonnet,
and Opus. The `acct-16` path passed:

```text
claude-haiku-4-5
claude-sonnet-4-6
claude-opus-4-6
claude-opus-4-8
```

#### 3. Bridge 198 to cc-proxy with SSH local forwarding

If the Alibaba security group does not allow the proxy port, do not fight it
with Cloudflare. Use 198-initiated SSH local forwarding over `47.250.197.56:22`.

On 198, create one tunnel key per account:

```bash
ssh-keygen -t ed25519 -N "" \
  -f /root/.ssh/ccmax-acctN-tunnel \
  -C "ccmax-acctN-tunnel-198-to-cc-proxy"
```

On `cc-proxy`, append the public key to `/root/.ssh/authorized_keys` with a
restricted option. Adjust the remote port per account:

```text
restrict,port-forwarding,permitopen="127.0.0.1:<remote-proxy-port>" ssh-ed25519 ...
```

Create a 198 systemd service:

```text
/etc/systemd/system/ccmax-acctN-tunnel.service
```

Use this shape:

```text
ExecStart=/usr/bin/ssh -i /root/.ssh/ccmax-acctN-tunnel \
  -o BatchMode=yes -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -N \
  -L 10.68.13.198:<local-tunnel-port>:127.0.0.1:<remote-proxy-port> \
  root@47.250.197.56
Restart=always
RestartSec=5
```

Bind to `10.68.13.198`, not `127.0.0.1`, because LiteLLM pods run in their own
network namespace and need to reach the host address.

Verification:

```bash
systemctl is-enabled ccmax-acctN-tunnel.service
systemctl is-active ccmax-acctN-tunnel.service
ss -ltnp | grep <local-tunnel-port>
curl -fsS http://10.68.13.198:<local-tunnel-port>/health
kubectl exec -n litellm-product <litellm-pod> -- \
  python -c 'import urllib.request; print(urllib.request.urlopen("http://10.68.13.198:<local-tunnel-port>/health", timeout=10).read().decode())'
```

If SSH intermittently fails during setup, check `cc-proxy` ssh logs. In the
`acct-16` run, public SSH scanning triggered `MaxStartups throttling`, which can
drop 198's tunnel attempts even when the key is correct. Retry after the burst
or tune sshd separately.

#### 4. Register 198 LiteLLM models as an isolated group

Do not replace production `claude-max-*` until the new path is proven. First add
an isolated model group with a distinct prefix, for example:

```text
claude-max-my-haiku
claude-max-my-sonnet
claude-max-my-opus
```

Register through the 198 admin API `/model/new`, using:

```text
api_base = http://10.68.13.198:<local-tunnel-port>
api_key  = <proxy API key from /Data/claude-max-proxy-acct-N/.env>
model    = anthropic/claude-haiku-4-5
model    = anthropic/claude-sonnet-4-6
model    = anthropic/claude-opus-4-8
model_info.id = ccmax-my-acctN-{haiku,sonnet,opus}
```

If a bad direct-public `api_base` was already registered, delete by
`model_info.id` and recreate. `/model/info` may hide the `api_key`; that is
normal. It should show the correct `api_base`.

After registration, restart 198 prod LiteLLM to clear route/model caches:

```bash
scripts/jms ssh AIYJY-litellm \
  'kubectl -n litellm-product rollout restart deploy/litellm-proxy &&
   kubectl -n litellm-product rollout status deploy/litellm-proxy --timeout=180s'
```

End-to-end smoke:

```bash
scripts/jms ssh AIYJY-litellm '
MK=$(kubectl get secret litellm-secrets -n litellm-product \
  -o jsonpath="{.data.LITELLM_MASTER_KEY}" | base64 -d)
for m in claude-max-my-haiku claude-max-my-sonnet claude-max-my-opus; do
  curl -sS -m 120 -X POST http://localhost:30402/v1/chat/completions \
    -H "Authorization: Bearer $MK" -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"reply OK\"}],\"max_tokens\":10}"
done'
```

The `acct-16` run passed all three model names with HTTP 200.

#### 4b. Add a random aggregate group for one-key users

When a user should share all Malaysia accounts instead of pinning to one IP,
run a tiny random fan-out proxy on `cc-proxy`:

```text
/Data/claude-max-proxy-random/proxy-random.py
/etc/systemd/system/ccmax-proxy-random.service
cc-proxy:127.0.0.1:3462
198:10.68.13.198:3463 -> SSH local tunnel -> cc-proxy:127.0.0.1:3462
```

The random proxy should:

- expose `/health` with per-backend counters;
- randomly choose among local per-account proxies, such as `3456`, `3458`,
  and `3460`;
- forward `/v1/messages` without printing request bodies, API keys, or tokens;
- preserve streaming by flushing line-oriented SSE chunks immediately.

Register distinct 198 model groups so product models can map to exact upstream
model versions:

```text
claude-max-my-random-opus-4-6 -> anthropic/claude-opus-4-6 -> http://10.68.13.198:3463
claude-max-my-random-opus-4-7 -> anthropic/claude-opus-4-7 -> http://10.68.13.198:3463
claude-max-my-random-opus-4-8 -> anthropic/claude-opus-4-8 -> http://10.68.13.198:3463
claude-max-my-random-sonnet   -> anthropic/claude-sonnet-4-6 -> http://10.68.13.198:3463
claude-max-my-random-haiku    -> anthropic/claude-haiku-4-5  -> http://10.68.13.198:3463
```

Add router fallbacks from each random group to the matching Wangsu product
model. Keep the existing Wangsu Opus fallback chain intact; the first fallback
target for random Opus 4.6 should be `anthropic.claude-opus-4-6`, not 4.8.

For selected `claude-code-*` keys, do not expose the random model groups in the
key allowlist. Instead use key-level aliases from product model IDs to random
groups. This keeps `/v1/models` clean while preserving Malaysia-first routing.
See `codex-ccmax-litellm` "Products-Only Key Routing".

Do not add key-level `router_settings.fallbacks` for these keys unless there is
a deliberate per-key override. LiteLLM applies key router settings before global
router settings, so an old key-level fallback list can hide the global
`claude-max-my-random-* -> anthropic.claude-*` fallbacks. If a key logs:

```text
No fallback model group found for original model_group=claude-max-my-random-...
```

while `/app/config.yaml` already contains the random fallbacks, inspect and
clear that key's `router_settings` via the `codex-ccmax-litellm` fallback repair
flow. Keep the aliases if the user should remain Malaysia-first; clear both
aliases and `router_settings` only for an emergency Wangsu restore.

Random routing is request-level, not session-sticky. That is acceptable for
short independent Claude Code/API requests. If a future client needs account
stickiness for long sessions, add an explicit sticky key before using this
proxy for that traffic.

#### 4c. Streaming compatibility for Claude Code clients

If Claude Code appears to "think for minutes and then return everything at
once", check streaming at both layers:

```text
client -> 198 LiteLLM -> 198 local tunnel -> cc-proxy random proxy -> acct proxy -> Anthropic
```

The per-account proxy and random proxy must stream Server-Sent Events as
decoded lines and flush each line. Do not read from `resp.fp.readline()` on an
HTTP chunked response; that can leak raw chunk framing such as hexadecimal
chunk sizes into the client stream. The safe pattern is byte-by-byte decoding
into lines, then writing completed SSE lines and flushing.

Runtime env conventions:

```text
SSE_READ_MODE=line
THINKING_DISPLAY_MODE=preserve
```

Add response headers that discourage buffering:

```text
Cache-Control: no-cache
X-Accel-Buffering: no
```

Verification bar:

- A streamed Haiku or Opus request through 198 returns first bytes within a few
  seconds, not only after the full upstream completion.
- No raw chunk-size lines appear in the client stream.
- SpendLogs show `model_group=claude-max-my-random-*` and
  `api_base=http://10.68.13.198:3463/v1/messages`.

#### 4d. Malaysia quota and upstream pressure checks

For the Malaysia pool, the default 198 quota wrapper does not automatically see
tokens unless that runtime has the matching auth directory. Point the same
wrapper at the Malaysia runtime with `CC_MAX_QUOTA_ASSET=<cc-proxy asset>` when
available, or run the Python probe on `cc-proxy` against:

```text
/Data/anthropic-auth/acct-16/.env
/Data/anthropic-auth/acct-17/.env
/Data/anthropic-auth/acct-18/.env
```

Report only:

- account label;
- HTTP status;
- 5h and 7d utilization headers;
- reset countdown if available;
- whether fallback headers indicate pressure.

Never print OAuth token values. If comparing SpendLogs to upstream quota,
remember SpendLogs measure our LiteLLM usage, while upstream utilization can
include other sessions sharing the same Anthropic subscription account.

#### 4e. CCMax Pool Guard and circuit breaker

Use the pool guard when the user wants automatic removal/recovery based on
upstream quota or acct-level request limits. The guarded architecture must stay
outside LiteLLM so LiteLLM upgrades and ConfigMap changes are unaffected:

```text
198 LiteLLM -> 198 local tunnel :3463 -> cc-proxy guarded random proxy :3466
  -> active-upstreams.json -> per-account proxy -> Anthropic
```

Sidecar deployment facts from 2026-06-08:

```text
cc-proxy:/Data/ccmax-pool-guard/
  config.json
  state.json
  active-upstreams.json
  events.jsonl
  ccmax-pool-guard.py

cc-proxy:/Data/claude-max-proxy-random-guarded/
  .env
  proxy.py

systemd:
  ccmax-pool-guard.timer
  ccmax-pool-guard.service
  ccmax-proxy-random-guarded.service

sidecar port:
  127.0.0.1:3466

198 validation tunnel:
  ccmax-guarded-random-tunnel.service
  10.68.13.198:3465 -> cc-proxy:127.0.0.1:3466
```

Threshold behavior:

- Drain when `5h >= 70%` or `7d >= 90%`.
- Fast-drain when `5h >= 75%` or `7d >= 95%`.
- Recover only after cooldown/reset when `5h < 40%` and `7d < 80%`.
- `401` / `403` means `HARD_DOWN`; require manual account/token review.
- Per-account `rpm_limit` and `concurrency_limit` are enforced inside the
  guarded random proxy before a request reaches any account proxy.

Safety rules:

- Do not replace the existing production `ccmax-proxy-random.service` until the
  guarded sidecar passes health, non-streaming, streaming, empty-pool, and rate
  limit checks.
- Do not change 198 LiteLLM ConfigMap, DB model rows, or user key aliases for
  the guarded rollout. The production switch should be tunnel-only: keep
  `10.68.13.198:3463` local listen address and change the remote target from
  `cc-proxy:127.0.0.1:3462` to `cc-proxy:127.0.0.1:3466`.
- The guarded proxy must accept the existing production random proxy API key
  from `/Data/claude-max-proxy-random/.env`; otherwise tunnel-only switch would
  make LiteLLM requests fail with 401.
- Rollback is the reverse tunnel-only change back to `3462`.
- `active-upstreams.json` must not contain OAuth tokens or proxy API key values;
  it references per-account proxy `.env` files by path.

Useful checks:

```bash
scripts/jms ssh cc-proxy \
  'systemctl is-active ccmax-pool-guard.timer ccmax-proxy-random-guarded.service &&
   curl -sS http://127.0.0.1:3466/health'

scripts/jms ssh cc-proxy \
  'python3 /Data/ccmax-pool-guard/ccmax-pool-guard.py --config /Data/ccmax-pool-guard/config.json --dry-run'
```

Detailed runbook:

```text
docs/ccmax-pool-guard-runbook.md
```

#### 4f. 10.68.13.224 Astrill egress variant

Use this path when the user wants all CC Max traffic to egress through the
desktop/VPN host `10.68.13.224` instead of the Malaysia `cc-proxy` host.

Serving shape:

```text
198 LiteLLM
  -> 10.68.13.198:3467
  -> SSH local tunnel to 10.68.13.224
  -> 224:127.0.0.1:3456 ccmax-proxy-acct19
  -> Astrill tun0
  -> api.anthropic.com /v1/messages?beta=true
```

Environment facts from the acct-19 rollout:

- Runtime host: `10.68.13.224` (`aiyjy-cc-proxy`), user `cltx`.
- Astrill full-tunnel creates `tun0`; LAN traffic to `10.68.13.198` stays on
  the physical interface. Verify with `ip route get api.anthropic.com`,
  `ip route get 10.68.13.198`, and `curl https://api.ipify.org`.
- Per-account proxy service on 224:
  `ccmax-proxy-acct19.service`, `127.0.0.1:3456`,
  `/Data/claude-max-proxy-acct-19/`.
- 198 tunnel service:
  `ccmax-acct19-224-tunnel.service`,
  `10.68.13.198:3467 -> 10.68.13.224:127.0.0.1:3456`.
- The 198 tunnel key is authorized on 224 with a restricted
  `permitopen="127.0.0.1:3456"` option. Do not expose the proxy with NodePort,
  Cloudflare, or a public listener.

SessionKey onboarding notes:

- A seller-provided `sk-ant-sid02-*` is a Claude session cookie, not an email
  login credential. Do not fill mailbox password, query 171mail, or ask the
  user to log into Claude when a complete sessionKey is present.
- Still run `claude setup-token` in `tmux` to obtain the OAuth authorize URL,
  inject the `sessionKey` cookie into a browser context, click/authorize, then
  feed `<callback-code>#<state>` back to the `tmux` prompt.
- On bare-metal Ubuntu 26.04, Snap Chromium can render the Claude OAuth page as
  a blank white page under automation. Install/use Google Chrome stable and set
  the patchright launch `executable_path` to `/usr/bin/google-chrome`.
- The Docker-oriented sessionKey script writes screenshots under
  `/work/screenshots`; for bare-metal runs, patch the runtime copy to use
  `CC_SCREENSHOTS_DIR` or a `/tmp/cc-screenshots-*` directory. Do not patch the
  repository copy for one host.
- Only accept callback codes from
  `platform.claude.com/oauth/code/callback?...code=<real-code>`. The authorize
  URL contains `code=true`; never treat that value as the callback code or
  `claude setup-token` will return OAuth 400.

Fable 5 notes:

- The proxy should advertise and pass through `claude-fable-5` alongside
  Haiku, Sonnet, and Opus.
- Register LiteLLM Fable entries with
  `model=anthropic/claude-fable-5`.
- Fable can emit a `thinking` block before text. A tiny `max_tokens` probe can
  return HTTP 200 with no text. Verify Fable with `max_tokens >= 128` and a
  prompt such as `Say exactly OK`.

Astrill node-change SOP:

- Restart only the Astrill GUI/VPN processes when the user wants to switch VPN
  nodes. Do not restart the machine, `x11vnc`, `openbox`, the per-account proxy,
  or the 198 tunnel.
- Before touching Astrill, record:

  ```bash
  pgrep -af 'Astrill|astrill|asproxy|asovpnc' || true
  ss -lntp 2>/dev/null | grep -E '(:5901|:3456)' || true
  ip route get 10.68.13.198 || true
  ip route get 1.1.1.1 || true
  ```

- To reset the UI to an OFF/choose-node state:

  ```bash
  pkill -u cltx -f '^/usr/local/Astrill/astrill$' || true
  pkill -u cltx -f '/usr/local/Astrill/asproxy' || true
  pkill -u cltx -f '/usr/local/Astrill/asovpnc' || true
  sleep 2
  rm -f /run/astrill-gui/astrill.pid /tmp/astrill-manual-restart.pid 2>/dev/null || true
  DISPLAY=:99 HOME=/home/cltx nohup /usr/local/Astrill/astrill \
    >/tmp/astrill-manual-restart.log 2>&1 &
  ```

- After restart, `127.0.0.1:5901` and `:3456` must still be listening. If they
  are not, fix the GUI/VNC/proxy separately; do not continue changing VPN nodes.
- When Astrill is connected, expect `tun0`, `asproxy`, and `asovpnc`. External
  routes such as `1.1.1.1` should go via `tun0`; management traffic to
  `10.68.13.198` must stay on `ens1`.
- To identify the exit location, prefer two independent Geo services because
  some IP-check endpoints reset under Astrill:

  ```bash
  curl -m 12 -sS https://ipinfo.io/json || true
  curl -m 12 -sS 'http://ip-api.com/json/?fields=status,country,countryCode,regionName,city,isp,org,as,query,timezone' || true
  curl -m 12 -sS https://ipapi.co/json || true
  ```

- If `api.ipify.org`, `api.myip.com`, or `ipapi.co` times out/resets but
  `ipinfo.io` and `ip-api.com` agree, use the agreeing results and note the
  failed endpoint. A failed endpoint is not by itself proof the VPN is broken.
- If there is no `tun0`, no `asproxy/asovpnc`, and the public IP is still the
  China Unicom LAN egress, Astrill is only open in the GUI and is not connected.
  Ask the user to select a node and click ON in VNC.

SSH/VNC access when Astrill changes routes:

- Local direct SSH to `10.68.13.224` can fail while Astrill is connected, even
  when 198-to-224 remains healthy. Distinguish these cases:

  ```bash
  # From local workstation
  ping -c 1 -W 2 10.68.13.224 || true
  nc -vz -w 3 10.68.13.224 22 || true

  # From 198
  scripts/jms ssh AIYJY-litellm \
    'ping -c 1 -W 2 10.68.13.224 >/dev/null && echo ok || echo fail; nc -vz -w 3 10.68.13.224 22'
  ```

- If local direct access fails but 198 access succeeds, continue through 198.
  The helper script is:

  ```bash
  scripts/ssh-224-via-198.sh check
  scripts/ssh-224-via-198.sh ssh
  scripts/ssh-224-via-198.sh cmd 'hostname; whoami; ip route get 10.68.13.198 | head -1'
  scripts/ssh-224-via-198.sh vnc
  ```

- The helper uses `scripts/jms proxy AIYJY-litellm ... 10.68.13.224:22` and
  then SSHes to the local proxy port. It must not store the 224 password; SSH
  prompts interactively. `check` should show `proxy_ok` and either
  `ssh_login_ok` or `ssh_port_ok_password_required`.
- For VNC, keep the tunnel command running. The VNC URL is
  `vnc://127.0.0.1:<local-port>`. If the default local `5901` is already in use,
  the helper chooses a free local port.
- Do not conclude SSH is broken from local failure alone. Confirm whether
  `198 -> 224:22` works, then log in through the helper if needed.

198 cleanup when the user says "only acct-19":

- Prefer `/model/info` and systemd state to understand the live config; use SQL
  only for backups or deeper forensics.
- Rebuild all ccmax-compatible model names so their `model_info.acct` is
  `acct-19` and their `api_base` is `http://10.68.13.198:3467`:
  `claude-max-*`, `claude-max-my-random-*`, `fable5`, and
  `claude-max-224-*`.
- Remove static ConfigMap `model_list` entries named
  `claude-max-opus`, `claude-max-sonnet`, and `claude-max-haiku` if they still
  point at old hosts such as `10.68.13.188:3456`. Otherwise `/model/info` shows
  duplicate `claude-max-*` rows with no `acct` metadata.
- Stop/disable old tunnels such as `ccmax-acct16-tunnel.service`,
  `ccmax-random-tunnel.service`, and `ccmax-guarded-random-tunnel.service`.
  Only `ccmax-acct19-224-tunnel.service` should remain active for the Astrill
  path.

Verification bar for the Astrill-only state:

```text
/model/info ccmax rows: acct_counts == acct-19 only
systemd on 198: only ccmax-acct19-224-tunnel.service active
ss on 198: only 10.68.13.198:3467 for ccmax
224 /health: {"ok": true, "accounts": ["acct-19"], ...}
224 egress IP: external IP from Astrill, not the LAN host
224 route to 10.68.13.198: ens1, not tun0
224 route to public internet: tun0 when Astrill is ON
representative calls:
  claude-max-opus
  claude-max-my-random-opus-4-8
  fable5
  claude-max-224-fable-5
all return HTTP 200; Fable uses max_tokens >= 128
```

#### 5. Rollback

Rollback is explicit and narrow:

- Remove the three isolated 198 model entries by `model_info.id`.
- Stop/disable `ccmax-acctN-tunnel.service` on 198.
- Stop/disable `ccmax-proxy-acctN.service` on the runtime host (`cc-proxy`,
  `10.68.13.224`, or the current host for that account).
- Remove the restricted tunnel public key from the runtime host if the account
  is retired.
- Keep `/Data/anthropic-auth/acct-N/.env` only if the token is still needed for
  later reuse; otherwise delete the per-account auth and proxy directories.

Only after the isolated group passes and the user explicitly asks should you
move or alias production `claude-max-*` traffic to the chosen egress path.

## Verification

Use bottom-up checks:

```bash
# Upstream account and quota snapshot
./scripts/anthropic-onboard/cc-max-upstream-status.sh

# 198 local health and route availability
scripts/jms ssh AIYJY-litellm \
  'curl -sS http://localhost:30402/model/info -H "Authorization: Bearer $MK"'
```

For user-facing route proof, use SpendLogs on 198. A CC Max request should route
to `claude-max-*` and the proxy `api_base`, not Wangsu `aigateway`, unless
fallback intentionally triggered.

## Common Failure Modes

| Symptom | First check |
|---|---|
| OAuth direct Opus/Sonnet returns 429 | Normal allowlist; use v3 proxy path. |
| New account gets token but no Opus traffic | Is token in proxy `ACCT_TOKENS`, and did compose restart? |
| 198 cannot reach new proxy | Security group/firewall/listen address; test from `AIYJY-litellm`. |
| Anthropic sees wrong country | Default route or proxy outbound binding wrong; check interface egress. |
| `claude-max-*` not found | Run `patch-litellm-claude-max.py prod` and rollout 198 LiteLLM. |
| One user still routes Wangsu | Per-key aliases/models not granted or LiteLLM key cache still stale. |
| Global random fallback exists but one key still fails | That key may have old `router_settings.fallbacks`; clear the key-level router settings and flush Redis. |

## Completion Bar

Do not call the pool change complete until:

- Token files exist only on the target runtime host and are not printed.
- `claude-max-proxy` is running and includes the intended accounts.
- Upstream quota/status is known for the active runtime host.
- 198 can reach the proxy endpoint.
- 198 `claude-max-*` model entries point to the intended proxy.
- A minimal request succeeds and SpendLogs prove the expected route.
- Rollback path is clear: revert 198 `api_base`, revoke per-key aliases, or stop the new proxy.
