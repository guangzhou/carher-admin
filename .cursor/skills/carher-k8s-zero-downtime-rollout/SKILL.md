---
name: carher-k8s-zero-downtime-rollout
description: >-
  Carher 集群上实现"零消息丢失"滚动升级：LiteLLM Proxy 双副本模式、Her
  实例单副本 + ReadinessGate 模式、分批并行 rollout 脚本模板、preStop +
  graceful shutdown 最佳实践。Use when rolling out image upgrades, pod
  template changes, or any change that restarts pods; or when the user
  mentions "rollout / 滚动 / 升级 / 重启 / readiness / zero downtime /
  消息丢失 / 中断".
---

# 零消息丢失滚动升级

## 两种零下线机制（按服务类型选）

### 1) 多副本服务（如 LiteLLM Proxy）

依赖标准 K8s rolling update，配合 `preStop` + `graceful shutdown`：

```yaml
spec:
  replicas: 2
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0    # ★ 关键：永远保持所有副本 Ready
      maxSurge: 1          # ★ 允许 surge 1 个新 pod 才 terminate 旧
  template:
    spec:
      terminationGracePeriodSeconds: 60   # 给 in-flight 请求足够时间完成
      containers:
      - lifecycle:
          preStop:
            exec:
              command: ["sh", "-c", "sleep 15"]   # ★ 让 kube-proxy 先把 endpoint 摘掉
```

**行为**：
1. 新 pod 启动 → probe Ready
2. Service endpoint 加入新 pod（旧 pod 仍在）
3. 旧 pod 收到 SIGTERM → **preStop sleep 15s**（期间 kube-proxy 已从 endpoint 摘除，新流量不再来）
4. 15s 后 `SIGTERM` 传给主进程，uvicorn/FastAPI graceful shutdown，in-flight 请求有 ~45s（60-15）完成时间

这套是 `k8s/litellm-proxy.yaml` 的实际配置，已验证 rollout 期间 0 次 5xx。

### 2) 单副本服务 + 业务层 ReadinessGate（如 Her 实例）

每个 her 是**单副本**（replicas=1），无法靠多副本天然零中断。operator 用了 **ReadinessGate**：

```yaml
spec:
  strategy:
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  template:
    spec:
      readinessGates:
        - conditionType: carher.io/feishu-ws-ready   # ★ 除容器 ready 外的额外就绪条件
      terminationGracePeriodSeconds: 30   # 够用：ReadinessGate 保证新 pod 真 ready 才切流
      containers:
      - lifecycle:
          preStop:
            exec:
              command: ["sh", "-c", "sleep 15"]
```

**为何 Her 的 grace=30s 就够**（vs LiteLLM 的 60s）：
- LiteLLM 常有 >30s 的 streaming chat completion 在进行中，grace 要大点避免被强杀
- Her 的消息处理大多是短请求（短 HTTP 回复），旧 pod preStop 15s + 15s 内通常能处理完在途消息；即便 drain 打断，飞书侧 webhook 会 retry，消息不丢

**关键**：K8s 把"新 pod Ready"定义为 **所有容器 ready AND 所有 readinessGate=True**。operator 的 `HealthChecker` 每 30s 轮询 pod 的 `:18789/healthz`，当 `{"feishuWS":"connected"}` 时把 `carher.io/feishu-ws-ready` 这个 condition patch 为 True。

**行为**：
1. 新 pod 启动（容器 ~20s 起好）
2. 容器 ready，但 ReadinessGate 还是 False → pod 未 Ready → Service 不路由
3. HealthChecker 探测到 feishu WS 连上 → patch condition=True → **pod Ready**
4. 此时 K8s 才允许 terminate 旧 pod
5. 旧 pod preStop sleep 15 + SIGTERM + grace 30s 优雅退出

**效果**：新 pod 真正能处理消息才切流 → 用户侧 **0 感知**。实测 rollout 一次约 60-90s。

**依赖**：
- bot 必须暴露 `:18789/healthz` 返回 `{"ok":true,"feishuWS":"connected"}` —— carher 已实现
- operator 里 `HealthChecker` 必须跑起来（`deploy/carher-operator` 默认启用）

## 触发 rolling update 的几种方式

| 方式 | 命令 | 典型用途 |
|---|---|---|
| 改 image tag | `kubectl set image deploy/X -n carher <container-name>=<new-image>` | 版本升级 |
| 改 ConfigMap 后 restart | `kubectl rollout restart deploy/X -n carher` | 让 subPath 挂载的配置生效 |
| 改 pod spec apply | `kubectl apply -f <yaml>` | 改 probe / resources / volumes |

注意 `set image` 用的是**容器名**（carher Deployment 里 `litellm-proxy` 的容器叫 `litellm`，her 实例的容器叫 `carher`），不是 `app=` label。查容器名：`kubectl get deploy/X -n carher -o jsonpath='{.spec.template.spec.containers[*].name}'`

## 分批并行 rollout 脚本模板

适用：一次要滚 N 个独立 Deployment（比如 197 个 her）。

