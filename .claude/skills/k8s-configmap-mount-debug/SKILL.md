---
name: k8s-configmap-mount-debug
description: >-
  排查"改了 ConfigMap 但 pod 里看不到新内容"类问题：subPath 挂载的固有限制、
  kubelet 同步延迟、用 sidecar watcher + emptyDir 绕开 subPath 限制的模式、
  stat/ls 判断文件实际同步状态。Use when a ConfigMap update doesn't
  propagate to running pods, when the user mentions subPath / 挂载 / 不同步 /
  没生效 / kubectl apply 后 pod 还是旧内容.
---

# ConfigMap 挂载更新调试

## 核心事实：subPath 挂载不自动同步

K8s 官方文档（[存储卷章节](https://kubernetes.io/docs/concepts/storage/volumes/)）明确写明：

> A container using a ConfigMap as a subPath volume mount will not receive ConfigMap updates.

**本质原因**：subPath 挂载是 bind-mount 到具体文件 inode，而 kubelet 更新 ConfigMap 是创建新文件再原子 rename，bind-mount 指向的 inode 不会跟着新文件跑。

## 三种挂载方式 vs 同步行为

| 挂载方式 | K8s 同步 ConfigMap 更新？ | 典型 yaml |
|---|---|---|
| **非 subPath**（整目录挂载）| ✅ ~60s 自动同步 | `mountPath: /cfg` + volume `configMap: {name: foo}` |
| **subPath 直挂**（单文件）| ❌ **永不同步**，必须 pod 重启 | `mountPath: /app/x.yaml, subPath: x.yaml` |
| **sidecar + emptyDir**（绕开）| ✅ 热 reload（需 sidecar 搬运）| 见下方 pattern |

## Sidecar + emptyDir 热 reload 模式

Operator（`operator-go/internal/controller/reconciler.go`，搜 `reloaderScript`）就用的这个：

```
┌─────────────────────────────────────────────────────────────────┐
│ Pod                                                             │
│  ┌─────────────────────┐         ┌──────────────────────────┐   │
│  │ Sidecar             │         │ Main Container           │   │
│  │ 挂载: /config-watch │         │ 挂载: /app/config.json   │   │
│  │  (非 subPath, 自动) │         │ (subPath, 来自 emptyDir) │   │
│  │                     │         │                          │   │
│  │ watch /config-watch │  写     │                          │   │
│  │   每 5s 计算 hash   │ ────►  │ /app/config.json ← 更新  │   │
│  │   有变化 overwrite  │         │                          │   │
│  │   /merged/config... │         │                          │   │
│  └─────────────────────┘         └──────────────────────────┘   │
│         ▲                                 ▲                     │
│         │ ConfigMap auto-sync             │ subPath             │
│  ┌──────┴──────┐                   ┌──────┴──────┐              │
│  │  Volume 1   │                   │  emptyDir   │              │
│  │  ConfigMap  │                   │  merged-cfg │              │
│  └─────────────┘                   └─────────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

**关键**：sidecar 必须用 `writeFileSync`（保持 inode），不能 `rename` —— 否则 bind-mount 仍然指旧 inode。

Node.js sidecar 模板（carher operator 实际使用）：

```js
const fs=require('fs'),crypto=require('crypto');
const SRC='/config-watch/config.json', DST='/merged/config.json';
let lastHash='';
function sync(){
  try{
    const raw = fs.readFileSync(SRC,'utf8');
    const h = crypto.createHash('md5').update(raw).digest('hex').slice(0,12);
    if(h===lastHash) return;
    // 可以在这里注入 secret / 做模板替换
    fs.writeFileSync(DST, raw);   // ★ 必须 writeFileSync 保 inode
    lastHash = h;
    console.log('[reloader] synced hash='+h);
  } catch(e) {
    if(lastHash) console.error('[reloader]', e.message);
  }
}
sync(); setInterval(sync, 5000);
```

## 排查工具箱

### 1) 确认 ConfigMap 本身已更新

```bash
kubectl get cm <name> -n <ns> -o jsonpath='{.data.<key>}' | head -20
kubectl get cm <name> -n <ns> -o jsonpath='{.metadata.resourceVersion}'
# resourceVersion 变大 = 已更新
```

### 2) 确认 pod 里读到的文件

```bash
POD=<pod-name>
kubectl exec $POD -n <ns> -- cat /path/to/file | head -5
# 对比 ConfigMap 内容是否一致
```

### 3) `stat` 判断挂载类型和同步状态

```bash
kubectl exec $POD -n <ns> -- stat /path/to/file
```

读结果关键字段：

| 字段 | 含义 |
|---|---|
| `Links: 0` | subPath 挂载的典型特征（文件已被 unlink 但 bind-mount 持有） |
| `Links: 1` | 常规文件，可能是 emptyDir 或普通挂载 |
| `Modify time` | **文件内容最后修改时间** - 这是你要看的 |
| `Change time` | inode metadata 修改时间，subPath 挂载时 kubelet 尝试同步会更新 ctime 但不改内容 → Modify 和 Change 对不上就是 **subPath 阻止了内容更新** |
| `Birth time` | 文件创建时间（通常 = pod/init 时间） |

**经典 subPath 同步失败特征**：
```
Modify: 2026-04-21 01:47:41.023   ← pod 启动时的时间（老）
Change: 2026-04-21 07:48:55.516   ← ConfigMap apply 时的时间（新）
Links:  0
```

Change 新、Modify 旧 = kubelet 试图同步但 subPath 挡住了，**需要 pod 重启才能看到新内容**。

### 4) 查当前挂载类型

```bash
kubectl get pod $POD -n <ns> -o jsonpath='{.spec.containers[0].volumeMounts}' | python3 -m json.tool
```

找对应 `mountPath`：有 `subPath` 字段 = subPath 挂载；没有 = 整目录挂载。

## 决策树

```
Pod 里看不到 ConfigMap 的新内容？
  │
  ├─ kubectl get cm 看是不是 CM 真的更新了
  │    │
  │    ├─ 没更新 → kubectl apply 没成功，检查 yaml / 权限
  │    │
  │    └─ 已更新 ↓
  │
  ├─ stat 文件看 Links 和 Modify
  │    │
  │    ├─ Links: 1 + Modify 是旧的
  │    │   → kubelet 同步延迟，通常 60-120s 内会自动同步；等一下
  │    │
  │    ├─ Links: 0 + Modify 旧 + Change 新
  │    │   → subPath 挂载 + K8s 不同步 subPath
  │    │   → 选项 A: rollout restart 让新 pod 读新 CM
  │    │   → 选项 B: 引入 sidecar + emptyDir pattern
  │    │
  │    └─ Links: 1 + Modify 新
  │        → 文件确实更新了，问题不在挂载，检查应用是否 reload 配置
  │
  └─ 应用要不要主动 reload？
       有的应用（如 nginx）需要 SIGHUP，有的（如 bot）有自己的 watcher
```

## 紧急绕开

如果线上等不及改挂载架构，又必须让改动立即生效：

```bash
# 单个 pod
kubectl rollout restart deploy/<name> -n <ns>
# 批量（参考 carher-k8s-zero-downtime-rollout skill）
```

## 案例：base-config 改动没生效 (2026-04-21)

- **现象**：`kubectl apply` 更新 `carher-base-config` 后等 5+ 分钟，pod 里 `shared-config.json5` 内容依然是旧的（LiteLLM 路由没切过去）
- **诊断**：`stat` 显示 `Links: 0`、`Change time` 是 apply 时间、`Modify time` 是 pod 创建时间 → 典型 subPath 阻止同步
- **修复**：rollout restart 197 个 her deployment，新 pod 启动时 subPath 挂载的是 kubelet 最新同步的 ConfigMap 内容 → base-config 生效
- **教训**：要么接受改 base-config 就要滚动，要么引入 sidecar + emptyDir 模式（operator 已对 user-config 实现）

## 相关 skill

- Carher 实例的 user-config vs base-config 挂载差异实例 → [carher-instance-config-override](../carher-instance-config-override/SKILL.md)
- 零中断 rollout 触发新 pod 读 CM → [carher-k8s-zero-downtime-rollout](../carher-k8s-zero-downtime-rollout/SKILL.md)
