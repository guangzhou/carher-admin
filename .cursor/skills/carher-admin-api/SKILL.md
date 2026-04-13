# CarHer Admin API Skill

Use this skill when managing CarHer instances, checking cluster status, deploying images, or performing any operations on the CarHer K8s platform.

## API Base URL

```
https://admin.carher.net/api
```

In-cluster (from K8s pods): `http://carher-admin-svc.carher:8900/api`

For local dev: `http://localhost:8900/api`

## OpenAPI Schema

The full schema is available at: `GET /openapi.json`

## Authentication

All `/api/*` endpoints require authentication (except `/api/auth/login` and
`/api/deploy/webhook`). Two methods are supported:

**Method 1: API Key (recommended for automation / in-cluster calls)**

```bash
curl -s https://admin.carher.net/api/status \
  -H "X-API-Key: $ADMIN_API_KEY"
```

The API key is the value of the `ADMIN_API_KEY` environment variable on the
carher-admin deployment. For Her instances calling the API from within the
cluster, pass this key via `X-API-Key` header on every request. No login
flow needed. Also accepted as query param `?api_key=...`.

**Method 2: JWT Token (for Web UI / interactive sessions)**

```bash
# Login (returns JWT token, valid 24h)
curl -X POST https://admin.carher.net/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"xxx"}'

# Use token in subsequent requests
curl -s https://admin.carher.net/api/status \
  -H "Authorization: Bearer <token>"

# Check auth status
curl -s https://admin.carher.net/api/auth/me \
  -H "Authorization: Bearer <token>"
```

`/api/auth/login` returns `503` when `ADMIN_PASSWORD` or `JWT_SECRET` is
intentionally left unconfigured.

## Quick Reference

### Instance Management

