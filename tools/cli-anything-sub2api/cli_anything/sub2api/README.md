# cli-anything-sub2api

Stateful CLI harness for operating a **Sub2API** deployment through its native
administrator HTTP API. The real backend is the deployed Go service at
`SUB2API_BASE_URL`; this harness never reimplements account or gateway behavior.

## Prerequisites

- A reachable Sub2API deployment (the Go backend; run it directly or with the
  included Docker Compose stack).
- An administrator credential, either an admin API key or an administrator JWT.
- Python 3.10+.

## Install

```bash
cd agent-harness
python3 -m pip install -e .
which cli-anything-sub2api
```

## Configure

Credentials are read from flags or environment variables and are never persisted:

```bash
export SUB2API_BASE_URL='https://your-sub2api-host'
export SUB2API_ADMIN_API_KEY='<admin-api-key>'
# Or, when the deployment uses an admin JWT:
# export SUB2API_JWT='<admin-access-token>'
```

For the 198 development deployment, use the known NodePort target. The admin
credential is still required and is never persisted:

```bash
export SUB2API_ADMIN_API_KEY='<198-admin-api-key>'
cli-anything-sub2api --target 198 --json status
cli-anything-sub2api --target 198 --json accounts list --platform grok
```

Set `SUB2API_198_BASE_URL` if the 198 address changes.

You may also save only the base URL in a local profile:

```bash
cli-anything-sub2api profile set https://your-sub2api-host
cli-anything-sub2api profile show
```

## Basic Usage

```bash
# Enter the interactive shell
cli-anything-sub2api

# Inspect before mutating
cli-anything-sub2api status
cli-anything-sub2api accounts list --status active --page-size 20
cli-anything-sub2api accounts get 40
cli-anything-sub2api users list --search user@example.com
cli-anything-sub2api groups all
cli-anything-sub2api proxies all
cli-anything-sub2api dashboard stats

# Mutate with native JSON payloads; --dry-run shows the request without sending it
cli-anything-sub2api --dry-run accounts update 40 --data '{"concurrency":10}'
cli-anything-sub2api accounts bulk-update --ids 40,39 --data '{"concurrency":10}'
cli-anything-sub2api redeem-codes generate --data '{"count":1,"type":"balance","value":10}' --idempotency-key redeem-$(date +%s)

# Every command emits machine-readable JSON with --json
cli-anything-sub2api --json accounts list --status error
```

## Grok OAuth

Inspect the server capability first:

```bash
cli-anything-sub2api --target 198 --json grok oauth capabilities
```

If the operator explicitly enables the one-shot password authorization flow,
pass the password only through stdin. Never put it in argv or a JSON file:

```bash
printf '%s\n' "$GROK_PASSWORD" | \
  cli-anything-sub2api --target 198 grok import-password \
    --email 'account@example.com' --password-stdin
```

Only returned Grok OAuth credentials are stored; the password and raw SSO
cookie are not persisted. Missing YesCaptcha configuration or a disabled server
flow is surfaced as an error without bypassing it.

## Sensitive Exports

`accounts export`, `proxies export`, and sensitive raw `api` routes can return
upstream credentials. They require an explicit local output file and never print
the secret contents to the terminal:

```bash
cli-anything-sub2api accounts export --ids 40,39 --output-file accounts-export.json
cli-anything-sub2api proxies export --output-file proxies-export.json
```

## Raw Admin API

Newly added or uncovered routes remain available through the escape hatch. Paths
may be written as `/admin/...` or `/api/v1/admin/...`:

```bash
cli-anything-sub2api api GET /admin/groups/all
cli-anything-sub2api api POST /admin/accounts/bulk-update --data '{"account_ids":[40],"concurrency":10}'
```

## Tests

```bash
cd agent-harness
python3 -m pytest cli_anything/sub2api/tests/ -v -s
CLI_ANYTHING_FORCE_INSTALLED=1 python3 -m pytest cli_anything/sub2api/tests/ -v -s
```

The E2E suite boots a local HTTP server implementing Sub2API's native response
envelope and validates real HTTP requests and CLI subprocess behavior.
