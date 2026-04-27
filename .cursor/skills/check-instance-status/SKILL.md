---
name: check-instance-status
description: >-
  Check the status of a specific CarHer bot (her) instance on K8s.
  Use when the user asks to check, inspect, or troubleshoot a her instance,
  or mentions a person's name + "her" + status/state/logs/health.
---

# 查看 Her 实例状态

## 前置：kubectl 隧道

本地 kubectl 通过 JumpServer 堡垒机连接阿里云 K8s API Server。
完整说明见 `k8s-via-bastion` skill。先测试连通性：`kubectl get nodes`

如果报 `connection refused`，启动 proxy（已在跑会被 pgrep 检测到，不重复启动）：

```bash
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

## Step 1：定位实例

CRD 类型是 `her`（全称 `herinstances.carher.io`），命名空间 `carher`。

按名称模糊搜索：

```bash
kubectl get her -n carher \
  -o custom-columns='NAME:.metadata.name,DISPLAY:.spec.name,STATUS:.status.phase' \
  | grep -i "<关键字>"
```

如果不确定名字，列出全部：

```bash
kubectl get her -n carher \
  -o custom-columns='NAME:.metadata.name,DISPLAY:.spec.name,STATUS:.status.phase,WS:.status.feishuWS'
```

## Step 2：获取 CRD 详情

```bash
kubectl get her her-<ID> -n carher -o yaml
```

关键字段：

| 路径 | 含义 |
|------|------|
| `spec.name` | 显示名 |
| `spec.model` / `spec.provider` | 模型与供应商 |
| `spec.image` | 镜像 tag |
| `spec.deployGroup` | 部署组 |
| `spec.litellmKey` | LiteLLM 虚拟 key（仅 provider=litellm 时有值）。Operator 会将此值注入 Pod env `LITELLM_API_KEY`，覆盖共享 master key |
| `spec.paused` | 是否暂停 |
| `status.phase` | 运行阶段 (Running/Stopped/CrashLoopBackOff) |
| `status.feishuWS` | 飞书 WebSocket (Connected/Disconnected) |
| `status.message` | 异常信息（可能有历史残留，需结合 Pod 实际状态判断） |
| `status.restarts` | 容器重启次数 |
| `status.podIP` | Pod IP |
| `status.node` | 所在节点 |
| `status.lastHealthCheck` | 最近健康检查时间 |

## Step 3：检查 Pod

```bash
# 查找 Pod（label 是 user-id=<ID>）
kubectl get pod -n carher -l user-id=<ID> -o wide

# 详细描述（看 Events、Conditions、Readiness Gates）
kubectl describe pod <POD_NAME> -n carher | tail -60

# 资源用量
kubectl top pod <POD_NAME> -n carher
```

Pod 正常标准：
- `READY` 为 `2/2`（carher 主容器 + config-watcher sidecar）
- `STATUS` 为 `Running`
- Readiness Gate `carher.io/feishu-ws-ready = True`

## Step 4：查看日志

```bash
# 主容器最近日志
kubectl logs <POD_NAME> -n carher -c carher --tail=50

