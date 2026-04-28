---
name: migrate-s3-to-k8s
description: >-
  Migrate CarHer bot instances from on-prem S3 Docker servers to Alibaba Cloud K8s.
  Use when migrating user data, creating K8s instances for S3 users, or performing
  batch migration from Docker containers to K8s pods. Covers data transfer,
  instance creation, bot config, and verification.
---

# S3 Docker → K8s 迁移

将内网 Docker 服务器（S3）上的 Her 实例迁移到阿里云 ACK K8s 集群。

**核心原则**：容器是一次性运行环境，迁移的只有两样东西：
1. **用户数据** — `/data/.openclaw/` 下的记忆、会话、SQLite 数据库
2. **用户配置** — 飞书凭证、模型选择等（从 S3 `users.csv` 提取）

镜像不需要迁移，K8s 上统一使用当前线上版本。

## 环境信息

> **所有 SSH 操作都走 JumpServer 堡垒机**（详见 `k8s-via-bastion` skill）。
> 旧的 `sshpass -p ... ssh cltx@10.68.13.188` / `ssh -p 1023 root@47.84.112.136`
> 直连方式已下线。

### S3 Docker 服务器（堡垒机资产 `JSZX-AI-03`）

| 项目 | 值 |
|------|---|
| 内网 IP | 10.68.13.188 |
| 资产名 | `JSZX-AI-03` |
| 入口 | `scripts/jms ssh JSZX-AI-03 '...'` |
| 工作目录 | /Data/CarHer |
| 用户配置 | /Data/CarHer/docker/users.csv |
| 数据卷路径 | /Data/docker/volumes/carher-{id}-data/_data/ |

> S3 上 sudo 操作走 JMS 登录账号的默认 sudoers；如果 sudo 提示输入密码，
> 联系管理员在 `/etc/sudoers.d/` 加 `NOPASSWD`。

### K8s 构建服务器（堡垒机资产 `k8s-work-227`）

| 项目 | 值 |
|------|---|
| 内网 IP | 172.16.0.227 |
| 资产名 | `k8s-work-227` |
| 入口 | `scripts/jms ssh k8s-work-227 '...'` |
| NAS 根挂载 | /Data/ |
| PVC 路径规律 | /Data/{pv-name}/ （pv-name = PVC 的 spec.volumeName） |

### 数据传输链路（已改走堡垒机）

```
S3 (JSZX-AI-03)  ──jms 流式传输──▶  Mac  ──jms 流式传输──▶  k8s-work-227
                                                                  │
                                                                  ▼  VPC 内网 NFS mount (/Data/)
                                                              阿里云 NAS ──── K8s PVC
```

| 链路段 | 带宽（估） | 网络 | 说明 |
|--------|------|------|------|
| S3 → Mac | LAN 速率 | 公司联通 IDC LAN（10.68.x.x） | jms 流式 SSH cat |
| Mac → k8s-work-227 | Mac 上行 ~1-6 MB/s | 公网（Mac → 阿里云） | jms 流式 SSH cat |
| k8s-work-227 → NAS | NFS 本地写入速度 | **VPC 内网** | NAS 挂载在 /Data，tar 解压即落盘 |

> 与旧链路相比：S3→Mac 段更快（LAN），但多一次 Mac→Aliyun 出口。
> 整体耗时基本相同（仍受限于公网带宽）。
> 也可以用一条 pipe 把 Mac 中转的临时文件省掉（见 Phase 5）。

## 前置条件

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)

kubectl exec -n carher deploy/carher-admin -- \
  python -c "import os; print(bool(os.environ.get('CLOUDFLARE_API_TOKEN')))"

# kubectl 隧道（详见 k8s-via-bastion）：
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

## 迁移步骤（以 carher-N 为例）

### Phase 1: 获取 S3 配置

```bash
S3_ASSET="JSZX-AI-03"     # 堡垒机资产名（替代旧的 cltx@10.68.13.188）
USER_ID=75                # 替换为实际 ID

scripts/jms ssh $S3_ASSET \
  "grep '^${USER_ID},' /Data/CarHer/docker/users.csv"
```

**CSV 字段映射**（实际 10 列，header 只声明 9 列）：

> ⚠️ **第 10 列 `feishu_bot_open_id` 容易和第 6 列 `feishu_owner_open_id` 混淆！**
> - 第 6 列是 **owner 的 open_id**（人）→ 映射到 Admin API `owner` 字段
> - 第 10 列是 **bot 自身的 open_id**（机器人）→ 映射到 Admin API `bot_open_id` 字段
> - 两者搞混会导致群聊 @mention 被安全策略拦截（non-owner ignoring）

