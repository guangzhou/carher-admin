---
name: codex-ccmax-litellm
description: >-
  Use when configuring OpenAI Codex CLI to use the CarHer 198 LiteLLM gateway
  backed by the active Claude Max / CC Max pool, granting or revoking per-key
  aliases for Codex/Cursor/claude-code virtual keys, hiding internal
  claude-max-* pool model IDs from client model lists, validating Responses API
  routing through cc.auto-link.com.cn/pro, checking CC Max quota, or debugging
  Codex traffic that should route to claude-max-* / claude-max-my-random-* via
  the active CC Max backend (188 legacy, Malaysia, or 10.68.13.224 Astrill).
  Also use for planning Codex-facing expansion of the CC Max pool without
  exposing Anthropic OAuth tokens, virtual keys, cookies, or account secrets.
---

# Codex + CC Max LiteLLM

## What This Skill Covers

This is the Codex-facing wrapper around the existing Claude Max / CC Max pool:

```text
Codex CLI
  -> https://cc.auto-link.com.cn/pro/v1/responses
  -> 198 prod LiteLLM key auth + per-key aliases
  -> claude-max-* or claude-max-my-random-*
  -> active CC Max proxy/tunnel backend
     (legacy 188, Malaysia, or 10.68.13.224 Astrill)
  -> api.anthropic.com /v1/messages?beta=true
```

Use this skill for Codex client setup, key enablement, route verification,
quota triage, and Codex-specific failure handling. For acquiring or renewing
Anthropic OAuth accounts, delegate to the Claude Max onboarding runbook instead
of retyping that flow here.

## Reference Map

Read only the reference needed for the current task:

- `docs/claude-max-cli-proxy.md` - legacy 188 transparent proxy architecture, identity injection, fallback, operations.
- `docs/198-cc-max-routing-comparison.md` - 198 prod per-key alias model, baseline-key alignment, route-verification SQL, failure modes.
- `docs/codex-via-litellm-setup.md` - Codex CLI provider config and Responses API details.
- `docs/cc_max_litellm.md` - historical OAuth-direct research; useful for quota economics only, not the current prod path.
- `scripts/anthropic-onboard/claude-max-grant-key.sh` - single-key grant/revoke wrapper.
- `scripts/anthropic-onboard/cc-max-upstream-status.sh` - legacy 188 upstream 5h/7d quota snapshot; use the active runtime host probe for Malaysia or 224 Astrill accounts.
- `scripts/anthropic-onboard/patch-litellm-claude-max.py` - global LiteLLM model entry patcher.

## Safety Rules

- Never write real virtual keys, `sk-ant-oat*`, `sk-ant-sid*`, cookies, Gmail passwords, TOTP secrets, account emails, or temporary login links into skills, docs, diagrams, commits, or chat output.
- Treat `~/.codex/config.toml` as a local secret file. If showing examples, use placeholders only and remind the user to keep the file mode `600`.
- Do not clear aliases as a reflex when CC Max fails. First prove whether the issue is key config, LiteLLM route drift, active proxy/tunnel health, or upstream quota.
- Any "X caused Y" statement must use the repo's three-part diagnosis discipline: hypothesis, falsification condition, data path.

## Request Classification

1. **Client setup**: user wants Codex CLI to call CC Max. Provide or edit `~/.codex/config.toml`, then verify with `/v1/responses`.
2. **Key enablement**: user has a `claude-code-*`, `cursor-*`, or `cursor-codex-*` key alias and wants it routed to CC Max. Use the grant script or baseline-key alignment.
3. **Route debugging**: Codex works but is too expensive, failing, or not using CC Max. Verify aliases, Redis cache, SpendLogs route, and proxy/quota.
4. **Pool expansion**: user wants more CC Max capacity. Use `anthropic-max-litellm` for the active backend, then re-check proxy health, quota, and 198 route state.
5. **Products-only exposure**: user wants the client to see only product model IDs while LiteLLM privately rewrites them to hidden CC Max model groups.

## Codex Client Setup

Codex must use the Responses API. For the 198 prod path, set the base URL to
the path-prefixed gateway without `/v1`:

```toml
# ~/.codex/config.toml
model = "anthropic.claude-sonnet-4-6"
model_provider = "carher_ccmax"

[model_providers.carher_ccmax]
name = "CarHer CC Max via LiteLLM"
base_url = "https://cc.auto-link.com.cn/pro"
wire_api = "responses"
http_headers = { "Authorization" = "Bearer <LITELLM_VIRTUAL_KEY>" }
stream_idle_timeout_ms = 120000
request_max_retries = 3
stream_max_retries = 5

[profiles.ccmax-opus]
model = "anthropic.claude-opus-4-7"
model_provider = "carher_ccmax"

[profiles.ccmax-sonnet]
model = "anthropic.claude-sonnet-4-6"
model_provider = "carher_ccmax"

[profiles.ccmax-haiku]
model = "anthropic.claude-haiku-4-5"
model_provider = "carher_ccmax"
```

Use the public Anthropic-style model names above when the key has aliases. The
198 LiteLLM DB rewrites them to `claude-max-opus`, `claude-max-sonnet`, and
`claude-max-haiku` before routing.

## Claude Code Client Setup

For Claude Code CLI pointed at the 198 gateway, keep the client config in
product model names only. Do not put internal LiteLLM model groups such as
`claude-max-*` or `claude-max-my-random-*` into `~/.zshrc`,
`~/.claude/settings.json`, or `--model`.

Minimal `.zshrc` shape:

```bash
export ANTHROPIC_BASE_URL="https://cc.auto-link.com.cn/pro"
export ANTHROPIC_AUTH_TOKEN="<LITELLM_VIRTUAL_KEY>"

# Avoid duplicate /model entries: use env-injected products OR gateway
# discovery, not both.
unset CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY

export ANTHROPIC_MODEL="anthropic.claude-opus-4-8"
export ANTHROPIC_DEFAULT_OPUS_MODEL="anthropic.claude-opus-4-6"
export ANTHROPIC_DEFAULT_OPUS_MODEL_NAME="Opus 4.6"
export ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION="CC Max, fallback to Wangsu"
export ANTHROPIC_DEFAULT_SONNET_MODEL="anthropic.claude-sonnet-4-6"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="anthropic.claude-haiku-4-5"

# Claude Code currently exposes one stable custom model option.
export ANTHROPIC_CUSTOM_MODEL_OPTION="anthropic.claude-opus-4-7"
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Opus 4.7"
export ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="CC Max, fallback to Wangsu"

export ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES="effort,xhigh_effort,max_effort,thinking,adaptive_thinking,interleaved_thinking"
export ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES="effort,thinking,adaptive_thinking,interleaved_thinking"
export ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES=""
export ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES="effort,xhigh_effort,max_effort,thinking,adaptive_thinking,interleaved_thinking"
```

Expected `/model` shape:

```text
Default                 current product model, usually Opus 4.8
Opus 4.6                ANTHROPIC_DEFAULT_OPUS_MODEL
anthropic.claude-sonnet-4-6
anthropic.claude-haiku-4-5
Opus 4.7                ANTHROPIC_CUSTOM_MODEL_OPTION
```

If `/model` shows duplicate entries such as `From gateway` plus custom Opus /
Sonnet / Haiku entries, `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=true` is
still set somewhere. Disable discovery or remove all local model env injection;
do not run both sources at once.

Check for leaked internal IDs without printing secrets:

```bash
python3 - <<'PY'
from pathlib import Path
for path in [Path.home()/'.zshrc', Path.home()/'.claude/settings.json']:
    text = path.read_text(errors='replace') if path.exists() else ''
    print(path, 'INTERNAL_MODEL_PRESENT=' + str('claude-max' in text))
PY
```

## Key Enablement

For one key alias, prefer the wrapper:

```bash
./scripts/anthropic-onboard/claude-max-grant-key.sh <key_alias> --alias
```

This adds `claude-max-*` to the key's allowlist and maps:

```text
anthropic.claude-opus-4-7   -> claude-max-opus
anthropic.claude-sonnet-4-6 -> claude-max-sonnet
anthropic.claude-haiku-4-5  -> claude-max-haiku
```

To revoke a key back to the default paid Wangsu route:

```bash
./scripts/anthropic-onboard/claude-max-grant-key.sh <key_alias> --revoke
```