# 如果有崩溃，看上一次容器的日志
kubectl logs <POD_NAME> -n carher -c carher --previous --tail=50
```

## Step 5：检查 Service

```bash
kubectl get svc carher-<ID>-svc -n carher -o wide
```

## 状态判读

| 现象 | 说明 |
|------|------|
| Phase=Running + feishuWS=Connected + Pod 2/2 | 完全正常 |
| Phase=Running + message 含 CrashLoopBackOff | message 可能是历史残留，以 Pod 实际状态为准 |
| Phase=Running + feishuWS=Disconnected | 飞书连接异常，检查日志中 `[ws]` 相关错误 |
| Phase=Stopped + paused=true | 人工暂停，正常 |
| `LITELLM_API_KEY` env 与 CRD key 不匹配 | Operator 未 reconcile，annotate CRD 触发 reconcile |
| Pod 0/2 或 CrashLoopBackOff | 容器崩溃，用 `kubectl logs --previous` 查上次崩溃原因 |
| No Pod found | Operator 未创建 Pod，检查 `kubectl logs deploy/carher-operator -n carher` |

## 查找/设置 Owner open_id

> **关键**：飞书 open_id 是 per-app 的，同一个用户在不同飞书应用下有不同的 open_id。
> **绝对不能**用 `lark-cli` 查到的 open_id 来设置 carher 实例的 owner，因为 lark-cli 使用的是另一个飞书应用。
> 必须用该 carher 实例自己的 appId + appSecret 获取 tenant_access_token，再查用户的 open_id。

### Step 1: 获取实例的飞书应用凭据

**新建实例时**：用户直接提供了 app_id 和 app_secret，跳到 Step 2。

**已有实例**：从 K8s 或 Admin API 获取凭据：

```bash
# 方案 A：kubectl 可用
APP_ID=$(kubectl get her her-<ID> -n carher -o jsonpath='{.spec.appId}')
APP_SECRET=$(kubectl get secret carher-<ID>-secret -n carher -o jsonpath='{.data.app_secret}' | base64 -d)

# 方案 B：kubectl 不可用，通过 Admin API 获取 app_id
# （app_secret 不会在 API 响应中明文返回，需要用户提供或从 kubectl 获取）
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/instances/<ID>" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'app_id: {d.get(\"app_id\",\"N/A\")}')"
```

### Step 2: 获取 tenant_access_token

```bash
TOKEN=$(curl -s -X POST 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
  -H 'Content-Type: application/json' \
  -d "{\"app_id\":\"$APP_ID\",\"app_secret\":\"$APP_SECRET\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_access_token'])")
echo "TOKEN=$TOKEN"
```

### Step 3: 通过 user_id 查 open_id

先用 `lark-cli contact +search-user --query "姓名"` 获取用户的 `user_id`（user_id 跨应用一致），
然后用实例自己的 token 查该用户在此应用下的 open_id：

```bash
# 1. 查 user_id
lark-cli contact +search-user --query "姚鹏"
# → user_id: "debb89ae"

# 2. 用实例 token 查 per-app open_id
curl -s "https://open.feishu.cn/open-apis/contact/v3/users/debb89ae?user_id_type=user_id" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['user']['name'], d['data']['user']['open_id'])"
# → 姚鹏 ou_1d72a4547f4ae57c2dd14dc97fea430f
```

> ⚠️ lark-cli 返回的 `open_id`（如 `ou_9f18fae...`）是 lark-cli 应用的，**不是**该 carher 实例的。
> 只有 `user_id`（如 `debb89ae`）是跨应用一致的，可以拿来做第二步查询。

### Step 4: 更新 owner

多个 owner 用 `|` 分隔（参考 her-195 网络安全的her）：

```bash
# 方案 A：kubectl 可用
kubectl patch her her-<ID> -n carher --type=merge \
  -p '{"spec":{"owner":"ou_aaa|ou_bbb"}}'
kubectl delete pod -n carher -l user-id=<ID>

# 方案 B：kubectl 不可用，通过 Admin API 更新
curl -s -X PUT "https://admin.carher.net/api/instances/<ID>" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"owner":"ou_aaa|ou_bbb"}'
# Admin API 更新 owner 后 operator 会自动 reconcile，通常不需要手动重启
```

## 快速汇总模板

查完后向用户汇总以下信息：

```
实例: her-<ID> (<显示名>)
Pod:  <POD_NAME>  (<READY> <STATUS>, restarts: N)
节点: <NODE>
镜像: <IMAGE>
模型: <MODEL> (provider: <PROVIDER>)
LiteLLM Key: <已配置 / 未配置>
部署组: <DEPLOY_GROUP>
飞书WS: <Connected/Disconnected>
CPU/内存: <CPU> / <MEM>
运行时长: <AGE>
结论: <正常 / 异常描述>
```