| CSV 列 | 位置 | 字段名 | Admin API 字段 | 示例 |
|--------|------|--------|---------------|------|
| id | 列1 | id | id | 75 |
| 姓名 | 列2 | 姓名 | name | 林森的her |
| 模型 | 列3 | 模型 | model | gpt / opus / sonnet |
| feishu_app_id | 列4 | 飞书 App ID | app_id | cli_a94a0b73a878dbcb |
| feishu_app_secret | 列5 | 飞书 App Secret | app_secret | 7bCyWY7oXGLk... |
| feishu_owner_open_id | 列6 | Owner open_id（人） | owner | ou_xxx（可为空） |
| provider | 列7 | 模型提供商 | provider | 统一改为 litellm |
| 备注 | 列8 | 备注 | — | 林森的her |
| owner_allow_from | 列9 | Owner 白名单（共享 bot） | — | `ou_a\|ou_b`（可为空） |
| feishu_bot_open_id | **列10** | **Bot 自身 open_id** | **bot_open_id** | ou_31044043...（可为空） |

**域名前缀**：根据所在服务器确定（S1→s1, S2→s2, S3→s3），映射到 Admin API `prefix` 字段。

```bash
scripts/jms ssh $S3_ASSET \
  "docker logs carher-${USER_ID} 2>&1 | grep 'agent model' | tail -1"

scripts/jms ssh $S3_ASSET \
  "sudo du -sh /Data/docker/volumes/carher-${USER_ID}-data/_data/"
```

### Phase 2: 创建 K8s 实例

通过 Admin API `batch-import` 创建实例。会自动完成：
- K8s Secret（存储 app_secret）
- HerInstance CRD
- PVC + Service + ConfigMap + Deployment（Operator reconcile）
- LiteLLM 虚拟 Key（provider=litellm 时自动生成）
- Cloudflare DNS + tunnel ingress

```bash
# 替换以下变量为 Phase 1 获取的真实值
# ⚠️ owner = CSV 列6（人的 open_id），bot_open_id = CSV 列10（bot 的 open_id），不要搞混！
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
  "instances": [{
    "id": '$USER_ID',
    "name": "林森的her",
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "owner": "",
    "bot_open_id": "ou_xxx",
    "model": "opus",
    "provider": "litellm",
    "prefix": "s3"
  }]
}'
```

**必须检查返回结果**：

```json
{"results": [{"id": 75, "status": "created", "cloudflare": {"ok": true}}]}
```

- `cloudflare.ok=true` → 正常
- `cloudflare.ok=false` → 先修 Cloudflare 再继续
- `409` → ID 已存在，需要先处理旧的 CRD

### Phase 3: 暂停 K8s 实例 + 停止 S3 容器

**必须先暂停 K8s、再停 S3**，原因：
- 同一飞书 App ID 不能同时有两个 WebSocket 连接，否则消息路由混乱
- 停 S3 前必须确认 K8s 已暂停（不会抢连接）
- 停 S3 后再打包，确保 SQLite 数据一致性（无 WAL 写入）

```bash
# 3a. 暂停 K8s 实例
curl -s -X POST -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/${USER_ID}/stop

# 确认已暂停
curl -s -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/${USER_ID} | jq '.status'

# 3b. 停止 S3 容器
scripts/jms ssh $S3_ASSET \
  "cd /Data/CarHer && ./start-user.sh --id=${USER_ID} --down"

scripts/jms ssh $S3_ASSET \
  "docker ps --filter name=carher-${USER_ID} --format '{{.Names}} {{.Status}}'"
```

> **⚠️ 从此刻起用户服务中断**，直到 Phase 6 K8s 实例启动成功。
> 建议在用户不活跃时段（如午休、下班后）执行。

### Phase 4: 打包 S3 数据（容器已停，数据一致）

```bash
scripts/jms ssh $S3_ASSET \
  "sudo tar czf /tmp/carher-${USER_ID}-data.tar.gz \
   -C /Data/docker/volumes/carher-${USER_ID}-data/_data ."

scripts/jms ssh $S3_ASSET \
  "ls -lh /tmp/carher-${USER_ID}-data.tar.gz"
```

**打包整个目录**（不做文件筛选），原因：
- 避免遗漏数据（feishu-groups、cron、identity 等容易忘记）
- K8s 上 ConfigMap 挂载会自动覆盖 `openclaw.json`、`carher-config.json`、`shared-config.json5`
- `skills/` 目录由共享只读 PVC 覆盖挂载
- 多余文件不影响运行，但缺少文件可能导致功能丢失
- **容器已停止，不会出现 "file changed as we read it" 警告，SQLite 无 WAL 残留**