```bash
# List all instances (supports pagination)
# limit=0 returns all (max 5000)
curl -s "https://admin.carher.net/api/instances?offset=0&limit=200" | jq

# Search instances (all filters are AND-combined)
# Filters: status (Running/Stopped/Failed/Paused), model (gpt/sonnet/opus/gemini/minimax/glm/codex),
#   deploy_group, owner (contains), name (contains), feishu_ws (Connected/Disconnected)
# Pagination: offset, limit (default 200, max 5000)
curl -s "https://admin.carher.net/api/instances/search?status=Running&model=gpt&feishu_ws=Connected&limit=50" | jq

# Get instance detail (includes CRD status, PVC, feishu_ws, config_hash, image, paused, etc.)
curl -s https://admin.carher.net/api/instances/14 | jq

# Get next available ID (considers both DB and CRD instances)
curl -s https://admin.carher.net/api/next-id | jq

# Create instance
# Required: name, app_id, app_secret
# Optional: id (auto-assigned if omitted), model (default: gpt),
#   provider (default: wangsu), prefix (default: s1), owner (pipe-separated open_ids),
#   deploy_group (default: stable), litellm_route_policy (legacy, no longer affects routing)
curl -X POST https://admin.carher.net/api/instances \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"name":"新用户","model":"gpt","provider":"litellm","app_id":"cli_xxx","app_secret":"xxx","prefix":"s1","owner":"ou_xxx","deploy_group":"stable"}'
# Returns: {"id":N,"status":"created","managed_by":"operator","oauth_url":"https://...","cloudflare":{"ok":true}}
# If CLOUDFLARE_API_TOKEN is missing, create fails with HTTP 503.

# Update instance (only non-null fields are applied)
# Supported fields: name, model, provider, owner, deploy_group, image,
#   app_id, app_secret, prefix, bot_open_id, litellm_route_policy
#
# Provider → Model mapping:
#   openrouter: gpt (GPT-5.4), sonnet (Claude Sonnet 4.6), opus (Claude Opus 4.6), gemini (Gemini 3.1 Pro)
#   anthropic:  sonnet (Claude Sonnet 4.6), opus (Claude Opus 4.6)
#   wangsu:     gpt (GPT-5.4), sonnet (Claude Sonnet 4.6), opus (Claude Opus 4.6), gemini (Gemini 3.1 Pro)
#   litellm:    gpt (GPT-5.4), sonnet (Claude Sonnet 4.6), opus (Claude Opus 4.6),
#               gemini (Gemini 3.1 Pro), minimax (MiniMax M2.7),
#               glm (GLM-5), codex (GPT-5.3 Codex)
#
# litellm_route_policy: legacy field, no longer affects routing.
#   Routing is fixed: Sonnet/Opus → Wangsu Direct, GPT/Gemini → OpenRouter.
# When provider=litellm, a per-instance virtual key is auto-generated for spend tracking.
curl -X PUT https://admin.carher.net/api/instances/14 \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","provider":"litellm","deploy_group":"vip"}'

# Lifecycle
curl -X POST https://admin.carher.net/api/instances/14/stop       # CRD: paused=true
curl -X POST https://admin.carher.net/api/instances/14/start      # CRD: paused=false
curl -X POST https://admin.carher.net/api/instances/14/restart    # Deletes Pod, Operator recreates
curl -X DELETE "https://admin.carher.net/api/instances/14?purge=false"  # purge=true also deletes PVC

# Batch operations (actions: stop, start, restart, delete, update)
curl -X POST https://admin.carher.net/api/instances/batch \
  -H "Content-Type: application/json" \
  -d '{"ids":[14,25,30],"action":"restart"}'

# Batch update (action=update with params — same fields as PUT /api/instances/{uid})
curl -X POST https://admin.carher.net/api/instances/batch \
  -H "Content-Type: application/json" \
  -d '{"ids":[14,25,30],"action":"update","params":{"provider":"litellm","model":"gpt"}}'

# Batch import instances (preferred wrapped body)
curl -X POST https://admin.carher.net/api/instances/batch-import \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"instances":[{"name":"用户A","model":"gpt","provider":"litellm","app_id":"cli_xxx","app_secret":"xxx","prefix":"s1","owner":"ou_xxx"}]}'
# Legacy raw-array bodies are also accepted for backward compatibility.

# Verify create response
curl -s -X POST https://admin.carher.net/api/instances/batch-import \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"instances":[{"name":"用户A","model":"gpt","provider":"litellm","app_id":"cli_xxx","app_secret":"xxx","prefix":"s1","owner":"ou_xxx"}]}' \
  | jq '.results[] | {id,status,oauth_url,cloudflare}'

# Live callback verification: normal result is HTTP 400, not 404
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://s1-u180-auth.carher.net/feishu/oauth/callback?code=test&state=test"

# Get Pod logs (default tail=200)
curl -s "https://admin.carher.net/api/instances/14/logs?tail=200" | jq .logs

# Get K8s events (default limit=20)
curl -s "https://admin.carher.net/api/instances/14/events?limit=20" | jq

# Preview config (what openclaw.json would look like — secrets redacted)
curl -s https://admin.carher.net/api/instances/14/config-preview | jq

# Current applied config (from ConfigMap)
curl -s https://admin.carher.net/api/instances/14/config-current | jq

# Execute command in Pod (debugging — whitelisted commands only)
# Allowed prefixes: ls, cat, head, tail, grep, wc, df, du, ps, uptime, env, echo,
#   test, stat, find, node --version, npm --version, openclaw
curl -X POST https://admin.carher.net/api/instances/14/exec \
  -H "Content-Type: application/json" \
  -d '{"command":"ls -la /data/.openclaw/skills/"}'

# Instance metrics (real-time CPU/memory)
curl -s https://admin.carher.net/api/instances/14/metrics | jq

# Instance metrics history (default 24h, range 1-168h / 7 days)
curl -s "https://admin.carher.net/api/instances/14/metrics/history?hours=24" | jq
```

### Deploy Groups

```bash
# List all groups (with instance counts, ordered by priority)
curl -s https://admin.carher.net/api/deploy-groups | jq

# Create custom group (lower priority = deployed first)
curl -X POST https://admin.carher.net/api/deploy-groups \
  -H "Content-Type: application/json" \
  -d '{"name":"vip","priority":5,"description":"VIP users, deployed first"}'

# Update group
curl -X PUT https://admin.carher.net/api/deploy-groups/vip \
  -H "Content-Type: application/json" \
  -d '{"priority":10,"description":"Updated"}'

# Delete group (instances moved to stable)
curl -X DELETE https://admin.carher.net/api/deploy-groups/vip

# Move instance to group (syncs both CRD and DB)
curl -X PUT https://admin.carher.net/api/instances/14/deploy-group \
  -H "Content-Type: application/json" \
  -d '{"group":"vip"}'

# Batch move instances
curl -X POST https://admin.carher.net/api/instances/batch-deploy-group \
  -H "Content-Type: application/json" \
  -d '{"ids":[14,25,30],"group":"canary"}'
```

### Deployment Pipeline