For bulk repair or drift cleanup, read `docs/198-cc-max-routing-comparison.md`
and copy `aliases + models` from the baseline key instead of hand-writing JSON.
Flush the LiteLLM Redis key cache after direct DB updates, or wait for the
normal 60 second TTL.

## Products-Only Key Routing

Use this when a `claude-code-*` key should expose only product model IDs to the
client, while internally routing those products to a hidden CC Max pool and
falling back to Wangsu.

The target state is:

```text
client-visible models[]:
  anthropic.claude-opus-4-6
  anthropic.claude-opus-4-7
  anthropic.claude-opus-4-8
  anthropic.claude-sonnet-4-6
  anthropic.claude-haiku-4-5
  plus any non-Claude product models the key already owns

key aliases:
  anthropic.claude-opus-4-6   -> claude-max-my-random-opus-4-6
  anthropic.claude-opus-4-7   -> claude-max-my-random-opus-4-7
  anthropic.claude-opus-4-8   -> claude-max-my-random-opus-4-8
  anthropic.claude-sonnet-4-6 -> claude-max-my-random-sonnet
  anthropic.claude-haiku-4-5  -> claude-max-my-random-haiku
  fable5                      -> claude-max-my-random-fable-5
  anthropic.claude-fable-5    -> claude-max-my-random-fable-5

router fallback:
  claude-max-my-random-* -> matching anthropic.claude-* Wangsu product model
```

Important LiteLLM behavior verified on 198 Pro: an alias target does not need
to be present in the key's `models[]` for routing to work. That allows hiding
internal groups from `/v1/models` while still routing through them. Re-test with
a temporary key after LiteLLM upgrades before applying this pattern broadly.

Key-level `router_settings` can silently break this pattern. LiteLLM applies
router settings in this order:

```text
key router_settings > team router_settings > global router_settings
```

If a key has an old `router_settings.fallbacks` value, it overrides the global
fallbacks from `litellm-config`. The symptom is:

```text
No fallback model group found for original model_group=claude-max-my-random-...
Fallbacks=[old openrouter/wangsu list without claude-max-my-random-*]
```

When enabling hidden CC Max routing, the selected key should normally have
`router_settings={}` so global fallbacks apply. If you deliberately copy a
baseline key with key-level fallbacks, make sure it includes the current
`claude-max-my-random-*` and Fable fallback entries. Do not copy old per-key
fallbacks forward unless the user explicitly needs a custom override.

### Fable 5 And Key Alignment

Use this when copying a known-good `claude-code-*` key such as
`claude-code-liuguoxian-50gj` to another key such as `claude-code-buyitian`.

Rules:

- Keep the client-visible `models[]` product-only. Include `fable5` and
  `anthropic.claude-fable-5`, but do not expose `claude-max-*` or
  `claude-max-my-random-*`.
- Product Fable aliases should point to the Fable group, not Opus:

  ```text
  fable5                   -> claude-max-my-random-fable-5
  anthropic.claude-fable-5 -> claude-max-my-random-fable-5
  ```

- Remove stale internal aliases such as
  `claude-max-my-random-fable-5 -> claude-max-my-random-opus-4-8`. That was a
  temporary workaround for an old Fable streaming issue and silently turns
  Fable requests into Opus requests.
- Keep a fallback for `claude-max-my-random-fable-5` to a stable Claude product
  model such as `anthropic.claude-opus-4-8` if the deployment needs a
  key-specific fallback list.
- After the 224 Astrill consolidation, names like
  `claude-max-my-random-opus-4-8` and `claude-max-my-random-fable-5` may all
  route to `acct-19` behind the scenes. Per-key aliases do not need to change
  as long as `/model/info` shows those groups pointing at the intended account.
- When reading local shell config to identify a key, ignore commented lines
  such as `#export ANTHROPIC_AUTH_TOKEN=...`; the active key is the last
  non-comment `export`. A commented old key can otherwise be misidentified as
  the current client key.

For aligning two keys:

1. Backup both key rows and `litellm-config`.
2. Read the source key's `models`, `aliases`, and `router_settings`.
3. Apply only those routing fields to the target key via `/key/update`.
   Preserve the target key token, budget, spend, owner, and other metadata.
4. Clean the Fable aliases as described above.
5. Flush Redis and verify from the target key.

Verification from the target key:

```text
GET /v1/models:
  contains fable5 and anthropic.claude-fable-5
  contains product Claude IDs
  does not contain any claude-max* internal IDs

POST /v1/messages:
  anthropic.claude-opus-4-8 -> HTTP 200
  fable5 -> HTTP 200
  anthropic.claude-fable-5 -> HTTP 200
```

For Fable probes, use `max_tokens >= 128`; very small probes can return only a
`thinking` block with no text even when the route is healthy.

### Safe Change Flow

1. Backup the live key row and `litellm-config` on 198:

   ```bash
   scripts/jms ssh AIYJY-litellm 'bash -s' <<'REMOTE'
   set -euo pipefail
   NS=litellm-product
   ALIAS='<key_alias>'
   TS=$(date +%Y%m%d-%H%M%S)
   BDIR="/root/litellm-product-manifests/backups/${ALIAS}-products-only-${TS}"
   mkdir -p "$BDIR"
   kubectl get cm litellm-config -n "$NS" -o yaml > "$BDIR/litellm-config.yaml"
   kubectl exec -i -n "$NS" litellm-db-0 -- bash -lc \
     'PGPASSWORD=$POSTGRES_PASSWORD psql -U $POSTGRES_USER $POSTGRES_DB' \
     > "$BDIR/key-row.tsv" <<SQL
   \pset pager off
   \pset format unaligned
   \pset fieldsep '\t'
   SELECT key_alias, token, blocked, models, aliases::text, router_settings::text
   FROM "LiteLLM_VerificationToken"
   WHERE key_alias='${ALIAS}';
   SQL
   echo "$BDIR"
   REMOTE
   ```

2. Generate a temporary key with product-only `models[]` and the intended
   aliases. Verify:
   - `GET /v1/models` returns only product IDs.
   - Minimal `/v1/messages` calls for Haiku, Sonnet, Opus 4.6/4.7/4.8 return
     200.
   - SpendLogs route to `claude-max-my-random-*`.

3. Update the real key through `/key/update`, not raw SQL. Include the full
   intended product model list, the alias map, and `router_settings={}` unless
   you are deliberately installing a key-specific router override. Then flush
   Redis:

   ```bash
   kubectl exec -n litellm-product litellm-redis-0 -- redis-cli FLUSHDB
   ```

4. Verify from the client key:

   ```bash
   curl -sS "$ANTHROPIC_BASE_URL/v1/models" \
     -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" | jq -r '.data[].id'
   ```

   No output line should start with `claude-max`.

5. Prove routing with a time-filtered SpendLogs query. Always quote the mixed
   case column name:

   ```sql
   SELECT sl."startTime", sl.model_group, sl.model, sl.api_base, sl.status
   FROM "LiteLLM_SpendLogs" sl
   JOIN "LiteLLM_VerificationToken" vt ON sl.api_key = vt.token
   WHERE vt.key_alias = '<key_alias>'
     AND sl."startTime" > now() - interval '15 minutes'
     AND (sl.model LIKE '%claude%' OR sl.model_group LIKE '%claude%')
   ORDER BY sl."startTime" DESC
   LIMIT 20;
   ```

Common pitfalls:

- Leaving `claude-max*` in `models[]` leaks internal routing groups to
  `/v1/models`.
- Removing aliases makes product model requests fall back directly to Wangsu.
- Leaving an old key-level `router_settings.fallbacks` makes global fallback
  look correct in `/app/config.yaml` but fail at runtime for that key.
- Direct SQL updates leave LiteLLM key cache stale; use `/key/update`.
- A full SpendLogs scan can hang; always filter by `"startTime"`.

### Fallback Debug And Repair

Use this when a key is mapped to `claude-max-my-random-*`, the CC Max upstream
returns 401/429/5xx, and Wangsu fallback does not happen.

Diagnosis discipline:

1. Hypothesis: an old key-level `router_settings.fallbacks` is overriding the
   global random fallback.
2. Falsification condition: the key row has `router_settings={}` and no team
   router override, yet the error still shows an old fallback list.
3. Data path: inspect the key row, optional team row, then SpendLogs and the
   HTTP error body.

Read-only check:

```bash
scripts/jms ssh AIYJY-litellm 'bash -s' <<'REMOTE'
set -euo pipefail
NS=litellm-product
ALIAS='<key_alias>'
kubectl exec -i -n "$NS" litellm-db-0 -- bash -lc \
  'PGPASSWORD=$POSTGRES_PASSWORD psql -U $POSTGRES_USER $POSTGRES_DB' <<SQL
\pset pager off
SELECT key_alias, blocked, models, aliases::text, router_settings::text, team_id
FROM "LiteLLM_VerificationToken"
WHERE key_alias='${ALIAS}';
SELECT t.team_id, t.team_alias, t.router_settings::text
FROM "LiteLLM_TeamTable" t
JOIN "LiteLLM_VerificationToken" vt ON vt.team_id=t.team_id
WHERE vt.key_alias='${ALIAS}';
SQL
REMOTE
```

If `router_settings` contains old `fallbacks`, clear only that field while
preserving `models[]` and `aliases`. Backup first, update via `/key/update`,
then flush Redis:

```bash
scripts/jms ssh AIYJY-litellm 'bash -s' <<'REMOTE'
set -euo pipefail
NS=litellm-product
ALIAS='<key_alias>'
TS=$(date +%Y%m%d-%H%M%S)
BDIR="/root/litellm-product-manifests/backups/${ALIAS}-clear-key-router-settings-${TS}"
mkdir -p "$BDIR"
MK=$(kubectl get secret litellm-secrets -n "$NS" -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
TMPDIR=$(mktemp -d); trap 'rm -rf "$TMPDIR"' EXIT
kubectl exec -i -n "$NS" litellm-db-0 -- bash -lc \
  'PGPASSWORD=$POSTGRES_PASSWORD psql -U $POSTGRES_USER $POSTGRES_DB' \
  > "$BDIR/current-key.tsv" <<SQL
\pset pager off
\pset format unaligned
\pset fieldsep '\t'
SELECT key_alias, token, blocked, models, aliases::text, router_settings::text
FROM "LiteLLM_VerificationToken"
WHERE key_alias='${ALIAS}';
SQL
python3 - "$BDIR/current-key.tsv" "$TMPDIR/payload.json" <<'PY'
import json, sys
rows=[l.rstrip("\n") for l in open(sys.argv[1]) if l.strip()
      and not l.startswith(("Pager usage","Output format","Field separator","key_alias\t","("))]
if not rows:
    raise SystemExit("key row not found")
parts=rows[0].split("\t")
token=parts[1]
models=[x.strip() for x in parts[3].strip("{}").split(",") if x.strip()]
aliases=json.loads(parts[4]) if parts[4] and parts[4] != "{}" else {}
payload={"key":token,"models":models,"aliases":aliases,"router_settings":{}}
open(sys.argv[2],"w").write(json.dumps(payload,separators=(",",":")))
PY
curl -sS -X POST http://localhost:30402/key/update \
  -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
  --data-binary "@$TMPDIR/payload.json" >/dev/null
kubectl exec -n "$NS" litellm-redis-0 -- redis-cli FLUSHDB >/dev/null
echo "$BDIR"
REMOTE
```

Verify with the real client key and then SpendLogs. A working fallback keeps
`model_group=claude-max-my-random-*` while `api_base` becomes Wangsu
`aigateway.edgecloudapp.com`.

### Emergency Wangsu Restore

Use this only when a user is blocked and cannot wait for CC Max token refresh
or fallback debugging. It restores a single key to direct Wangsu while keeping
the visible product models.

Target state:

```text
models[]      = existing product/non-Claude model allowlist
aliases       = {}
router_settings = {}
```

That makes requests for `anthropic.claude-*` use the matching global Wangsu
model entries directly. Always backup the key row first, update via
`/key/update`, flush Redis, and verify with a temporary key or the user's key.

## Verification

Verify bottom-up and keep secrets local:

```bash
BASE="https://cc.auto-link.com.cn/pro"
KEY="<LITELLM_VIRTUAL_KEY>"

curl -s "$BASE/v1/responses" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"anthropic.claude-sonnet-4-6","input":"say ok","stream":false}' | jq
```

Then run Codex:

```bash
codex --profile ccmax-sonnet "say ok"
codex --profile ccmax-opus "explain this repository layout"
```