### Phase 5: 传输数据到 K8s PVC

**推荐路径：一条 pipe S3 → Mac → 构建服务器（无中间临时文件）**

```bash
BUILD_ASSET="k8s-work-227"

# 5a. 查找 PVC 对应的 NAS 路径
PV_NAME=$(kubectl get pvc carher-${USER_ID}-data -n carher \
  -o jsonpath='{.spec.volumeName}')
echo "PV: $PV_NAME → /Data/$PV_NAME/"

# 5b. 流式拉到构建服务器（不落 Mac 本地磁盘）
scripts/jms scp $S3_ASSET:/tmp/carher-${USER_ID}-data.tar.gz - \
  | scripts/jms scp - $BUILD_ASSET:/tmp/carher-${USER_ID}-data.tar.gz

# 5c. 在构建服务器上解压到 NAS
scripts/jms ssh $BUILD_ASSET \
  "tar xzf /tmp/carher-${USER_ID}-data.tar.gz -C /Data/$PV_NAME/"

# 5d. 验证文件数和关键文件
scripts/jms ssh $BUILD_ASSET \
  "ls /Data/$PV_NAME/memory/main.sqlite && \
   ls /Data/$PV_NAME/workspace/MEMORY.md && \
   find /Data/$PV_NAME/ -type f | wc -l"

# 5e. 清理临时文件
scripts/jms ssh $S3_ASSET     "sudo rm /tmp/carher-${USER_ID}-data.tar.gz"
scripts/jms ssh $BUILD_ASSET  "rm /tmp/carher-${USER_ID}-data.tar.gz"
```

**备选路径：先落 Mac 再 kubectl exec（构建服务器不可用时）**

```bash
scripts/jms scp $S3_ASSET:/tmp/carher-${USER_ID}-data.tar.gz \
  /tmp/carher-${USER_ID}-data.tar.gz

curl -s -X POST -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/${USER_ID}/start
sleep 60

kubectl exec -i -n carher deploy/carher-${USER_ID} -c carher \
  -- tar xzf - -C /data/.openclaw < /tmp/carher-${USER_ID}-data.tar.gz
```

### Phase 6: 启动 K8s 实例

S3 容器已在 Phase 3 停止，现在启动 K8s 实例，服务恢复。

```bash
# 6a. 启动 K8s 实例（解除暂停）
curl -s -X POST -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/${USER_ID}/start

# 6b. 等待 Pod 就绪（约 60-90s）
for i in $(seq 1 12); do
  STATUS=$(curl -s -H "X-API-Key: $API_KEY" \
    https://admin.carher.net/api/instances/${USER_ID} | jq -r '.status.phase // .status')
  WS=$(curl -s -H "X-API-Key: $API_KEY" \
    https://admin.carher.net/api/instances/${USER_ID} | jq -r '.status.feishuWS // .feishu_ws // "unknown"')
  echo "[$i] phase=$STATUS ws=$WS"
  [ "$WS" = "Connected" ] && break
  sleep 10
done
```

### Phase 7: 验证

```bash
# 7a. CRD 状态
kubectl get her her-${USER_ID} -n carher \
  -o jsonpath='image={.spec.image} phase={.status.phase} ws={.status.feishuWS}'
echo ""

# 7b. Pod 日志关键检查
kubectl logs deploy/carher-${USER_ID} -n carher -c carher --tail=100 | grep -E \
  'agent model|WSClient connected|ws client ready|memory-bridge|feishu_'

# 7c. 记忆文件验证
curl -s -X POST -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  https://admin.carher.net/api/instances/${USER_ID}/exec \
  -d '{"command":"ls -la /data/.openclaw/workspace/MEMORY.md /data/.openclaw/memory/main.sqlite"}'

# 7d. OAuth 回调验证（应返回 400，不是 404）
PREFIX="s3"  # 替换为实际前缀
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://${PREFIX}-u${USER_ID}-auth.carher.net/feishu/oauth/callback?code=test&state=test"

# 7e. 飞书端到端测试
# 在飞书上给 Bot 发一条消息，确认：
# 1. 能收到回复
# 2. Her 认识你（记忆没丢失）
# 3. 功能正常（cron、群聊等）
```

### Phase 8: 回滚（如果出问题）

