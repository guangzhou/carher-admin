---
name: her-self-restart
description: >-
  CarHer her 实例的两条"自重启"路径运维：(1) K8s livenessProbe 自动检测
  event-loop 卡死并 SIGKILL → restartPolicy 拉新；(2) 主人在飞书 DM 说"重启你
  自己" → bot 调 self-restart skill → admin /api/instances/self/restart →
  operator 重建 Pod。Use when the user says "重启自己 / restart yourself /
  开启自动重启 / livenessProbe / self-restart / her 卡了怎么自愈 / 谁的 her
  能自动重启 / enableLivenessProbe", or when triaging a hang that should
  have auto-recovered but didn't, or when rolling out the capability to a
  new instance / batch. Distinct from `carher-her-reply-failure-triage`
  (诊断 reply 失败原因) — 本 skill 是 *机制*，那个是 *诊断*。
---

# her-self-restart：两条自重启路径

CarHer her 实例的"自重启"由两条独立路径覆盖（2026-05-20 上线，全员 224/224）：

| 触发方 | 触发条件 | 物理终点 |
|--------|----------|---------|
| **kubelet 探针** | 容器 `/healthz:18789` 连续 6×30s 不返 200 | kubelet SIGKILL → `restartPolicy: Always` 拉新 |
| **主人 DM** | 飞书消息含明确"重启 her"语义 → LLM 调 `self-restart` skill | admin 删 Pod → operator 30s 内重建 |

两条路径**共享同一个底层重启动作**——删 Pod 让 operator 重建。区别只在"谁触发"。

## 何时来本 skill

- 给新实例 / 批量开/关 livenessProbe（Stage 1）
- self-restart skill 没生效，怀疑 LLM 没听描述（Stage 2 LLM 行为问题）
- 看到大量 her 误重启，要回滚阈值或开关
- 想理解架构再决定是否给某用户开

不来本 skill：
- her reply 失败诊断 → `carher-her-reply-failure-triage`
- 真 OOM / 阿里云告警 → `her-oom-alert-triage`
- 单实例配置 override → `carher-instance-config-override`

## 架构关系图

```
HerInstance CRD (her-N)
  ├── spec.enableLivenessProbe: bool  ← Stage 1 开关，default false
  │
  └── 触发 operator reconcile (reconciler.go)
        ├── pod-spec-key 含 |lp=1 段（仅 enableLivenessProbe=true 时追加）
        └── 生成 Deployment：
              container.livenessProbe = HTTP GET /healthz:18789
                {initialDelay:180s, period:30s, timeout:10s, failures:6}

Pod (containers[0]=carher)
  ├── OpenClaw gateway 监听 :18789
  │   └── /healthz → {"ok":true,"status":"live"}（无需 auth）
  └── 因 event-loop block，HTTP server 同loop → 探针超时

NAS shared-skills (RO mount)
  └── self-restart/
        ├── SKILL.md   ← LLM 看的描述
        └── run.sh     ← curl POST admin

bot 进程 (任何 her)
  └── run.sh → POST http://carher-admin.carher.svc:8900/api/instances/self/restart
                                              ↓
admin (backend/main.py)
  └── api_self_restart(request)
        ├── 路径在 AUTH_EXEMPT_PATHS（不要 JWT）
        ├── request.client.host = 调用者 podIP
        ├── kubectl list pods label app=carher-user → 找匹配 IP 的 pod
        ├── 读 pod.labels[user-id] → caller_uid
        └── 调既有 api_restart(caller_uid) → kubectl delete pod
                                              ↓
operator (reconciler.go health.go)
  └── Pod 不在 → 30s 内重建（Deployment 兜底）
```

## Stage 1：livenessProbe（自动 hang 检测）

### CRD 字段

`HerInstance.spec.enableLivenessProbe`（bool, default `false`）。
opt-in 设计——默认不动现有实例的 pod-spec-key（向后兼容）。

operator 代码：`operator-go/internal/controller/reconciler.go`
- L160-165: `desiredPodSpecKey` 只在 `enableLivenessProbe=true` 时追加 `|lp=1`
- L580-583: Deployment 注解的 `pod-spec-key` 同步追加
- L560-578: 注入 `corev1.Probe` 到 `containers[0]`