```bash
# Start deploy
# Modes: normal (canary→early→stable), fast (all at once in batches of 50),
#   canary-only (first wave only), group:<name> (single group)
# force=true to re-deploy same tag
curl -X POST https://admin.carher.net/api/deploy \
  -H "Content-Type: application/json" \
  -d '{"image_tag":"v20260329-abc1234","mode":"normal","force":false}'

# Webhook (called by GitHub Actions CI — auth exempt, uses secret field)
curl -X POST https://admin.carher.net/api/deploy/webhook \
  -H "Content-Type: application/json" \
  -d '{"image_tag":"v20260329-abc1234","secret":"xxx","branch":"main","commit_sha":"abc1234"}'

# Check deploy status
curl -s https://admin.carher.net/api/deploy/status | jq

# Continue paused deploy (after canary wave health check)
curl -X POST https://admin.carher.net/api/deploy/continue

# Rollback to previous image
curl -X POST https://admin.carher.net/api/deploy/rollback

# Abort current deploy
curl -X POST https://admin.carher.net/api/deploy/abort

# Deploy history
curl -s "https://admin.carher.net/api/deploy/history?limit=20" | jq

# List available image tags (from ACR sync + deploy history + active instances)
curl -s "https://admin.carher.net/api/image-tags?limit=30" | jq

# Sync image tags from Alibaba Cloud ACR (her/carher repo)
curl -X POST https://admin.carher.net/api/image-tags/sync | jq
```

### CI/CD & Branch Rules

```bash
# List branch rules
curl -s https://admin.carher.net/api/branch-rules | jq

# Create branch rule (supports glob: main, hotfix/*, feature/*)
curl -X POST https://admin.carher.net/api/branch-rules \
  -H "Content-Type: application/json" \
  -d '{"pattern":"release/*","auto_deploy":false,"deploy_mode":"canary-only"}'
# Legacy request key `branch_pattern` is also accepted.

# Update branch rule
curl -X PUT https://admin.carher.net/api/branch-rules/1 \
  -H "Content-Type: application/json" \
  -d '{"auto_deploy":true}'

# Delete branch rule
curl -X DELETE https://admin.carher.net/api/branch-rules/1

# Test branch rule matching (branch is a query parameter)
curl -X POST "https://admin.carher.net/api/branch-rules/test?branch=release/v2.0"

# Trigger GitHub Actions build
# Tip: call /api/ci/workflows first to discover the exact workflow file name.
curl -X POST https://admin.carher.net/api/ci/trigger-build \
  -H "Content-Type: application/json" \
  -d '{"repo":"guangzhou/CarHer","branch":"main","workflow":"<workflow-file>.yml","deploy_mode":"normal"}'

# List CI workflows (checks which have workflow_dispatch)
curl -s https://admin.carher.net/api/ci/workflows | jq

# List branches
curl -s https://admin.carher.net/api/ci/branches | jq

# List recent CI runs (default repo from settings, per_page 1-30)
curl -s "https://admin.carher.net/api/ci/runs?per_page=10" | jq
```

### Monitoring & Metrics

```bash
# Cluster status (pod counts, nodes, tunnel status)
curl -s https://admin.carher.net/api/status | jq

# Aggregated statistics (model/provider/prefix/group distributions, wave order, current image)
curl -s https://admin.carher.net/api/stats | jq

# Health check (all non-paused instances — feishu_ws status from CRD)
curl -s https://admin.carher.net/api/health | jq

# Metrics: cluster overview (nodes CPU/mem, Her totals, PVC)
curl -s https://admin.carher.net/api/metrics/overview | jq

# Metrics: per-node CPU/memory
curl -s https://admin.carher.net/api/metrics/nodes | jq

# Metrics: node history (default 24h, range 1-168h)
curl -s "https://admin.carher.net/api/metrics/history/nodes?hours=24" | jq

# Metrics: all Her pods CPU/memory
curl -s https://admin.carher.net/api/metrics/pods | jq

# Metrics: PVC storage status
curl -s https://admin.carher.net/api/metrics/storage | jq

# knownBots registry (app_id→name, botOpenId→appId mappings)
curl -s https://admin.carher.net/api/known-bots | jq

# Audit log (optional instance_id filter)
curl -s "https://admin.carher.net/api/audit?limit=20&instance_id=14" | jq
```

### System Administration

