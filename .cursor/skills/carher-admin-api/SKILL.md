# CarHer Admin API Skill

Use this skill when managing CarHer instances, checking cluster status, deploying images, or performing any operations on the CarHer K8s platform.

## API Base URL

```
https://admin.carher.net/api
```

For local dev: `http://localhost:8900/api`

## OpenAPI Schema

The full schema is available at: `GET /openapi.json`

## Authentication

```bash
# Login (returns JWT token)
curl -X POST https://admin.carher.net/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"xxx"}'

# Check auth status
curl -s https://admin.carher.net/api/auth/me \
  -H "Authorization: Bearer <token>"
```

Most API endpoints also accept `X-API-Key` header with the admin API key.
For automation, prefer `X-API-Key`. `/api/auth/login` returns `503` when
`ADMIN_PASSWORD` or `JWT_SECRET` is intentionally left unconfigured.

## Quick Reference

### Instance Management

```bash
# List all instances (supports pagination)
curl -s "https://admin.carher.net/api/instances?offset=0&limit=200" | jq

# Search instances (all filters are AND-combined)
# Filters: status, model, deploy_group, owner, name, feishu_ws
# Pagination: offset, limit
curl -s "https://admin.carher.net/api/instances/search?status=Running&model=gpt&feishu_ws=Connected&limit=50" | jq

# Get instance detail
curl -s https://admin.carher.net/api/instances/14 | jq

# Get next available ID
curl -s https://admin.carher.net/api/next-id | jq

# Create instance
curl -X POST https://admin.carher.net/api/instances \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"name":"新用户","model":"gpt","provider":"litellm","app_id":"cli_xxx","app_secret":"xxx","prefix":"s1","owner":"ou_xxx"}'

# Update instance (only non-null fields are applied)
# Supported fields: name, model, provider, owner, deploy_group, image,
#   app_id, app_secret, prefix, bot_open_id
#
# Historical backend defaults may differ. For new instances, always send
# provider=litellm and model=gpt explicitly instead of relying on defaults.
#
# Provider → Model mapping:
#   openrouter: gpt (GPT-5.4), sonnet (Claude Sonnet 4.6), opus (Claude Opus 4.6), gemini (Gemini 3.1 Pro)
#   anthropic:  sonnet (Claude Sonnet 4.6), opus (Claude Opus 4.6)
#   wangsu:     gpt (GPT-5.4), sonnet (Claude Sonnet 4.6), opus (Claude Opus 4.6), gemini (Gemini 3.1 Pro)
#   litellm:    gpt (GPT-5.4), sonnet (Claude Sonnet 4.6), opus (Claude Opus 4.6),
#               gemini (Gemini 3.1 Pro), minimax (MiniMax M2.7),
#               glm (GLM-5), codex (GPT-5.3 Codex)
#
# When provider=litellm, a per-instance LiteLLM virtual key (carher-{uid}) is
# auto-generated for spend tracking. Operator injects LITELLM_API_KEY env var
# into the Pod to override the shared master key.
# Routing: all 7 chat models currently go through OpenRouter.
# Runtime aliases: `gpt`, `sonnet`, `opus`, `gemini`, `minimax`, `glm`, `codex`
# (no `ws-*` / `or-*` aliases in pure LiteLLM mode).
# The create APIs now also return:
#   "cloudflare": {"ok": true|false, "message": "..."}
# If CLOUDFLARE_API_TOKEN is missing on carher-admin, create/batch-import
# fail fast with HTTP 503 instead of silently creating 404 callback routes.
curl -X PUT https://admin.carher.net/api/instances/14 \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","provider":"wangsu","deploy_group":"vip"}'

# Lifecycle
curl -X POST https://admin.carher.net/api/instances/14/stop
curl -X POST https://admin.carher.net/api/instances/14/start
curl -X POST https://admin.carher.net/api/instances/14/restart
curl -X DELETE "https://admin.carher.net/api/instances/14?purge=false"

# Batch operations (actions: stop, start, restart, delete, update)
curl -X POST https://admin.carher.net/api/instances/batch \
  -H "Content-Type: application/json" \
  -d '{"ids":[14,25,30],"action":"restart"}'

# Batch update (action=update with params)
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

# Get Pod logs
curl -s "https://admin.carher.net/api/instances/14/logs?tail=200" | jq .logs

# Get K8s events
curl -s https://admin.carher.net/api/instances/14/events | jq

# Preview config (what openclaw.json would look like)
curl -s https://admin.carher.net/api/instances/14/config-preview | jq

# Current applied config
curl -s https://admin.carher.net/api/instances/14/config-current | jq

# Execute command in Pod (debugging)
curl -X POST https://admin.carher.net/api/instances/14/exec \
  -H "Content-Type: application/json" \
  -d '{"command":"ls -la /data/.openclaw/skills/"}'

# Instance metrics (real-time)
curl -s https://admin.carher.net/api/instances/14/metrics | jq

# Instance metrics (history)
curl -s https://admin.carher.net/api/instances/14/metrics/history | jq
```

### Deploy Groups

