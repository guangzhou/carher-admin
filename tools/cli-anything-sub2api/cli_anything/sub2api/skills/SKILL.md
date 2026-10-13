---
name: cli-anything-sub2api
description: Operate a Sub2API AI API gateway deployment through its native administrator HTTP API. Use when the user mentions Sub2API, sub2api admin, upstream account operations, redeem code generation, group or proxy management, dashboard metrics, or raw Sub2API admin API calls.
---

# cli-anything-sub2api

The CLI talks to the real deployed Sub2API Go backend. Install it with
`pip install -e agent-harness` from the Sub2API source tree, or run it from the
repository with `PYTHONPATH=agent-harness python3 -m
cli_anything.sub2api.sub2api_cli`.

## Configuration

```bash
export SUB2API_BASE_URL='https://your-sub2api-host'
export SUB2API_ADMIN_API_KEY='<admin-api-key>'
# Or, for administrator JWT deployments:
# export SUB2API_JWT='<admin-access-token>'
```

198 shortcut (the deployed dev NodePort):

```bash
export SUB2API_ADMIN_API_KEY='<198-admin-api-key>'
cli-anything-sub2api --target 198 --json status
```

The default target URL is `http://10.68.13.198:31880`; override it with
`SUB2API_198_BASE_URL` if needed. `--target 198-dev` is an alias.

The admin API key is sent as `x-api-key`; when absent, the JWT is sent as
`Authorization: Bearer`. Credentials are never written to disk. `profile set`
stores only the base URL.

## Command Groups

- `accounts`: list/get/create/update/delete, usage/stats/today-stats/models,
  schedulable/status/action, bulk-update, and sensitive export.
- `users`: list/get/create/update/delete panel users; `balance` changes the
  internal ledger via the dedicated balance route.
- `audit-logs list`: who called which admin route, and when.
- `groups`: list/all/get/create/update/delete, with platform and inactive filters.
- `proxies`: list/all/get/create/update/delete and sensitive export.
- `redeem-codes`: list/get/stats/generate/expire.
- `dashboard`: stats and snapshot-v2 metrics.
- `settings`: get and partial update of administrator settings.
- `ops`: concurrency, realtime traffic, and ingress rejections.
- `api`: raw native admin endpoint escape hatch.
- `profile`: persist only the non-secret base URL.
- `status`: verify native health and deployed version.
- `grok oauth capabilities`: inspect Grok OAuth server capability flags.
- `grok import-password`: one-shot Grok password-to-OAuth import; password is
  accepted only from stdin and never persisted.

## Agent Guidance

- Always use `--json` for machine-readable output and parse the printed JSON.
- Run read-only introspection (`accounts list`, `accounts get`, `groups all`)
  before a write.
- Use `--dry-run` to preview a mutating request; it prints `dry_run: true` and
  sends no network traffic.
- Sensitive exports (`accounts export`, `proxies export`, and raw `api` calls to
  `/accounts/data` or `/proxies/data`) require `--output-file` and never print
  credential contents.
- Pass both `/admin/...` and `/api/v1/admin/...` paths to `api`; the CLI
  normalizes them to the `/api/v1/admin/...` contract.
- `INVALID_ADMIN_KEY` means the credential is missing or stale; regenerate the
  admin API key or refresh the administrator JWT.
- API payload schemas follow the backend's native fields; use `accounts get` or
  the frontend admin types before constructing partial updates.
- For 198, run `status`, then read-only account/group discovery before writes.
  In the carher-admin repo prefer `scripts/sub2api-cli.sh`, which logs in with
  the admin password and exports a short-lived JWT (see skill `cli-anything-sub2api`).
- Some writes return 200 but change nothing: `accounts update` ignores
  `schedulable` (use `set-schedulable` or `bulk-update`), `users update` ignores
  `balance` (use `users balance`), and only `accounts action <id> clear-error`
  clears a temporary park. Verify every write with a follow-up read.
- `accounts delete` is a soft delete; the row keeps `deleted_at`.
- Grok password import requires the server's explicit password-auth switch and
  any upstream YesCaptcha prerequisite. Do not bypass either requirement.
- Never pass a Grok password in argv, `--data`, or a file. Use
  `printf '%s\n' "$GROK_PASSWORD" | cli-anything-sub2api --target 198 grok import-password --email ... --password-stdin`.

## Examples

```bash
cli-anything-sub2api --json status
cli-anything-sub2api --target 198 --json accounts list --platform grok
cli-anything-sub2api --target 198 --json grok oauth capabilities
printf '%s\n' "$GROK_PASSWORD" | cli-anything-sub2api --target 198 grok import-password --email account@example.com --password-stdin
cli-anything-sub2api --json accounts list --status error --page-size 20
cli-anything-sub2api --json accounts get 40
cli-anything-sub2api --dry-run users balance 1 add 1000 --notes topup
cli-anything-sub2api --json audit-logs list --page-size 20
cli-anything-sub2api --dry-run accounts update 40 --data '{"concurrency":10}'
cli-anything-sub2api accounts bulk-update --ids 40,39 --data '{"priority":2}'
cli-anything-sub2api --json groups all --include-inactive
cli-anything-sub2api --json redeem-codes list --status unused
cli-anything-sub2api --json dashboard stats
cli-anything-sub2api accounts export --ids 40,39 --output-file accounts-export.json
cli-anything-sub2api --json api GET /admin/ops/request-errors --param status=open
```