```bash
# 把要滚的 ID 写成一行一个
cat > /tmp/ids.txt <<EOF
1
2
3
...
EOF

run_batch() {
  local FILE=$1
  # 1) 并行触发 rollout restart（非阻塞）
  while read ID; do
    [ -z "$ID" ] && continue
    kubectl rollout restart deploy/carher-${ID} -n carher >/dev/null 2>&1 &
  done < $FILE
  wait
  # 2) 并行等待 rollout 完成（ReadinessGate 通过才算 done）
  while read ID; do
    [ -z "$ID" ] && continue
    kubectl rollout status deploy/carher-${ID} -n carher --timeout=240s >/dev/null 2>&1 &
  done < $FILE
  wait
  echo "$(date '+%H:%M:%S') batch done"
}

# 切分为每批 20 个（实测 2026-04-21 走过此节奏，197 台 ~18min 完成，0 次 5xx）
split -l 20 /tmp/ids.txt /tmp/batch-
for f in /tmp/batch-*; do
  run_batch $f
  sleep 60   # 批间留时间让 LiteLLM 等下游稳定
done
```

**不推荐全并行**（`xargs -P 50+` 一次滚 200 台）：会同时创建 200 个 pod + 摧毁 200 个旧 pod，对 etcd / scheduler / LiteLLM 瞬时冲击大，节省的几分钟不值得这个风险。20/批 已经很快。

## 监控 rollout 进度

```bash
# Pod 实时状态（含 age，新旧 hash 区分）
watch -n 5 'kubectl get pod -n carher -l app=litellm-proxy'

# 某 deploy 的 readinessGate 详情（单副本业务层）
kubectl get pod -n carher -l user-id=1000 \
  -o jsonpath='{range .items[*]}{.metadata.name}: gates={range .status.conditions[?(@.type=="carher.io/feishu-ws-ready")]}{.status}{end} phase={.status.phase}{"\n"}{end}'

# LiteLLM 5xx 监控（rollout 期间应当接近 0）
kubectl logs deploy/litellm-proxy -n carher --since=3m 2>/dev/null \
  | grep -oE 'HTTP/1.1" [0-9]+' | sort | uniq -c
```

## 常见陷阱

| 坑 | 症状 | 解法 |
|---|---|---|
| `maxUnavailable: 25%`（默认）| 单副本 deploy 算成 `maxUnavailable=0` 没问题；多副本会导致最多 25% 不可用 | 手动设 `maxUnavailable: 0` |
| 没有 ReadinessGate / probe 的单副本 pod | rollout 时容器刚 Running 就被当 Ready，bot 还没真 ready 就切流 → 消息丢失 | 加 ReadinessGate（operator 已支持）|
| `terminationGracePeriodSeconds: 30`（默认）| streaming 请求 >30s 被强杀 | 拉到 60-120s（LiteLLM 已拉到 60）|
| 无 `preStop` | SIGTERM 瞬间，endpoint 还没从 Service 摘掉，新请求进到正在退出的 pod 返回 connection refused | 加 `preStop sleep 15` |
| 并发 rollout 过多 | etcd / API server 卡 | 分批 + 批间 sleep |
| 同时改 `replicas` 和 `template`（会同时扩容+滚动）| 期望态难预测，pod 数和 spec 同时变 | 先改 `replicas` 等稳定，再改 `template` |

## 回滚

```bash
# 单个 deploy 回滚到上一版
kubectl rollout undo deploy/<name> -n carher
# 回滚到指定 revision
kubectl rollout history deploy/<name> -n carher
kubectl rollout undo deploy/<name> -n carher --to-revision=<N>
# 批量回滚
for ID in $(cat /tmp/ids.txt); do
  kubectl rollout undo deploy/carher-${ID} -n carher &
done; wait
```

## 案例数据（2026-04-21 实测）

| 场景 | 副本 | 结果 |
|---|---|---|
| LiteLLM Proxy `replicas:1→2` + 改 ConfigMap + rollout | 2 | 滚动 ~5 min，**0 次 5xx** |
| LiteLLM rollout restart 引入新 hook | 2 | 旧 pod preStop sleep 15 + grace 60s 优雅退出；自然流量 5xx=0 |
| 全量 197 个 her rollout restart（10 批 × 20，批间 60s）| 1×197 | **18 min** 完成，每个 pod ReadinessGate 通过后旧 pod 才 drain，用户 0 感知 |
| 单个 Her rollout restart（实测 carher-1000）| 1 | 新 pod +11s 启动、+27s WS connected → gate=True → 旧 pod 开始 drain、+60s 完成 |

## 相关 skill

- 配置变更触发 rollout 的场景 → [carher-instance-config-override](../carher-instance-config-override/SKILL.md)
- ConfigMap 挂载同步机制 → [k8s-configmap-mount-debug](../k8s-configmap-mount-debug/SKILL.md)
- LiteLLM Proxy 特定的运维 → [litellm-ops](../litellm-ops/SKILL.md)