CRD schema：`k8s/crd.yaml` L83-86 加 `enableLivenessProbe: {type: boolean, default: false}`

### 阈值含义

```
initialDelaySeconds: 180   # 冷启动 prework / plugin install 留够时间
periodSeconds: 30          # 每 30s 探一次
timeoutSeconds: 10         # 单次响应 >10s 算失败
failureThreshold: 6        # 连续 6 次失败才杀
successThreshold: 1
```

**杀容器的最快路径** = 180s 冷启动 + 6 × (30s period + 10s timeout) ≈ **180 + 240 = 420s**
**杀容器的慢路径** ≈ 已稳定后才出问题 = 6 × 30s + 5 × 10s timeout ≈ **230s**

> 调阈值前必读"踩过的 5 个坑"段第 2 条。

### 全员开

```bash
# 已开数
kubectl get herinstance -n carher -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
on = sum(1 for h in d['items'] if h['spec'].get('enableLivenessProbe'))
print(f'enabled: {on} / {len(d[\"items\"])}')"

# 一次性全开（用 xargs -P 5 并发，每个 patch 触发 reconcile + rolling）
kubectl get herinstance -n carher -o name |
  xargs -P 5 -I{} kubectl patch {} -n carher --type=merge \
    -p '{"spec":{"enableLivenessProbe":true}}'
```

零中断；Deployment `maxSurge=1, maxUnavailable=0`。实测 224 实例
~60s 收敛。监控：

```bash
kubectl get pods -n carher -l app=carher-user -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
states = {}
probed = 0
for p in d['items']:
    s = p['status'].get('phase', '?')
    if p['metadata'].get('deletionTimestamp'): s = 'Terminating'
    states[s] = states.get(s, 0) + 1
    for c in p['spec']['containers']:
        if c['name']=='carher' and c.get('livenessProbe'):
            probed += 1; break
print(f'pods total: {len(d[\"items\"])} | probed: {probed} | phases: {states}')"
```

### 单实例开/关

```bash
# 开
kubectl patch herinstance her-<uid> -n carher --type=merge \
  -p '{"spec":{"enableLivenessProbe":true}}'

# 关（pod-spec-key 失配 → 触发滚动到无 probe 版本）
kubectl patch herinstance her-<uid> -n carher --type=merge \
  -p '{"spec":{"enableLivenessProbe":false}}'
```

### 误杀回滚

如果某实例反复被 liveness 杀（看 `kubectl describe pod` 找
`Liveness probe failed` 事件），先关该实例的开关，再去查 hang 根因：

```bash
# 看 liveness 事件
kubectl get events -n carher --field-selector reason=Unhealthy --sort-by=lastTimestamp |
  grep "Liveness probe failed" | tail -20

# 看 137 (SIGKILL) 被杀的实例 + 触发源
for p in $(kubectl get pods -n carher -l app=carher-user -o jsonpath='{range .items[?(@.status.containerStatuses[0].restartCount>0)]}{.metadata.name}{"\n"}{end}'); do
  reason=$(kubectl get pod -n carher "$p" -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}{" "}{.status.containerStatuses[0].lastState.terminated.exitCode}')
  trigger=$(kubectl describe pod -n carher "$p" 2>&1 | grep "failed liveness probe\|OOMKilled" | head -1)
  echo "  $p → $reason  trigger: ${trigger:-(unclear)}"
done
```

`exitCode 137` 可能是 liveness 也可能是 OOM；用 `kubectl describe`
events 段分辨 `failed liveness probe` vs `OOMKilled`。

不要先调阈值——根因可能是 OpenClaw 真卡（reindex 死循环 / cache bloat），
该用 `her-memory-reindex-rescue` 而不是放宽探针。**正常的瞬时卡顿（10s 内
1-2 次探针失败）不会触发 kill**——只有连续 6 × 30s = 3 分钟持续不响应才会。

### 实测基线（2026-05-21 上线 8h 后）

- 探针失败警告：12 个 pod 出现过 1 次 `Liveness probe failed`（瞬时卡）
- 真被 SIGKILL：3 个 pod，其中 carher-178 在 4h 内累计 15 次失败、被杀 2 次
  → 这个实例**真的间歇性 hang >230s**，机制按预期生效
