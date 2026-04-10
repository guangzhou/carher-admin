---
name: check-instance-status
description: >-
  Check the status of a specific CarHer bot (her) instance on K8s.
  Use when the user asks to check, inspect, or troubleshoot a her instance,
  or mentions a person's name + "her" + status/state/logs/health.
---

# 查看 Her 实例状态

## 前置：kubectl 隧道

本地 kubectl 通过 SSH 隧道连接阿里云 K8s API Server。
先测试连通性：`kubectl get nodes`

如果报 `connection refused`，建立隧道：

```bash
SSHPASS='5ip0krF>qazQjcvnqc' sshpass -e ssh \
  -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -p 1023 -L 16443:172.16.1.163:6443 -N root@47.84.112.136 &
```

等待 3 秒后重试 `kubectl get nodes`。

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
