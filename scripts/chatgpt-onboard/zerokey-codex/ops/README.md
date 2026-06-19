# zerokey-codex on 188 — operations runbook

OpenAI-compatible bridge that serves your ChatGPT **web** quota (account
`kristine_free517@mail.com`) as an API on `http://10.68.13.188:8123`, so Codex /
VS Code / any OpenAI client can use it while the Codex 5h/7d quota is exhausted.

This is a standalone Docker stack on 188. It does NOT touch K8s, carher-admin,
or any other service.

## Layout (`~/zerokey-codex/`)

```
zerokey/            zerokey repo + our patches + Dockerfile + docker-compose.yml
  routes/raw.js       raw passthrough + model resolver (our addition)
  core/chatgpt/api.js per-request model override (patched)
state/
  users.json          LIVE captured session (mounted into the server, rw)
  profile/            persistent browser profile (reused by refresh, no OTP)
  out/                capture scratch (zerokey-users.json, otp.txt, screenshots)
secrets/{mail_pw,chatgpt_pw}.txt
capture/              capture image (patchright) + zerokey-web-capture.py
ops/{refresh.sh,capture-manual.sh,README.md}
logs/
```

## Two request modes (auth header selects)

| `Authorization` | Path | Behavior |
|---|---|---|
| `Bearer vscode` (default) | ToolCompiler | VS Code tool grammar injected. Stateful web conversation. **Unchanged upstream behavior.** |
| `Bearer raw` (or `codex`/`openai`/`plain`) | raw passthrough | No tool injection. Stateless: full message history sent each call (standard OpenAI semantics). |

## Model selection (both modes)

Send any web slug as `model`. `GET /v1/models` lists them. Examples:
`gpt-5-5-pro`, `gpt-5-5-thinking`, `gpt-5-5`, `gpt-5-4-pro`, `o3`, `o3-pro`,
`gpt-4-5`, `research`. Aliases: `gpt-4o→gpt-5-mini`, `gpt-5.5→gpt-5-5`, etc.
Omitting `model` uses `ZK_DEFAULT_MODEL` (compose: `gpt-5-5`).

## Run / manage the server

```bash
cd ~/zerokey-codex/zerokey
docker compose up -d --build      # build + start (restart:always)
docker compose logs -f            # tail
docker compose restart            # reload after a session refresh
docker compose down               # stop
curl -s localhost:8123/v1/models | head
```

## Codex client config (`~/.codex/config.toml` on your laptop)

```toml
model = "gpt-5-5"
model_provider = "chatgpt-web"

[model_providers.chatgpt-web]
name = "ChatGPT web (zerokey/188)"
base_url = "http://10.68.13.188:8123/v1"
env_key = "ZK_KEY"          # value is the MODE: set ZK_KEY=raw
wire_api = "chat"
requires_openai_auth = false
```
`export ZK_KEY=raw` so Codex sends `Authorization: Bearer raw`.

## Integrated into 198 LiteLLM Pro (litellm-product)

zerokey is also wired into the 198 LiteLLM Pro proxy as upstream models, so any
LiteLLM consumer (Cursor / Codex / claude-code keys) can reach the ChatGPT web
quota by model name. 198 (`AIYJY-litellm`) reaches 188 over the internal network
directly — no tunnel.

Model entries live in ConfigMap `litellm-config` (ns `litellm-product`),
inserted before `router_settings:` in `model_list`:

```yaml
- model_name: zerokey-gpt-5.5            # also: -5.5-thinking, -5.5-pro, zerokey-o3
  litellm_params:
    model: openai/gpt-5-5                 # web slug; openai/ provider
    api_base: http://10.68.13.188:8123/v1
    api_key: raw                          # literal -> Bearer raw -> raw passthrough
    use_chat_completions_api: true        # Codex wire_api=responses bridge
    input_cost_per_token: 0
    output_cost_per_token: 0
```