- 误杀率：0（所有 SIGKILL 都对应到 `kubectl describe` events 段里
  `failed liveness probe` 明确事件，没出现"健康但被杀"的情况）

## Stage 2：DM "重启自己"（用户主动）

### 三段组成

1. **OpenClaw skill** `self-restart`（装在 NAS shared-skills RO mount）
   - SKILL.md 描述里硬绑定触发词 + 明确说"gateway tool 报
     `commands.restart=false` 时立即 fallback 到本 skill"
   - run.sh 执行 `curl POST http://carher-admin.carher.svc:8900/api/instances/self/restart`

2. **admin 端点** `/api/instances/self/restart`（`backend/main.py`）
   - 在 `AUTH_EXEMPT_PATHS` 里（不走 JWT）
   - 鉴权：源 IP → `list pods label=app=carher-user` → 找匹配 podIP → 读
     label `user-id` → 调既有 `api_restart(caller_uid)`
   - 安全保证靠 K8s CNI 不让 pod 伪造 IP；调用者**只能重启自己**

3. **operator** 已有的 reconcile + restartPolicy（Stage 1 复用同一路径）

### 关键 admin 路由顺序

```python
# 必须放在 /api/instances/{uid}/restart 之前
@app.post("/api/instances/self/restart")
async def api_self_restart(request: Request): ...

@app.post("/api/instances/{uid}/restart")
async def api_restart(uid: int): ...
```

顺序反了 FastAPI 把 `self` 当作 uid 路径参数 → 422 int_parsing。

### NAS skill 路径

```
shared-skills PVC = carher-shared-skills (RWX NAS)
build server mount = /Data/nas-429a1fe7-c0cb-4cf4-9349-8268e39c9acb/
她 pod 看到的路径 = /data/.openclaw/skills/   (RO)
```

直接在 k8s-work-227 上写 NAS：

```bash
scripts/jms ssh k8s-work-227 'ls /Data/nas-429a1fe7-c0cb-4cf4-9349-8268e39c9acb/self-restart/'
```

更新流程：先写 staging 目录 → 原子 mv → NFS 立即对所有 her 可见
（不需要重启 her）。新一轮 LLM 对话开始时重读 metadata。

## 验证

### 手测 livenessProbe

```bash
POD=$(kubectl get pod -n carher -l user-id=<uid> -o jsonpath='{.items[0].metadata.name}')

# 直接探健康端点
kubectl -n carher exec $POD -c carher -- \
  curl -s -w "HTTP %{http_code} time=%{time_total}s\n" --max-time 10 http://127.0.0.1:18789/healthz

# 看 deployment 注解
kubectl get deployment carher-<uid> -n carher -o jsonpath='{.metadata.annotations.carher\.io/pod-spec-key}{"\n"}'
# 期待结尾有 |lp=1

# 看 container probe 字段
kubectl get pod $POD -n carher -o jsonpath='{.spec.containers[0].livenessProbe}{"\n"}'
```

### 手测 self-restart 端点

从某个 her pod 内调（zero-arg，**会真删 Pod**）：

```bash
kubectl -n carher exec carher-<uid>-... -c carher -- \
  curl -sS -X POST http://carher-admin.carher.svc:8900/api/instances/self/restart -w '\nHTTP %{http_code}\n'
# → {"id":<uid>,"action":"restarted","managed_by":"operator","note":"Pod deleted; operator will recreate within 30s"}
```

伪造测试（从 admin pod 内调，期待 403）：

```bash
ADMIN_POD=$(kubectl get pod -n carher -l app=carher-admin -o jsonpath='{.items[0].metadata.name}')
kubectl -n carher exec $ADMIN_POD -- \
  curl -sS -X POST http://localhost:8900/api/instances/self/restart -w '\nHTTP %{http_code}\n'
# → {"detail":"Source IP <ip> does not match any carher-user pod"} HTTP 403
```

### 端到端：飞书 DM

用户 DM her："你重启一下你自己"，看：

```bash
# 1. admin 收到调用
kubectl logs -n carher -l app=carher-admin --since=2m | grep "self/restart"
# 期待: POST /api/instances/self/restart HTTP/1.1 200 OK

# 2. her stdout 有 skill 触发
kubectl logs -n carher -l user-id=<uid> -c carher --since=2m |
  grep -iE "self-restart calling|admin response"

# 3. Pod 被替换
kubectl get pod -n carher -l user-id=<uid>
```

