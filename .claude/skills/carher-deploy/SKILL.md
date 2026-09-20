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

1. CI 构建 `her/carher` 镜像（从 `docker/Dockerfile`）
2. Tag 格式：`v{YYYYMMDD}-{sha7}`
3. 推送到 ACR
4. CI 调用 `POST /api/deploy/webhook` → admin 触发 bot 实例滚动更新
5. CI 轮询 `/api/deploy/status` 90s 验证

**触发条件**：`docker/**` 或 `configs/**` 有变更时触发。admin/operator 代码变更不触发。

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

# 回滚（仅对 admin web /api/deploy 部署过的版本有效，30 天 SQLite 保留）
curl -X POST -H "X-API-Key: $API_KEY" https://admin.carher.net/api/deploy/rollback

# 中止
curl -X POST -H "X-API-Key: $API_KEY" https://admin.carher.net/api/deploy/abort

# 部署历史
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/deploy/history?limit=10" | jq
```

---

## 单实例回滚到上一版 fix 镜像（admin rollback 不可用时的兜底）

**何时用本节而不是 `/api/deploy/rollback`**：

- 当前 image 是手工 `kubectl patch herinstance` 上去的（如批量推 `67ffa406-clean` 时 admin webhook 没走），`/api/deploy/history` 里没有对应 deploy 记录
- 只想回 1-3 台 prod her，不想触发全员 rollout
- 30 天前的 deploy 记录已被 SQLite 清掉，但镜像 tag 还在 ACR

### 定位"上一版 fix 镜像"

集群里如果全员都已经在新版上跑（`kubectl get her -o jsonpath='{...spec.image}' | sort | uniq -c` 只剩一种 tag），fallback baseline 在 admin 代码里：

```bash
ADMIN_POD=$(kubectl -n carher get pod -l app=carher-admin -o name | head -1 | sed 's|pod/||')
kubectl -n carher exec $ADMIN_POD -- grep -n "DEFAULT_IMAGE_TAG\|fix-compact" /app/backend/k8s_ops.py /app/backend/database.py 2>/dev/null
```

`backend/k8s_ops.py:24` 的 `DEFAULT_IMAGE_TAG = "fix-compact-eb348941"` 就是新 her 创建时用的默认版，也是事实上的"上一稳定版" baseline。

### 单实例 patch（per-her 回滚模板）

```bash
TARGET_IMAGE=fix-compact-eb348941
for UID in 1000 30 53; do
  echo "--- her-$UID before ---"
  kubectl -n carher get herinstance her-$UID -o jsonpath='{.spec.image}{"\n"}'
  kubectl -n carher patch herinstance her-$UID --type=merge -p "{\"spec\":{\"image\":\"$TARGET_IMAGE\"}}"
done
```

operator 走 ReadinessGate：新 pod 起 → ws ready → 旧 pod terminate，单 her 耗时 ~60-90s，零中断（详见 `hot-grayscale` skill）。

### 单实例回滚验证三件套（prod 必须全过）

```bash
UID=1000  # 替换
POD=$(kubectl -n carher get pod -l app=carher-user,user-id=$UID -o name | head -1 | sed 's|pod/||')

# 1. CRD 状态 + Pod image
kubectl -n carher get herinstance her-$UID \
  -o jsonpath='{"image: "}{.spec.image}{"\nphase: "}{.status.phase}{"\nfeishuWS: "}{.status.feishuWS}{"\n"}'
kubectl -n carher get pod $POD \
  -o jsonpath='{range .spec.containers[*]}{.name}{" → "}{.image}{"\n"}{end}'

# 2. restarts=0（OOM / crashloop 的快速排除）
kubectl -n carher get pod $POD \
  -o jsonpath='{range .status.containerStatuses[*]}{.name}{" restarts="}{.restartCount}{" ready="}{.ready}{"\n"}{end}'

# 3. 业务真活了（飞书 WS + bot-registry 都建上）
kubectl -n carher logs $POD -c carher --tail=80 2>&1 \
  | grep -iE "ws client ready|bot-registry.*registered self|bot-registry.*discovered" | tail -3
```

期望看到：
- `restarts=0 ready=true`
- `[bot-registry] registered self: <appId>`
- `[bot-registry] discovered <N> bots`
- `[ws] ws client ready`

任何一条缺失 → 看 `carher-her-reply-failure-triage` skill 分诊。

### 集群分布对账（每次 patch 后必做）

```bash
kubectl -n carher get herinstance -o jsonpath='{range .items[*]}{.spec.image}{"\n"}{end}' \
  | sort | uniq -c | sort -rn
