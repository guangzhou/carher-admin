---
name: clone-instance-memory
description: >-
  Clone a CarHer bot instance with full PVC data to a new instance ID.
  Use when creating a new her that reuses an existing her's memory, personality,
  workspace, and all persistent data. Covers duplicating instances or migrating
  data between instances.
---

# 克隆 Her 实例（全量 PVC）

将已有 her 实例的配置和全部持久化数据克隆到一个新 ID。

## 前置条件

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)

# 新增 her 前，必须确认 admin 已加载 Cloudflare token；
# 否则创建接口会直接返回 503，避免静默生成 404 callback。
kubectl exec -n carher deploy/carher-admin -- \
  python -c "import os; print(bool(os.environ.get('CLOUDFLARE_API_TOKEN')))"
```

若本地 kubectl 不通（`127.0.0.1:16443` 拒连），先建 SSH 隧道：

```bash
SSHPASS='<password>' sshpass -e ssh -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=30 -p 1023 \
  -L 16443:172.16.1.163:6443 -N root@47.84.112.136 &
```

## 步骤

### 1. 获取源实例配置

```bash
curl -s -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{SOURCE_ID} | jq
```

记录：`name`、`app_id`、`model`、`provider`、`prefix`、`deploy_group`、`owner`、`image`。

**prefix 不要盲目复制源实例！** prefix 决定 OAuth 回调域名（`{prefix}-u{id}-auth.carher.net`）。
源实例可能用特殊 prefix（如 `s3`），新实例通常应使用默认值 `s1`。
必须与用户确认新实例的 prefix，不确定时用 `s1`。

从 K8s Secret 取 `app_secret`：

```bash
kubectl get secret carher-{SOURCE_ID}-secret -n carher \
  -o jsonpath='{.data.app_secret}' | base64 -d
```

### 2. 创建新实例 + 同步镜像

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
  "instances": [{
    "id": {NEW_ID},
    "name": "<同源>",
    "app_id": "<同源>",
    "app_secret": "<同源>",
    "owner": "<同源>",
    "model": "<同源>",
    "provider": "<同源>",
    "prefix": "s1",
    "deploy_group": "<同源>"
  }]
}'
```

返回结果里必须确认 `cloudflare.ok=true`。如果是 `false`，或接口直接返回
`503` 且提示 `CLOUDFLARE_API_TOKEN`，先修 Cloudflare 再继续，不要往下执行。

如果源实例用了**非默认镜像**，新实例也要立即对齐：

```bash
curl -s -X PUT "https://admin.carher.net/api/instances/{NEW_ID}" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"image":"<源实例 image tag>"}'
```

等待 Pod Running：

```bash
curl -s -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{NEW_ID} | jq .status
```

### 3. 临时 Pod 挂载双 PVC 拷贝全部数据

**为什么不用 `kubectl cp` 或 HTTP 传输？**

- `kubectl cp`：数据经 SSH 隧道中转本地，慢且易 EOF 断连
- Pod 间 HTTP：多此一举，两个 PVC 在同一个 NAS 上
- **临时 Pod 挂双 PVC**：`cp -a` 在 NAS 内部直接拷贝，最快最可靠

每个 Pod 只能看到自己的 PVC，无法跨 PVC 直接拷贝。临时 Pod 同时挂载源和目标两个 PVC（均为 `ReadWriteMany`），让 `cp -a` 在同一个 NAS 的两个目录之间执行。

```bash
cat <<'YAML' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: pvc-copier-{SOURCE_ID}-to-{NEW_ID}
  namespace: carher
spec:
  restartPolicy: Never
  containers:
  - name: copier
    image: cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:upgrade-0402-8ef16fb
    command:
    - sh
    - -c
    - |
      echo "=== PVC copy: carher-{SOURCE_ID} -> carher-{NEW_ID} ==="
      du -sh /src/*/ 2>/dev/null | head -20
      echo "--- Copying ---"
      cd /src && cp -a \
        agents browser canvas compaction-reports cron \
        delivery-queue devices extensions \
        feishu-doc-backups feishu-groups \
        feishu-card-text-cache.json feishu-message-text-cache.json \
        feishu-sent-messages.json exec-approvals.json \
        identity media memory subagents tasks workspace \
        /dst/ 2>&1
      [ -f /src/.voice-token ] && cp -a /src/.voice-token /dst/
      echo "=== Done ==="
      du -sh /dst/
    volumeMounts:
    - name: src
      mountPath: /src
      readOnly: true
    - name: dst
      mountPath: /dst
    resources:
      requests:
        cpu: "100m"
        memory: "128Mi"
      limits:
        cpu: "1"
        memory: "512Mi"
  volumes:
  - name: src
    persistentVolumeClaim:
      claimName: carher-{SOURCE_ID}-data
      readOnly: true
  - name: dst
    persistentVolumeClaim:
      claimName: carher-{NEW_ID}-data
YAML
```