```bash
# 8a. 停止 K8s 实例
curl -s -X POST -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/${USER_ID}/stop

# 8b. 重启 S3 容器
scripts/jms ssh $S3_ASSET \
  "cd /Data/CarHer && ./start-user.sh --id=${USER_ID}"

# 8c. Cloudflare DNS 切回 S3 tunnel（如果域名已切到 K8s）
# Admin API 创建实例时自动将域名指向 K8s tunnel
# 回滚需要手动切回，在 S3 服务器上执行：
scripts/jms ssh $S3_ASSET \
  "sudo cloudflared tunnel route dns --overwrite-dns carher-s3 s3-u${USER_ID}-auth.carher.net && \
   sudo cloudflared tunnel route dns --overwrite-dns carher-s3 s3-u${USER_ID}-fe.carher.net && \
   sudo cloudflared tunnel route dns --overwrite-dns carher-s3 s3-u${USER_ID}-proxy.carher.net"
```

## 批量迁移

对于多个实例，按以下顺序执行：

### 1. 批量获取 S3 配置

```bash
scripts/jms ssh $S3_ASSET \
  "docker ps --filter 'name=carher-' --format '{{.Names}}' | sort -t- -k2 -n"

scripts/jms ssh $S3_ASSET "cat /Data/CarHer/docker/users.csv"
```

### 2. 批量创建 K8s 实例

将 CSV 数据整理为 batch-import 格式，一次创建多个：

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
  "instances": [
    {"id":75,"name":"张三的her","app_id":"cli_xxx","app_secret":"xxx","owner":"ou_xxx","model":"gpt","provider":"litellm","prefix":"s3"},
    {"id":76,"name":"李四的her","app_id":"cli_yyy","app_secret":"yyy","owner":"ou_yyy","model":"gpt","provider":"litellm","prefix":"s3"}
  ]
}'
```

**创建后立即批量暂停**（防止飞书冲突）：

```bash
curl -s -X POST -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  https://admin.carher.net/api/instances/batch \
  -d '{"ids":[75,76],"action":"stop"}'
```

### 3. 批量停 S3 容器 + 逐个传输数据

**先停所有待迁移容器**（确保数据一致），再逐个打包传输：

```bash
S3_ASSET="JSZX-AI-03"
BUILD_ASSET="k8s-work-227"

# 3a. 批量停止 S3 容器（建议在用户不活跃时段执行）
for UID in 75 76; do
  scripts/jms ssh $S3_ASSET \
    "cd /Data/CarHer && ./start-user.sh --id=$UID --down"
  echo "carher-$UID stopped"
done

# 3b. 逐个打包+传输（构建服务器 /tmp 空间有限，逐个执行）
for UID in 75 76; do
  echo "=== Migrating carher-$UID ==="

  scripts/jms ssh $S3_ASSET \
    "sudo tar czf /tmp/carher-${UID}-data.tar.gz \
     -C /Data/docker/volumes/carher-${UID}-data/_data ."

  scripts/jms scp $S3_ASSET:/tmp/carher-${UID}-data.tar.gz - \
    | scripts/jms scp - $BUILD_ASSET:/tmp/carher-${UID}-data.tar.gz

  PV=$(kubectl get pvc carher-${UID}-data -n carher -o jsonpath='{.spec.volumeName}')
  scripts/jms ssh $BUILD_ASSET \
    "tar xzf /tmp/carher-${UID}-data.tar.gz -C /Data/$PV/ && rm /tmp/carher-${UID}-data.tar.gz"

  scripts/jms ssh $S3_ASSET "sudo rm /tmp/carher-${UID}-data.tar.gz"

  echo "carher-$UID data transferred to /Data/$PV/"