```

应当看到 canary 数量精确符合预期（如 `3 fix-compact-eb348941 + 216 67ffa406-clean`）。多一台或少一台都说明操作出错或还没 reconcile 完。

### 踩坑

| 坑 | 原因 | 规避 |
|---|---|---|
| `/api/deploy/rollback` 报"no prev_image_tag" | 当前 image 是手工 patch 上去的，admin SQLite `deploys` 表里 prev 字段空 | 改走 `kubectl patch herinstance` 单实例回，并核对目标 tag 在 ACR 还在（用其它 her 跑过的版本最稳） |
| ACR 上目标 tag 已被回收 | ACR 镜像保留策略可能清掉超过 N 天的 tag | patch 后看 pod events 是否 ImagePullBackOff，如果是立即 patch 回当前 image 撤销 |
| status.phase 短暂滞后 | operator status reconcile 不立即触发 | 等 10s 再读 phase，别相信 patch 后立即读到的值（spec 已变，status 还在赶） |
| 19+ 并发 `kubectl patch` 部分 TLS handshake 失败 | jms tunnel 单连接被多个并发请求打饱（"first record does not look like a TLS handshake"），无 retry 兜底 | 批量 >10 台限 `xargs -P 4-6` 串并联；patch 后必跑"补漏 grep"对账没 patched 上的 id，串行补 patch |

---

## 批量 prod 回滚（5-30 台一次）

19 台 prod her 同时回滚到 `fix-compact-eb348941` 的实测模板（2026-05-18）。**比单实例多两件事**：限并发避免 tunnel 打饱 + 补漏对账。

### 步骤 1：核对范围（所有目标都存在 + 都在当前 image）

```bash
cat <<'EOF' > /tmp/check_hers.sh
#!/bin/bash
IDS=(66 89 90 91 105 117 126 161 162 172 192 193 202 203 204 205 206 211 212)  # 替换
printf "%-10s %-25s %-10s %-10s %s\n" "her_id" "image" "phase" "ws" "owner"
for id in "${IDS[@]}"; do
  out=$(kubectl -n carher get herinstance "her-$id" -o jsonpath='{.spec.image}|{.status.phase}|{.status.feishuWS}|{.spec.name}' 2>/dev/null)
  [ -z "$out" ] && { printf "%-10s NOT_FOUND\n" "her-$id"; continue; }
  IFS='|' read -r img phase ws name <<< "$out"
  printf "%-10s %-25s %-10s %-10s %s\n" "her-$id" "$img" "$phase" "$ws" "$name"
done
EOF
bash /tmp/check_hers.sh
```

任何一台 NOT_FOUND 或者已经不在源 image 上，**立刻停**核实是不是给错 id 了。

### 步骤 2：批量 patch（限并发 4-6）

```bash
TARGET=fix-compact-eb348941
printf '%s\n' "${IDS[@]}" | xargs -P 5 -I{} kubectl -n carher patch herinstance her-{} \
  --type=merge -p "{\"spec\":{\"image\":\"$TARGET\"}}"
```

⚠️ **不要无脑 `&` + `wait` 并发全部**——jms tunnel 单连接，19 个并发会 5 个 TLS handshake fail（实测）。`-P 5` 比较稳。

### 步骤 3：补漏对账（patch 阶段必跑）

```bash
cat <<'EOF' > /tmp/check_patched.sh
#!/bin/bash
IDS=(66 89 90 91 105 117 126 161 162 172 192 193 202 203 204 205 206 211 212)  # 替换
TARGET=fix-compact-eb348941
for id in "${IDS[@]}"; do
  img=$(kubectl -n carher get herinstance "her-$id" -o jsonpath='{.spec.image}' 2>/dev/null)
  [ "$img" = "$TARGET" ] && printf "her-%-4s ✓\n" "$id" || printf "her-%-4s ✗ %s\n" "$id" "$img"
