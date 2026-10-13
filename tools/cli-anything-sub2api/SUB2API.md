# Sub2API CLI-Anything Analysis

## Target

`sub2api` is an AI API gateway platform. Its presentation layer is a Vue
administrator console, while its operational backend is the Go HTTP service in
`backend/`. The CLI harness is intentionally an API client for the deployed
service, rather than a replacement for either the Vue console or the Go gateway.

## Real Backend Contract

- **Backend engine:** Go/Gin server registered in
  `backend/internal/server/router.go`.
- **Administrator API root:** `/api/v1/admin`, registered in
  `backend/internal/server/routes/admin.go`.
- **Response format:** `{"code": 0, "message": "success", "data": ...}`
  from `backend/internal/pkg/response/response.go`; the harness unwraps only
  successful envelopes and surfaces non-zero codes as errors.
- **Authentication:** `x-api-key` for `SUB2API_ADMIN_API_KEY`, falling back to
  `Authorization: Bearer <jwt>` for `SUB2API_JWT`, consistent with the existing
  `skills/sub2api-admin` implementation.
- **Health probe:** unauthenticated `GET /health`; the CLI follows it with
  `GET /api/v1/admin/system/version` when credentials are present.
- **198 deployment target:** `--target 198` or `SUB2API_TARGET=198` resolves to
  the 198 dev NodePort; `SUB2API_198_BASE_URL` overrides the address.

## Console-to-CLI Mapping

| Console area | Native API routes | CLI surface |
| --- | --- | --- |
| Upstream accounts | `/admin/accounts` and diagnostics/actions | `accounts` |
| Panel users | `/admin/users` | `users` |
| Consumer groups | `/admin/groups` | `groups` |
| Upstream proxies | `/admin/proxies` | `proxies` |
| Redemption codes | `/admin/redeem-codes` | `redeem-codes` |
| Dashboard | `/admin/dashboard/*` | `dashboard` |
| Settings | `/admin/settings` | `settings` |
| Operations | `/admin/ops/*` | `ops` |
| Newly added admin APIs | any `/admin/...` route | `api METHOD PATH` |

## State Model

Sub2API owns all durable state (PostgreSQL, Redis, and upstream account
credentials). The CLI stores no project model and does not maintain an offline
shadow database. The optional profile is only a locked JSON document containing
the base URL. API keys and JWTs are accepted from process flags/environment only
and are never written to this file or printed in command output.

## Mutation and Safety Model

- Dedicated mutations use the deployed backend's native request schemas.
- `--dry-run` prevents mutation HTTP requests and returns the planned method,
  path, and payload instead.
- Single-record deletes require an interactive confirmation.
- Credential-bearing account and proxy exports require an explicit
  `--output-file`; results are written locally without printing secrets.
- The generic `api` command allows documented admin endpoints but blocks
  foreign absolute URLs and requires an output file for known sensitive exports.
- Grok password authorization is opt-in and stdin-only. The CLI never accepts a
  password as an argument, prints it, or persists password/SSO fields.

## Out of Scope

There is no visual render pipeline or native project-file format to manipulate,
so preview bundles, rendering, undo/redo, and project auto-save do not apply.
The harness uses the live deployed Sub2API API as its source of truth.
