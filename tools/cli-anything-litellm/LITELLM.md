# LiteLLM Agent Harness

## Backend analysis

LiteLLM already exposes two production command-line surfaces:

- `litellm` starts and configures the proxy server
- `lite` manages a running proxy, including models, keys, teams, users, authentication, HTTP requests, and coding-agent routing

The harness does not reimplement those surfaces. It provides a stable agent-facing layer over the installed commands and the proxy's OpenAI-compatible HTTP API.

### 1.1.0: the 198 deployment as a second backend

For our production (198, ns `litellm-product`) "LiteLLM" is not the PyPI package: it is a
fork tag **plus four patch layers** (callbacks, `sitecustomize.py`, whole-file overlays,
patches baked into the image) **plus a DB that owns most of the routing config**. Reading
any one of these alone gives a wrong picture, so 1.1.0 adds read-only commands that go to
the real thing over ssh:

| Real backend | Reached through | Used by |
|---|---|---|
| upstream source of the live version | `git -C /root/litellm-fork show/grep <tag>` (no checkout) | `src` |
| the running interpreter of a lane | `kubectl exec -i <pod> -- python -` with a probe on stdin | `patches` |
| production Postgres | `psql` in `litellm-db-0`, `default_transaction_read_only=on`, `statement_timeout` | `db` |

Nothing is mirrored locally: no cached schema, no copy of the callbacks, no shadow DB.

## Command design

- `backend status`: discover the installed LiteLLM package and native executables
- `profile show|set|reset`: persist non-secret defaults such as proxy URL and model
- `config inspect`: parse a proxy YAML file, summarize deployments, and redact secret-like fields
- `proxy health|models|chat`: inspect and call a real LiteLLM proxy with JSON-first output
- `native server`: pass arguments to the upstream `litellm` executable without invoking a shell
- `native lite`: pass arguments to the optional newer `lite` executable when the installed version provides it
- default REPL: expose the same commands interactively
- `src path`: the request path (endpoint → auth → pre-call → route → fallback/retry → pick
  deployment → cooldown → provider → stream out → spend) with the patches that hook each step
- `src find SYMBOL` / `src show PATH:LINE`: read the tag that matches a lane's live version;
  an explicit `--lane` with a different `--tag` is refused (exit 1)
- `patches targets --lane L`: for 27 upstream symbols we patch, which callback replaced each,
  measured by object identity in the live interpreter, with the wrapper stack where several
  patches stack on one symbol
- `patches compare`: the same across prod / gray / guarded-old, differences only
- `patches purpose [NAME]`: one-line purpose catalog (documentation, not a ruler)
- `db anchor`: structural counts proving this is the prod DB (dev has same-named objects)
- `db catalog`: the tag's `schema.prisma` vs the live DB (missing tables, non-schema tables,
  largest tables, migrations)
- `db config-owner`: per router/general setting, whether config.yaml or the DB row owns it
- `db spend-reconcile DATE [--model-group G]`: per key, DailyUserSpend requests vs SpendLogs rows
- `db retention`: how far back SpendLogs and the daily tables reach

## What reading the 1.105.1 source established (2026-10-20)

- **Settings precedence** (`proxy/config_resolvers/settings_store.py`): runtime > config.yaml > DB
  row in `LiteLLM_Config`. A key in config.yaml shadows the DB value completely. On prod,
  `fallbacks` (129), `model_group_alias` (24), `num_retries`, `timeout`, `routing_strategy`,
  `model_group_retry_policy` are **DB-owned**; `cooldown_time`, `allowed_fails`, redis, pre-call
  checks are config-owned. Editing those DB-owned keys in YAML would silently take them over.
- **Deployment selection order** (`router.py:async_get_healthy_deployments`): team filter →
  web-search filter → health-check filter → cooldown filter → blocked → `async_filter_deployments`
  callbacks (weighted_affinity, encrypted-content checks) → `_pre_call_checks` → tags → order →
  strategy. So affinity pins only ever see non-cooled-down deployments.
- **Retry vs cooldown**: `should_retry_this_error` raises immediately for non-retryable status
  codes except 401/403; `_is_cooldown_required` cools down 401/404/408/429 and all non-4xx. A 400
  is neither retried nor cooled — the gap `dead_deployment_retry` fills for the dead-leaf 400.
  `_is_allowed_fails_set_on_router` returns False when `allowed_fails == litellm.allowed_fails`
  (both 3), so the percentage branch is what runs.
- **Spend write path**: callback → `update_database` (queue SpendLogs row + daily transactions
  in one task) → `update_spend_logs` flush every 10s via `create_many`; a prisma `DataError`
  bisects the batch and drops only the bad row (`_create_spend_logs_with_poison_isolation`).
  The daily tables are written independently, so **a dropped row loses detail, not billing**.
- **Patch stacks**: `_process_chunk` = upstream ← codex_compaction_v2 ← streaming_output_backfill
  ← deepseek_id_prefix; `jsonify_object` = upstream ← sitecustomize ← null_byte_sanitize.
  Each layer keeps the previous `_orig`; the order follows the callbacks list.

## State and security

The session contains only a base URL and default model. API keys are accepted through `--api-key` or `LITELLM_PROXY_API_KEY`, are never serialized, and are redacted from displayed configuration. Session writes use an exclusive lock and atomic replacement. Mutations auto-save unless `--dry-run` is supplied.

The 198 commands store nothing. They use the operator's ssh key and `sudo -n` on 198; no
password is read or passed. DB access runs in a read-only transaction (a write is refused by
Postgres — covered by a test), one session per call (the DB is shared, `max_connections=100`),
and every user value that reaches SQL passes a strict allow-list. `master_key` / `database_url`
values are never printed.

## Backend truthfulness

All model discovery and inference commands call the real proxy HTTP API. Native passthrough invokes the actual installed `lite` or `litellm` executable. Configuration inspection parses the same YAML consumed by the proxy but does not claim that static validation proves provider credentials or connectivity.

`patches targets` decides "patched" by object identity after each import, never by
`__module__` (functools.wraps copies it) and never by import success (a fingerprint guard can
refuse silently). Symbols wrapped at interpreter start by `sitecustomize` are reported only when
a sentinel attribute or `__wrapped__` proves it. The `PURPOSE` catalog is prose and is not used
to decide anything.