done
EOF
bash /tmp/check_patched.sh | grep '✗' && {
  echo "→ 串行补 patch 漏的"
  for id in <漏的id列表>; do
    kubectl -n carher patch herinstance "her-$id" --type=merge \
      -p '{"spec":{"image":"'$TARGET'"}}'
  done
}
```

### 步骤 4：等 rollout（监控版）

```bash
cat <<'EOF' > /tmp/wait_rollout.sh
#!/bin/bash
IDS=(66 89 90 91 105 117 126 161 162 172 192 193 202 203 204 205 206 211 212)  # 替换
TOTAL=${#IDS[@]}
for round in 1 2 3 4 5 6 7 8 9 10; do
  sleep 30
  done_count=0; not_done=()
  for id in "${IDS[@]}"; do
    pods=$(kubectl -n carher get pod -l app=carher-user,user-id=$id --no-headers 2>/dev/null | wc -l | tr -d ' ')
    running=$(kubectl -n carher get pod -l app=carher-user,user-id=$id --no-headers 2>/dev/null | awk '$3=="Running"' | wc -l | tr -d ' ')
    if [ "$pods" = "1" ] && [ "$running" = "1" ]; then
      done_count=$((done_count + 1))
    else
      not_done+=("her-$id($pods/$running)")
    fi
  done
  echo "=== round $round (t+$((round*30))s): $done_count/$TOTAL done ==="
  [ ${#not_done[@]} -gt 0 ] && [ ${#not_done[@]} -le 8 ] && echo "  pending: ${not_done[*]}"
  [ "$done_count" = "$TOTAL" ] && { echo "→ 全部 rollout 完成"; exit 0; }
done
echo "→ 超时仍有 $((TOTAL - done_count)) 台未完成: ${not_done[*]}"
EOF
bash /tmp/wait_rollout.sh
```

**典型耗时**：19 台并行 rollout，~2-4min 全部完成（per-her ReadinessGate 独立，不互相阻塞）。

### 步骤 5：批量验证（table 一屏看完）

```bash
cat <<'EOF' > /tmp/verify_batch.sh
#!/bin/bash
IDS=(66 89 90 91 105 117 126 161 162 172 192 193 202 203 204 205 206 211 212)  # 替换
printf "%-8s %-25s %-22s %-8s %s\n" "her" "spec.image" "pod_image_tag" "restart" "pod_age"
for id in "${IDS[@]}"; do
  spec_img=$(kubectl -n carher get herinstance "her-$id" -o jsonpath='{.spec.image}' 2>/dev/null)
  pod_line=$(kubectl -n carher get pod -l app=carher-user,user-id=$id --no-headers 2>/dev/null | head -1)
  pod_name=$(echo "$pod_line" | awk '{print $1}')
  ready=$(echo "$pod_line" | awk '{print $2}')
  restarts=$(echo "$pod_line" | awk '{print $4}')
  age=$(echo "$pod_line" | awk '{print $5}')
  pod_img=$(kubectl -n carher get pod "$pod_name" -o jsonpath='{.spec.containers[0].image}' 2>/dev/null | awk -F: '{print $NF}')
  printf "%-8s %-25s %-22s %-8s %s (%s)\n" "her-$id" "$spec_img" "$pod_img" "$restarts" "$age" "$ready"
done
EOF
bash /tmp/verify_batch.sh
```

要全部满足：
- `spec.image == pod_image_tag == 目标 tag`
- `restart=0`（OOM / crashloop 排除）
- `pod_age` 在 1-5 min 内（**反推 rollout 是真发生过**——如果 pod age 跟 patch 时间不匹配，说明 operator 没触发滚动，要排查）
- `ready=2/2`

### 步骤 6：业务采样（3 台足够，不必全跑）

```bash
for id in <头/中/尾各挑一台>; do
  POD=$(kubectl -n carher get pod -l app=carher-user,user-id=$id -o name | head -1 | sed 's|pod/||')
  echo "--- her-$id ($POD) ---"
  kubectl -n carher logs $POD -c carher --tail=500 2>&1 \
    | grep -iE "ws client ready|bot-registry.*registered self|bot-registry.*discovered" | tail -3
done
```

⚠️ `--tail=80` 经常翻页出去看不到（pod 启动 2-3min 后 grep 不到 ws ready），改 `--tail=500` 起步。

### 步骤 7：集群分布对账

```bash
kubectl -n carher get herinstance -o jsonpath='{range .items[*]}{.spec.image}{"\n"}{end}' \
  | sort | uniq -c | sort -rn
```

期望两行：`<本次回滚总数> <目标 tag>` + `<原集群规模 - 本次回滚总数> <原 tag>`，加起来 = 全 her 总数。

任何不对就溯源。

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
