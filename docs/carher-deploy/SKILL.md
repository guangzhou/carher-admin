---
name: carher-deploy
description: >-
  Deploy carher main program (bot instances) to Alibaba Cloud K8s.
  Use when rolling out new image tags to bot instances, performing
  canary/grayscale deployments, or troubleshooting bot deploy failures.
  Does NOT touch admin or operator deployments.
---

# CarHer 主程序（Bot 实例）部署

对所有 bot 实例（`carher-{uid}`）进行滚动更新。**不涉及 admin 或 operator**。

Bot 实例由 operator 管理（Deployment），镜像更新通过 admin API 触发 operator reconcile。

| Component | Image | K8s Resource | Managed By |
|-----------|-------|-------------|------------|
| **carher bot** | `her/carher` | `deploy/carher-{uid}` (per user) | carher-operator |

---

## 方式 1：CI/CD（默认）

Push 到 `main` 分支自动触发 `.github/workflows/build-deploy.yml`：

1. CI 构建 admin + operator 镜像（bot 镜像共用同一 tag）
2. Tag 格式：`v{YYYYMMDD}-{sha7}`
3. CI 调用 `POST /api/deploy/webhook` → admin 触发 bot 实例滚动更新
4. CI 轮询 `/api/deploy/status` 90s 验证

**Paths ignored**（不触发 CI）：`*.md`, `docs/**`

Deploy modes（通过 webhook 的 mode 字段或 branch rules 控制）：

| Mode | 行为 |
|------|------|
| `normal` | canary 组先更新 → 健康检查通过 → stable 组跟进 |
| `fast` | 所有实例并行更新 |
| `canary-only` | 只更新 canary 组，stable 不动 |
| `group:<name>` | 只更新指定灰度组 |

---

## 方式 2：Admin API（CI deploy 失败时使用）

CI 镜像构建成功但 webhook/deploy 步骤失败时，手动触发：

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)

TAG="v$(date +%Y%m%d)-$(git rev-parse --short HEAD)"

# modes: fast, normal, canary-only, group:<name>
# force=true 如果 tag 已被之前的尝试注册过
curl -s -X POST "https://admin.carher.net/api/deploy" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d "{\"image_tag\":\"$TAG\",\"mode\":\"fast\",\"force\":true}"
```

### 检查部署状态

```bash
curl -s -H "X-API-Key: $API_KEY" https://admin.carher.net/api/deploy/status | jq
```

### 其他操作

```bash
# 继续暂停的部署
curl -X POST -H "X-API-Key: $API_KEY" https://admin.carher.net/api/deploy/continue

# 回滚
curl -X POST -H "X-API-Key: $API_KEY" https://admin.carher.net/api/deploy/rollback

# 中止
curl -X POST -H "X-API-Key: $API_KEY" https://admin.carher.net/api/deploy/abort

# 部署历史
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/deploy/history?limit=10" | jq
```

---

## 方式 3：手动 Webhook 触发

重新触发 webhook（例如修复 GitHub secrets 后）：

```bash
WEBHOOK_SECRET=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.deploy-webhook-secret}' | base64 -d)
TAG="v$(date +%Y%m%d)-$(git rev-parse --short HEAD)"

curl -s -X POST "https://admin.carher.net/api/deploy/webhook" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg tag "$TAG" --arg secret "$WEBHOOK_SECRET" \
    --arg branch "main" --arg sha "$(git rev-parse HEAD)" \
    --arg msg "$(git log -1 --format=%s)" \
    --arg author "$(git log -1 --format=%an)" \
    --arg repo "guangzhou/carher-admin" \
    --arg run_url "https://github.com/guangzhou/carher-admin/actions" \
    '{image_tag:$tag, secret:$secret, mode:"", branch:$branch,
      commit_sha:$sha, commit_msg:$msg, author:$author, repo:$repo,
      run_url:$run_url}')"
```

---

## Deploy Response Codes

| Response | Meaning | Action |
|----------|---------|--------|
| `200 status:pending` | Deploy started | Monitor via `/api/deploy/status` |
| `200 already_deployed` | Tag already deployed | Add `"force":true` to re-deploy |
| `200 build_only` | Branch rule has `auto_deploy=false` | Use `POST /api/deploy` directly |
| `403 Invalid webhook secret` | Secret mismatch | See Pitfall below |

## Secrets

| Secret | Keys | Usage |
|--------|------|-------|
| `carher-admin-secrets` | `admin-api-key` | Admin API 认证 |
| `carher-admin-secrets` | `deploy-webhook-secret` | Webhook 认证 |

## Pitfalls

### Webhook 403

**Symptom**: CI build succeeds but deploy fails with 403 `Invalid webhook secret`.

**Cause**: Three places store the webhook secret, must all agree:
1. **DB setting** `webhook_secret` — highest priority
2. **K8s env var** `DEPLOY_WEBHOOK_SECRET` — fallback
3. **GitHub repo secret** `DEPLOY_WEBHOOK_SECRET` — CI sends this

**Fix**:

```bash
# 1. Read canonical value from K8s Secret
WEBHOOK_SECRET=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.deploy-webhook-secret}' | base64 -d)

# 2. Sync DB setting
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)
curl -s -X PUT "https://admin.carher.net/api/settings" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d "$(jq -n --arg s "$WEBHOOK_SECRET" '{webhook_secret: $s}')"

# 3. Sync GitHub repo secret
# → https://github.com/guangzhou/carher-admin/settings/secrets/actions
```

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| Webhook 403 | Compare secrets in DB/K8s/GitHub | Sync all three (see above) |
| Deploy stuck | `/api/deploy/status` | `POST /api/deploy/abort` then retry |
| Canary paused | Dashboard → Deploy status | Check canary health, `POST /api/deploy/continue` |
| Instances not updating | `kubectl get deploy -n carher` | Check operator logs, CRD status |
