# zerokey-codex on 188 — operations runbook

OpenAI-compatible bridge: ChatGPT **web chat quota** → API on `10.68.13.188:8123+`.
Standalone Docker on 188; does NOT touch K8s / carher-admin.

**Full design:** `docs/chatgpt-web-to-codex-zerokey.md`  
**Index (skills / scripts):** `docs/zerokey-codex-artifacts.md`  
**Agent roadmap:** `docs/zerokey-codex-agent-bridge-design.md`

## Layout

```text
~/zerokey-codex/                    # default account (kristine), :8123
~/zerokey-codex-accounts/<id>/      # extra accounts, e.g. timothy :8124
  zerokey/   state/   secrets/   capture/   ops/   logs/
```

## Scripts (this directory)

| File | Purpose |
|------|---------|
| `refresh.sh` | Auto re-capture → validate → swap `users.json` → restart |
| `capture-manual.sh` | Interactive capture when `REFRESH_STALE` |
| `add-account.sh` | New account: secrets + capture + container on `[port]` |
| `docker-compose.account.yml` | Per-account compose template |
| `litellm-register-zerokey.py` | Register 8 zerokey models on 198 (+ manifest sync) |

## Two request modes (`Authorization`)

| Header | Path | Use |
|--------|------|-----|
| `Bearer vscode` | ToolCompiler | VS Code tool grammar; stateful session |
| `Bearer raw` | raw passthrough | Stateless chat; **LiteLLM upstream uses this** |

## Server

```bash
cd ~/zerokey-codex/zerokey   # or ~/zerokey-codex-accounts/<id>/zerokey
docker compose up -d --build
curl -s localhost:8123/health
curl -s localhost:8123/v1/models | head
```

## Codex via 198 LiteLLM (2026+)

Codex requires `wire_api = "responses"`. Point at 198, model `zerokey-gpt-5.5` or
`zerokey-timothy-gpt-5.5`. See main doc §Codex.

Register/repair on 198:

```bash
python3 litellm-register-zerokey.py              # dry-run
python3 litellm-register-zerokey.py --apply --sync-manifest
```

Each zerokey model needs `use_chat_completions_api: true` in live cm.

Verify on 198:

```bash
MK=$(kubectl get secret litellm-secrets -n litellm-product -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -s -X POST -H "Authorization: Bearer $MK" localhost:30402/v1/responses \
  -d '{"model":"zerokey-gpt-5.5","input":"hi","stream":false}'
```

## Multi-account

| Account | Port | LiteLLM prefix |
|---------|------|----------------|
| kristine | 8123 | `zerokey-gpt-5.5` … |
| timothy | 8124 | `zerokey-timothy-gpt-5.5` … |

```bash
cd ~/zerokey-codex/ops
./add-account.sh <id> <email> '<mail_pw>' '<chatgpt_pw>' [port]
```

OTP/login: skill `.codex/skills/chatgpt-login-session/SKILL.md`.

## Cron (every 6h)

```cron
0 */6 * * * ~/zerokey-codex/ops/refresh.sh >/dev/null 2>&1
0 */6 * * * ~/zerokey-codex-accounts/timothy/ops/refresh.sh >/dev/null 2>&1
```

Optional: `ZK_ALERT_WEBHOOK="<feishu-url>"` prefix in cron or `ops.env`.

## Manual OTP re-seed

```bash
~/zerokey-codex/ops/capture-manual.sh
# when prompted:
echo <6-digit> > ~/zerokey-codex/state/out/otp.txt
```

## Troubleshooting

| Symptom | Action |
|---------|--------|
| `curl …/health` not healthy | `docker compose logs`; check `state/users.json` |
| 401/403 upstream | Session expired → `refresh.sh` or manual capture |
| LiteLLM non-stream Empty response | Rebuild after `raw.js` stream default fix |
| Codex no file edits | Expected on LiteLLM+raw; see agent-bridge design doc |
| Screenshots | `state/out/screenshots/` |