等待完成（1.2GB 约 3-5 分钟）：

```bash
kubectl get pod pvc-copier-{SOURCE_ID}-to-{NEW_ID} -n carher --watch
# 等到 Completed，然后查看日志确认
kubectl logs pvc-copier-{SOURCE_ID}-to-{NEW_ID} -n carher
```

清理：

```bash
kubectl delete pod pvc-copier-{SOURCE_ID}-to-{NEW_ID} -n carher
```

### PVC 拷贝清单

**需要拷贝（用户数据）：**

| 目录/文件 | 说明 |
|-----------|------|
| `workspace/` | **MEMORY.md（人格记忆）、SOUL.md、USER.md、IDENTITY.md** + 工作文件 |
| `memory/` | 语义搜索 SQLite（main.sqlite） |
| `agents/` | 对话历史 |
| `media/` | 媒体文件 |
| `browser/` | 浏览器数据 |
| `feishu-groups/` | 飞书群配置 |
| `tasks/` | 任务数据 |
| `identity/`、`canvas/`、`cron/`、`extensions/`、`subagents/` 等 | 其他运行时数据 |

**不拷贝（由 ConfigMap / 共享 PVC 覆盖挂载）：**

| 路径 | 原因 |
|------|------|
| `openclaw.json*` | operator 按实例生成，会被 ConfigMap 覆盖 |
| `carher-config.json`、`shared-config.json5` | ConfigMap base-config 挂载 |
| `skills/` | 共享只读 PVC |
| `sessions/` | 共享 PVC，按 uid 子路径隔离 |
| `logs/`、`update-check.json` | 运行时自动生成 |
| `feishu-user-tokens/` | OAuth token 绑定原实例 |

### 4. 验证关键文件

```bash
curl -s -X POST -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  https://admin.carher.net/api/instances/{NEW_ID}/exec \
  -d '{"command":"ls -la /data/.openclaw/workspace/MEMORY.md /data/.openclaw/workspace/SOUL.md /data/.openclaw/memory/main.sqlite"}'
```

三个文件都存在且大小与源一致即可。

### 5. 重启新实例 + 停源实例

```bash
curl -s -X POST -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{NEW_ID}/restart

# 同 app_id 不能同时运行，停掉源实例
curl -s -X POST -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{SOURCE_ID}/stop
```

等 ~30s 后确认：

```bash
curl -s -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{NEW_ID} | jq '{status, feishu_ws, oauth_url}'
```

再做一次 callback live 验证：

```bash
# 正常结果应为 HTTP 400（无效测试 code），不是 404
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://{prefix}-u{NEW_ID}-auth.carher.net/feishu/oauth/callback?code=test&state=test"
```

## OAuth 回调地址

自动生成规则：`https://{prefix}-u{id}-auth.carher.net/feishu/oauth/callback`

## 注意事项

| 事项 | 说明 |
|------|------|
| **拷贝全量 PVC** | 不能只拷 `memory/main.sqlite`；bot 的人格记忆在 `workspace/MEMORY.md`，缺少则 bot 不认识用户 |
| **临时 Pod 挂双 PVC** | 唯一可靠的跨 PVC 拷贝方式；`kubectl cp` 经隧道会断，HTTP 多此一举 |
| **镜像版本** | 当前默认镜像是 `upgrade-0402-8ef16fb`；若源实例使用其他 tag，必须创建后立即 `PUT /api/instances/{id}` 对齐 |
| **prefix 不要照搬** | 源实例可能用特殊 prefix（如 s3），新实例默认用 `s1`，必须与用户确认 |
| **Cloudflare 必须成功** | 新实例创建后必须检查 `cloudflare.ok=true`；否则 callback 可能 `404` |
| **同 app_id 冲突** | 新旧实例共用同一飞书应用时，不能同时运行，否则消息路由混乱、响应丢失 |
| **WAL 文件** | 若 `memory/` 下有 `-wal`/`-shm`，拷贝前先 `PRAGMA wal_checkpoint(TRUNCATE)` |
| **exec API 白名单** | 只允许 ls/cat/du/node 等；tar/sqlite3/cp 不在白名单，大操作用 kubectl exec 或临时 Pod |
| **不影响其他实例** | 每实例独立 PVC，sessions 按 uid 子路径隔离 |
| **LiteLLM Key** | 若源实例 `provider=litellm`，新实例会自动生成独立的 LiteLLM 虚拟 key（不会复用源实例的 key），spend 从零开始 |