done
```

### 4. 逐个启动 K8s + 验证

```bash
for UID in 75 76; do
  echo "=== Starting carher-$UID on K8s ==="

  curl -s -X POST -H "X-API-Key: $API_KEY" \
    https://admin.carher.net/api/instances/$UID/start

  sleep 60
  WS=$(curl -s -H "X-API-Key: $API_KEY" \
    https://admin.carher.net/api/instances/$UID | jq -r '.status.feishuWS // .feishu_ws')
  echo "carher-$UID: feishuWS=$WS"
done
```

## 关键注意事项

| 事项 | 说明 |
|------|------|
| **飞书 App ID 冲突** | 同一 App ID 不能同时有两个 WS 连接。Phase 3 先暂停 K8s（不连 WS）再停 S3，确保零冲突 |
| **打包整个数据目录** | 不要手动筛选文件。ConfigMap 覆盖挂载会处理配置文件，多余文件不影响运行 |
| **provider 统一改 litellm** | S3 上可能是 openrouter/wangsu/anthropic，迁移后统一用 litellm（自动分配虚拟 Key） |
| **prefix 保持原值** | s1/s2/s3 对应 OAuth 回调域名，必须保持一致，否则飞书 OAuth 失效 |
| **构建服务器 /tmp 空间** | 25GB 可用。每次传完一个用户就清理，避免堆积 |
| **NAS 路径映射** | PVC → PV name → `/Data/{pv-name}/`。必须从 kubectl 查 `spec.volumeName` |
| **已存在的 K8s 实例** | 如果该 ID 已有 CRD/PVC（如 her-14），需要先评估是否保留旧数据 |
| **SQLite 一致性** | Phase 3 先停容器再打包，确保无 WAL 残留。停机后 SQLite 只有主文件，解压即可用 |
| **不影响其他用户** | 每个实例独立 PVC，迁移操作不触及共享 ConfigMap 或 Operator |
| **LiteLLM Key 自动生成** | batch-import 时 provider=litellm 会自动调用 LiteLLM proxy 生成虚拟 Key，spend 从零开始 |
| **镜像版本** | 迁移后默认使用 K8s 当前线上版本，不需要从 S3 同步镜像 |

## 单用户迁移耗时预估

| 阶段 | 耗时 | 用户是否中断 | 说明 |
|------|------|-------------|------|
| Phase 1: 获取配置 | ~1 min | 否 | SSH 读 CSV |
| Phase 2: 创建 K8s 实例 | ~2 min | 否 | API 调用 + Operator reconcile |
| Phase 3: 暂停 K8s + **停 S3** | ~1 min | **⚠️ 中断开始** | API 调用 + stop 容器 |
| Phase 4: 打包数据 | ~3-8 min | 中断 | 取决于数据量（4.7GB → ~2.6GB gz） |
| Phase 5: 传输 + NAS 写入 | ~13-17 min | 中断 | 公网 SCP 2.6MB/s + VPC 内网 NFS 解压 |
| Phase 6: 启动 K8s | ~2 min | **⚠️ 中断结束** | 解除暂停 + 等待 WS 连接 |
| Phase 7: 验证 | ~3 min | 否 | 日志 + OAuth + 飞书测试 |
| **总计** | **~25-30 min/user** | **中断约 20 min** | |

## 与其他 Skill 的关系

| Skill | 何时使用 |
|-------|---------|
| `add-instances` | Phase 2 的 batch-import 详细参数说明 |
| `clone-instance-memory` | K8s 内部实例间数据克隆（不适用于 S3→K8s） |
| `carher-admin-api` | Admin API 完整参考 |
| `hot-grayscale` | 迁移后的镜像升级 |
| `check-instance-status` | Phase 7 验证的详细检查项 |

## 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| batch-import 返回 409 | K8s 上已有同 ID 的 CRD | 先 `DELETE /api/instances/{id}?purge=false` 删除旧 CRD（保留 PVC），再重新创建 |
| batch-import 返回 503 | Cloudflare token 未配置 | 修复 `carher-admin-secrets` 中的 `cloudflare-api-token`，重启 admin |
| Pod CrashLoopBackOff | 数据目录权限问题或配置冲突 | `kubectl logs carher-N -n carher -c carher` 查看错误，通常重启可解 |
| 群聊 @mention 无反应 | owner/bot_open_id 搞混（CSV 列6 vs 列10） | 日志 `non-owner ... ignoring` 确认。`PUT /api/instances/{id}` 修正 `owner` 和 `bot_open_id`，然后 restart |
| 飞书 WS 连不上 | S3 容器未停 / App Secret 错误 | 确认 S3 已停，检查 K8s Secret 中的 app_secret 是否正确 |
| OAuth 404 | Cloudflare DNS 未同步 | `curl -X POST https://admin.carher.net/api/cloudflare/sync` |
| 记忆丢失 | 数据未正确解压到 PVC | 检查 NAS 路径是否正确，重新传输 |
| `scripts/jms` permission denied | AccessKey 过期或资产权限被收回 | 见 `k8s-via-bastion` skill 的故障排查 |
| `sudo` 提示输入密码 | 堡垒机登录账号没有 NOPASSWD | 联系管理员加 sudoers，或手动进 KoKo terminal 临时执行 |
| 流式 pipe 中途断开 | Mac 上行带宽抖动 | 改"备选路径"先落 Mac 再 kubectl exec |
| NAS 路径找不到 | Pod 被调度到其他节点 | PVC volumeName 在所有节点的 /Data/ 下都可见（共享 NAS） |