**Codex 2026+** must use `wire_api = "responses"` against LiteLLM (not direct 188 +
`wire_api = "chat"`). See `docs/chatgpt-web-to-codex-zerokey.md` §Codex.

Register/repair on 198: `ops/litellm-register-zerokey.py --apply --sync-manifest`

Edit safely (cm is JSON-in-JSON, don't `kubectl apply` the stale manifest):
`kubectl get cm ... -o json` → string-splice the yaml → `kubectl replace` →
`kubectl rollout restart deployment/litellm-proxy -n litellm-product`. Helper:
`/tmp/zk-add-models.py` on 198 (idempotent, backs up to `~/zerokey-litellm-backups/`).

Verify (NodePort 30402, master key from `litellm-secrets`):
```bash
curl -s -H "Authorization: Bearer $MK" localhost:30402/v1/models | grep zerokey
curl -s -X POST localhost:30402/v1/chat/completions -H "Authorization: Bearer $MK" \
  -d '{"model":"zerokey-gpt-5.5","messages":[{"role":"user","content":"hi"}]}'
```

> **stream default fix (raw.js)**: OpenAI spec treats an absent `stream` field as
> non-streaming. LiteLLM's OpenAI SDK omits `stream` on non-stream calls, so
> zerokey's old `stream = true` default returned SSE and LiteLLM errored
> *"Empty or invalid response from LLM endpoint"*. `routes/raw.js` now defaults
> `stream = false`; explicit `stream:true` still streams. Rebuild after editing:
> `docker compose up -d --build`.
>
> For per-user access, add the `zerokey-*` names to the relevant key `models`
> allowlist via `/key/update` (see `litellm-pro-ops`); master key works already.

### Multi-account (separate ports)

| Account | Port | LiteLLM prefix | Host path |
|---|---|---|---|
| kristine | 8123 | `zerokey-gpt-5.5` … | `~/zerokey-codex/` |
| timothy | 8124 | `zerokey-timothy-gpt-5.5` … | `~/zerokey-codex-accounts/timothy/` |

Add another account (full-auto mail.com OTP on first capture):

```bash
cd ~/zerokey-codex/ops
./add-account.sh <id> <email> '<mail_pw>' '<chatgpt_pw>' [port]
```

See skill `.codex/skills/chatgpt-login-session/SKILL.md` for OTP/login details.

## Session refresh (auto)

`ops/refresh.sh` re-captures a fresh session reusing `state/profile` (no OTP
while the login is alive), validates it, atomically swaps `state/users.json`,
and restarts the server. On failure it keeps the old session, writes
`state/REFRESH_STALE`, and pings `ZK_ALERT_WEBHOOK` if set.

Cron (every 6h) — one line per account:

```cron
0 */6 * * * ~/zerokey-codex/ops/refresh.sh >/dev/null 2>&1
0 */6 * * * ~/zerokey-codex-accounts/timothy/ops/refresh.sh >/dev/null 2>&1
```

Optional alert on failure: prefix with `ZK_ALERT_WEBHOOK="<feishu-bot-url>"` (see `refresh.sh`).

The cf_clearance cookie / sentinel proof are short-lived and IP-bound to 188, so
6h keeps them fresh. The underlying web login lasts much longer; when it finally
expires, refresh fails and alerts → run the manual step.

## Session refresh (manual, OTP required)

When `state/REFRESH_STALE` appears / alert fires:
```bash
~/zerokey-codex/ops/capture-manual.sh
# in another shell, when it asks:
echo <6-digit-otp> > ~/zerokey-codex/state/out/otp.txt
```

## Health / troubleshooting

- `curl localhost:8123/health` → `{"status":"healthy"}`
- 401/403 from upstream in logs → session expired → run refresh/manual.
- Capture debugging screenshots: `state/out/screenshots/`.
- Codex agentic tool-calls (apply_patch) won't natively work: the web model
  emits prose, not OpenAI `tool_calls`. Raw mode gives clean chat passthrough;
  it's great for Q&A / code generation, not full autonomous file editing.
```