## 已经踩过的 5 个坑

### 1. `gateway restart` flag 误以为是开关

`commands.restart` 在 base-config 里设 `false` 时，飞书内置的 `gateway` tool
调 restart 报 `commands.restart=false`。**翻成 true 也没用**——它走 SIGUSR1
in-process 软重启，不换容器、PVC 脏状态不清。所以 Stage 2 不是改 flag，而是绕开
那个 tool 走 admin 端点。

### 2. operator pod-spec-key 必须有 |lp=N 段

reconciler 用 `pod-spec-key` 注解 short-circuit "需不需要 rollout"。如果
`EnableLivenessProbe` 不进 key，patch CRD 后 operator 看 key 没变 → 不 rollout
→ probe 永远不会注入。复现过：patch CRD 等了 8s 啥都没动。修复就是
`reconciler.go:160-165 + 580-583`。

**修复时不能把字段无条件追加进 key**——会让所有 224 实例 `pod-spec-key` 失配
触发全员 rollout。正确做法是 `if EnableLivenessProbe { key += "|lp=1" }`。

### 3. FastAPI 路由顺序：`self` 在 `{uid}` 之前

参考"关键 admin 路由顺序"段。把 `/self/restart` 放在 `/{uid}/restart` 之前，
否则 FastAPI 把 "self" 当 uid 解析 → 422。

### 4. shared skill 装上去不等于 LLM 会用

第一次 DM 验证时 LLM 完全无视 skill，先调 native gateway tool → 失败 → 回了一句
"重启被拦住了" 就停。**修复**：在 SKILL.md description 里加：

- 用 🚨 emoji + "唯一可用的重启路径" 拉高优先级
- 明写"看到 `commands.restart=false` 错误时 **必须立即** fallback 到本 skill"
- 列禁止调用的反例

改完第二次 DM 立刻生效。**机制：每轮对话开始时 OpenClaw 重读 skill metadata**，所以
改完不需要重启 her。

### 5. 看 SpendLogs / kubectl 不能验证"自重启是否真的会触发"

livenessProbe 是 kubelet 行为不是 LLM 行为。要看探针真的工作只能：
- 在 her pod 里手测 `/healthz` 响应正常 (200 < 10s)
- 或人为制造 hang（不推荐生产做）：
  ```bash
  kubectl -n carher exec <pod> -c carher -- bash -c \
    'kill -STOP $(pgrep -f openclaw-gateway)'
  # 等 ~230s 应该被 SIGKILL 重启
  ```
  STOP 测过会真触发。**不要**在主人的 her 上做。

## 镜像与回滚

| 组件 | 当前镜像 (2026-05-21) | 回滚 |
|------|----------------------|------|
| operator | `carher-operator:v20260521-livenessprobe-fix1` | `kubectl set image deployment/carher-operator -n carher operator=...:v20260520-f223ab2` |
| admin | `carher-admin:v20260521-self-restart-fix1` | `kubectl set image deployment/carher-admin -n carher admin=...:v20260517-a6b9785` |

回滚 admin → `/self/restart` 端点 404，但全员 her 的 SKILL.md 仍然在 NAS
（运行 run.sh 会 404 → exit 1 → bot 回错误信息）。可以同时手动删
NAS skill：

```bash
scripts/jms ssh k8s-work-227 'rm -rf /Data/nas-429a1fe7-c0cb-4cf4-9349-8268e39c9acb/self-restart'
```

回滚 operator → 新建的 her 没有 livenessProbe 注入逻辑，但已注入的
Pod 仍带 probe（kubelet 自己探，不依赖 operator）。要清就 patch CRD
`enableLivenessProbe=false` 触发 rollout。

## 一键开关脚本

见同目录 `enable.sh`：

```bash
# 给单个实例开
bash .cursor/skills/her-self-restart/enable.sh on <uid>

# 给单个实例关
bash .cursor/skills/her-self-restart/enable.sh off <uid>

# 全员开
bash .cursor/skills/her-self-restart/enable.sh on all

# 状态汇总
bash .cursor/skills/her-self-restart/enable.sh status
```