```bash
# Force ConfigMap sync (all non-CRD instances)
curl -X POST https://admin.carher.net/api/sync/force | jq

# DB → K8s consistency check
curl -s https://admin.carher.net/api/sync/check | jq

# Trigger SQLite backup to NAS
curl -X POST https://admin.carher.net/api/backup | jq

# Import instances from K8s ConfigMaps (one-time migration)
curl -X POST https://admin.carher.net/api/import-from-k8s | jq

# Reconcile cloudflared ConfigMap + remote tunnel ingress
# Requires CLOUDFLARE_API_TOKEN to be configured on carher-admin.
curl -X POST https://admin.carher.net/api/cloudflare/sync | jq

# Get settings (secrets are masked)
curl -s https://admin.carher.net/api/settings | jq

# Update settings (only send keys you want to change)
# Valid keys: github_token, github_repos, webhook_secret, feishu_webhook,
#   agent_api_key, acr_registry, acr_username, acr_password
curl -X PUT https://admin.carher.net/api/settings \
  -H "Content-Type: application/json" \
  -d '{"webhook_secret":"new_secret_value"}'

# Get configured GitHub repos
curl -s https://admin.carher.net/api/settings/repos | jq
```

### LiteLLM Key Management

```bash
# Generate a virtual key for a specific instance (idempotent: returns existing key if present)
curl -X POST "https://admin.carher.net/api/litellm/keys/generate?uid=1000" \
  -H "X-API-Key: $API_KEY"

# Batch-generate keys for ALL litellm-provider instances that don't have one yet
curl -X POST https://admin.carher.net/api/litellm/keys/generate-batch \
  -H "X-API-Key: $API_KEY"

# Get per-instance spend summary (token usage & cost from LiteLLM proxy)
curl -s https://admin.carher.net/api/litellm/spend \
  -H "X-API-Key: $API_KEY" | jq
```

### CRD Direct Query

```bash
# List all HerInstance CRDs (spec + status from K8s etcd)
curl -s https://admin.carher.net/api/crd/instances | jq

# Get single CRD (spec + status + metadata)
curl -s https://admin.carher.net/api/crd/instances/14 | jq
```

### AI Agent (Natural Language)

```bash
# Ask the agent anything (Chinese or English)
curl -X POST https://admin.carher.net/api/agent \
  -H "Content-Type: application/json" \
  -d '{"message":"当前有多少实例在运行？飞书断连的有哪些？"}'

# Dry run (explain what would happen, don't execute)
curl -X POST https://admin.carher.net/api/agent \
  -H "Content-Type: application/json" \
  -d '{"message":"重启所有飞书断连的实例","dry_run":true}'

# Agent capabilities
curl -s https://admin.carher.net/api/agent/capabilities | jq
```

## Environment Variables

### Admin Backend

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ADMIN_API_KEY` | Yes | | API key for `X-API-Key` auth |
| `ADMIN_PASSWORD` | No | | Login password for JWT auth |
| `ADMIN_USERNAME` | No | `admin` | Login username |
| `JWT_SECRET` | No | `ADMIN_API_KEY` | JWT signing key |
| `CORS_ALLOW_ORIGINS` | No | `https://admin.carher.net` | Comma-separated |
| `CARHER_ADMIN_DB_DIR` | No | `/data/carher-admin` | SQLite DB directory |
| `CARHER_ADMIN_BACKUP_DIR` | No | `/nas-backup/carher-admin` | Backup directory |
| `CLOUDFLARE_API_TOKEN` | Yes (for create) | | Cloudflare API token |
| `LITELLM_MASTER_KEY` | No | | LiteLLM admin key |
| `LITELLM_PROXY_URL` | No | `http://litellm-proxy.carher.svc:4000` | LiteLLM proxy |
| `FEISHU_DEPLOY_WEBHOOK` | No | | Feishu webhook for deploy notifications |

### Deployer

| Variable | Default | Description |
|----------|---------|-------------|
| `DEPLOY_BATCH_SIZE` | `50` | Instances per batch |
| `DEPLOY_HEALTH_WAIT_CANARY` | `30` | Seconds to wait after canary wave |
| `DEPLOY_HEALTH_WAIT` | `15` | Seconds to wait between waves |
| `DEPLOY_USE_CRD` | `true` | Use CRD path for deploy |

### AI Agent

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AGENT_LLM_API_KEY` | Yes (for agent) | | LLM API key |
| `AGENT_LLM_BASE_URL` | No | `https://openrouter.ai/api/v1` | LLM base URL |
| `AGENT_MODEL` | No | `openai/gpt-4o` | LLM model name |