Confirm the actual route in SpendLogs. A successful CC Max route has model group
`claude-max-*` or `claude-max-my-random-*` and a proxy/tunnel `api_base` for the
active backend, such as `10.68.13.198:3467` for the 224 Astrill path. A default
Wangsu route has an aigateway `api_base`.

## Quota And Failure Triage

Start with quota:

```bash
./scripts/anthropic-onboard/cc-max-upstream-status.sh
# add --watch 60 for live monitoring, --json for automation
```

This is an upstream Anthropic header probe, not a LiteLLM SpendLogs query. By
default it only reads 188 `/Data/anthropic-auth/acct-*/.env`; ACK isolated K8s
accounts are invisible unless explicitly exported to a controlled auth dir or
probed with a K8s-safe variant. Use SpendLogs to prove which user/key created
traffic, and use `cc-max-upstream-status.sh` to see account-level 5h/7d pressure.

Interpretation:

- `5h > 50%`: fallback mode may begin; watch growth rate.
- `5h > 80%`: avoid more high-context Codex sessions on that pool.
- `7d high`: expect sustained failures until reset or add capacity.
- Upstream utilization growing much faster than our SpendLogs implies another buyer/session is sharing the same Anthropic account.
- `HTTP 401/403` in the quota script means token invalid or wrong token file,
  not quota exhaustion.

Common Codex-specific symptoms:

| Symptom | First checks |
|---|---|
| Codex 401 for model | Key allowlist lacks the model or alias did not apply; inspect the key row and Redis cache. |
| Traffic still goes Wangsu | Wrong endpoint, no alias on that key, cache not flushed, or the request uses a model name not in the alias map. |
| `claude-max-*` failures with empty `api_base` | Alias rewrote correctly but route failed; check active proxy/tunnel health and quota before changing DB config. |
| Streaming stalls | Check LiteLLM Responses bridge and proxy logs; do not assume CC Max quota without data. |
| One heavy user fails while others pass | Compare that user's model mix and SpendLogs with upstream 5h/7d quota. Temporary revoke may be safer than global changes. |

## Pool Expansion For Codex Traffic

When Codex demand exceeds the current pool, switch to `anthropic-max-litellm`
for account onboarding and keep this skill focused on 198 key routing:

1. Choose the active backend deliberately: legacy 188 Docker pool, Malaysia
   `cc-proxy`, 10.68.13.224 Astrill, or an isolated ACK test pool.
2. Create or refresh `/Data/anthropic-auth/acct-N/.env` only on that runtime
   host. Choose the flow from the credential type; `session_key` is usually the
   fastest when available.
3. Add the new token to that host's `claude-max-proxy` runtime configuration
   without committing it. Restart only the relevant proxy/service.
4. Probe the account from the runtime host and confirm 198 can reach the exposed
   proxy/tunnel endpoint.
5. Ensure 198 LiteLLM has the intended `claude-max-*` or
   `claude-max-my-random-*` model entries, then grant or align Codex-facing keys
   and verify via Responses and SpendLogs.

Do not point high-volume CarHer bot traffic at this pool unless explicitly
approved; this pool is fragile, subscription-backed, and intended for selected
developer tooling such as Codex/Cursor.

If the user asks to validate a new account on Aliyun ACK before touching 198
prod, switch to `anthropic-max-litellm` and use its ACK isolated onboarding test
pool workflow. That workflow creates a one-account ClusterIP-only proxy and
manual probes first; it must not patch 198 prod LiteLLM or grant user keys until
the isolated Haiku/Sonnet/Opus checks pass.

If the user asks to add more Malaysia-egress or Astrill-egress CC Max accounts,
switch to `anthropic-max-litellm` and use the matching egress variant. Those
workflows keep one account per runtime port, bridge 198 with a restricted SSH
local tunnel when needed, and first register isolated 198 model groups such as
`claude-max-my-*` instead of replacing production `claude-max-*`.

## Completion Bar

The task is complete only when:

- Codex config points at `https://cc.auto-link.com.cn/pro` with `wire_api = "responses"`.
- The virtual key has either direct `claude-max-*` access or aliases from public `anthropic.claude-*` names to `claude-max-*`.
- A `/v1/responses` smoke test succeeds.
- SpendLogs prove the request routed through the active CC Max backend or intentionally stayed on Wangsu.
- Quota status is known, and rollback is either `--revoke` for the key or baseline-key realignment.