```bash
# List all groups (with instance counts, ordered by priority)
curl -s https://admin.carher.net/api/deploy-groups | jq

# Create custom group
curl -X POST https://admin.carher.net/api/deploy-groups \
  -H "Content-Type: application/json" \
  -d '{"name":"vip","priority":5,"description":"VIP users, deployed first"}'

# Update group
curl -X PUT https://admin.carher.net/api/deploy-groups/vip \
  -H "Content-Type: application/json" \
  -d '{"priority":10,"description":"Updated"}'

# Delete group
curl -X DELETE https://admin.carher.net/api/deploy-groups/vip

# Move instance to group
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
# Start deploy (modes: normal, fast, canary-only, group:<name>)
# Use force=true if the tag was already registered by a prior attempt
curl -X POST https://admin.carher.net/api/deploy \
  -H "Content-Type: application/json" \
  -d '{"image_tag":"v20260329-abc1234","mode":"normal","force":false}'

# Webhook (called by GitHub Actions CI)
curl -X POST https://admin.carher.net/api/deploy/webhook \
  -H "Content-Type: application/json" \
  -d '{"image_tag":"v20260329-abc1234","secret":"xxx","branch":"main","commit_sha":"abc1234"}'

# Check deploy status
curl -s https://admin.carher.net/api/deploy/status | jq

# Continue paused deploy
curl -X POST https://admin.carher.net/api/deploy/continue

# Rollback
curl -X POST https://admin.carher.net/api/deploy/rollback

# Abort
curl -X POST https://admin.carher.net/api/deploy/abort

# Deploy history
curl -s "https://admin.carher.net/api/deploy/history?limit=10" | jq
```

### CI/CD & Branch Rules

```bash
# List branch rules
curl -s https://admin.carher.net/api/branch-rules | jq

# Create branch rule
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

# Test branch rule matching (branch is a query parameter, not JSON body)
curl -X POST "https://admin.carher.net/api/branch-rules/test?branch=release/v2.0"

# Trigger GitHub Actions build
# Tip: call /api/ci/workflows first to discover the exact workflow file name.
curl -X POST https://admin.carher.net/api/ci/trigger-build \
  -H "Content-Type: application/json" \
  -d '{"repo":"guangzhou/CarHer","branch":"main","workflow":"<workflow-file>.yml","deploy_mode":"normal"}'

# List CI workflows
curl -s https://admin.carher.net/api/ci/workflows | jq

# List branches
curl -s https://admin.carher.net/api/ci/branches | jq

# List CI runs
curl -s https://admin.carher.net/api/ci/runs | jq
```

### Monitoring & Metrics

```bash
# Cluster status
curl -s https://admin.carher.net/api/status | jq

# Aggregated statistics
curl -s https://admin.carher.net/api/stats | jq

# Health check (all instances)
curl -s https://admin.carher.net/api/health | jq

# Metrics: cluster overview
curl -s https://admin.carher.net/api/metrics/overview | jq

# Metrics: node CPU/memory
curl -s https://admin.carher.net/api/metrics/nodes | jq

# Metrics: node history
curl -s https://admin.carher.net/api/metrics/history/nodes | jq

# Metrics: all pods
curl -s https://admin.carher.net/api/metrics/pods | jq

# Metrics: PVC storage
curl -s https://admin.carher.net/api/metrics/storage | jq

# knownBots registry
curl -s https://admin.carher.net/api/known-bots | jq

# Audit log
curl -s "https://admin.carher.net/api/audit?limit=20" | jq
```

### System Administration

```bash
# Force ConfigMap sync
curl -X POST https://admin.carher.net/api/sync/force | jq

# DB → K8s consistency check
curl -s https://admin.carher.net/api/sync/check | jq

# Trigger SQLite backup
curl -X POST https://admin.carher.net/api/backup | jq

# Import instances from K8s ConfigMaps (one-time migration)
curl -X POST https://admin.carher.net/api/import-from-k8s | jq

# Reconcile cloudflared ConfigMap + remote tunnel ingress
# Requires CLOUDFLARE_API_TOKEN to be configured on carher-admin.
curl -X POST https://admin.carher.net/api/cloudflare/sync | jq

# Get settings
curl -s https://admin.carher.net/api/settings | jq

# Update settings (e.g., webhook_secret)
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

# Get per-instance spend summary (token usage & cost)
curl -s https://admin.carher.net/api/litellm/spend \
  -H "X-API-Key: $API_KEY" | jq
```

### CRD Direct Query

```bash
# List all CRDs (spec + status from K8s etcd)
curl -s https://admin.carher.net/api/crd/instances | jq

# Get single CRD
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

## Environment Variables (for AI Agent)

| Variable | Required | Description |
|----------|----------|-------------|
| `AGENT_LLM_API_KEY` | Yes (for agent) | API key for LLM (OpenRouter/OpenAI/Azure) |
| `AGENT_LLM_BASE_URL` | No | LLM API base URL (default: OpenRouter) |
| `AGENT_MODEL` | No | Model name (default: openai/gpt-4o) |
