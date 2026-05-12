---
name: k8s-build-buildkit-config
description: |
  k8s-work-227 (K8s build 节点) 的两个主用途：
  (1) buildkitd 的配置约束 + NFS+BoltDB 死锁规避；
  (2) 直接 mount 全部阿里云 NAS root 在 /Data，可作为"NAS jumphost"绕过
      kubectl cp + 临时 pod 写任意 PVC 数据（shared-skills / dept-skills /
      per-her user-data 全部直接可写）。
  Use when the user is debugging slow / hung / failing builds (futex_wait_queue
  / NFS / BoltDB / flock / 移 root 回 NAS 的诱惑); OR needs to write to a K8s
  PVC backed by Aliyun NAS (skills 同步、给某个 her user-data 改文件、批量
  rsync 到所有 her PVC) and wants to avoid kubectl cp + temp pod
  (which routes through K8s API tunnel and is unreliable).
---

# K8s Build 节点（k8s-work-227）— buildkit config

## TL;DR

- **buildkit 跑在 systemd unit `buildkit.service` 上**（不是 docker / nerdctl 容器）
- **data root 必须在本地 NVMe SSD**：`/var/lib/buildkit-local`
- **绝对不要改回 NAS**（`/Data/buildkit`）— NFSv3 + BoltDB flock 会死锁，build 卡 `futex_wait_queue` 几十分钟无进展
- 备份 `/etc/buildkit/buildkitd.toml.nas` 是历史证据，**不要 restore**

## 当前配置（k8s-work-227, 2026-05-11 起）

```toml
# /etc/buildkit/buildkitd.toml
# TEMP: local SSD root to bypass NFS futex deadlock (set by 2026-05-11 carher-1000 upgrade)
root = "/var/lib/buildkit-local"

[worker.containerd]
  enabled = false

[worker.oci]
  enabled = true
```

**说明**：comment 里写的"TEMP"其实是永久状态——除非 BoltDB 不再用 flock 或 NFS 改成 NFSv4 + 真锁，否则这就是稳定方案。

## 文件清单

| 路径 | 用途 |
|---|---|
| `/etc/systemd/system/buildkit.service` | systemd unit 定义 |
| `/usr/local/bin/buildkitd` | binary（不是 docker / nerdctl 部署） |
| `/etc/buildkit/buildkitd.toml` | **当前 config**（root=本地 SSD） |
| `/etc/buildkit/buildkitd.toml.nas` | 旧 NAS 配置备份（root=`/Data/buildkit`），**不要 restore** |
| `/etc/buildkit/buildkitd.toml.bak.<ts>` | 自动备份 |
| `/var/lib/buildkit-local/` | 当前 data root（本地 NVMe） |
| `/Data/buildkit/` | 旧 NAS data root，残留可清空 |

## 故障历史 — 2026-05-11 NFS+BoltDB 死锁

### 症状

升级 carher-1000 时跑 `buildctl ... build`，build 卡几十分钟无进展。

```
$ pgrep -af buildctl
3753934 /usr/local/bin/buildctl --addr=unix:///run/buildkit/buildkitd.sock build ...
```

进程在但 stuck，strace 看到 `futex_wait_queue`。kill 重跑也一样。

### 走过的弯路

误以为是另一个并发 build 抢锁。kill 掉所有 buildctl + 重启 buildkitd 没用，新 build 还是卡。

### 真因

buildkit data root 配在 NFSv3 mount（`/Data/buildkit`，挂载选项是 `nolock + local_lock=all`）。BoltDB 用 `flock()` 写 metadata，**NFSv3 + nolock + flock = 不可调和**：
- nolock 让 NFS client 不去问 server 锁
- BoltDB 假设 flock() 实际是有效的 file lock
- 结果是 BoltDB 拿到一个永远不会释放的"锁"，下次写时 futex_wait_queue 死等

不光 buildkit，任何用 BoltDB（etcd / containerd metadata / 部分 K8s 组件）放在 NFS 上都会撞这个。

### 修复

```bash
# 1. 备份原 config
sudo cp /etc/buildkit/buildkitd.toml /etc/buildkit/buildkitd.toml.nas

# 2. 改 root 到本地
sudo tee /etc/buildkit/buildkitd.toml <<'EOF'
# TEMP: local SSD root to bypass NFS futex deadlock (set by 2026-05-11 carher-1000 upgrade)
root = "/var/lib/buildkit-local"

[worker.containerd]
  enabled = false

[worker.oci]
  enabled = true
EOF

# 3. 创建 root 目录
sudo mkdir -p /var/lib/buildkit-local

# 4. 重启
sudo systemctl restart buildkit
sudo systemctl status buildkit
```

之后 build 立刻畅通。

## 常用操作

### 查 buildkitd 状态

```bash
jms ssh k8s-work-227 'systemctl status buildkit --no-pager | head -10'
jms ssh k8s-work-227 'pgrep -af buildkitd'
jms ssh k8s-work-227 'ls -la /etc/buildkit/'
```

### 看 data root 用量

```bash
jms ssh k8s-work-227 'df -h /var/lib/buildkit-local; du -sh /var/lib/buildkit-local'
```

NVMe 是 `/dev/nvme0n1p3` 99G 物理盘共享。Build 多了会爆——定期清 cache。

### 清 cache（释放磁盘）

```bash
jms ssh k8s-work-227 'sudo buildctl --addr=unix:///run/buildkit/buildkitd.sock prune --keep-storage 20000'
# keep-storage 是 MB，留 20GB
```

或者更激进：

```bash
jms ssh k8s-work-227 'sudo buildctl --addr=unix:///run/buildkit/buildkitd.sock prune --all'
# 清光，下次 build 全冷
```

### 看 buildkitd 日志

```bash
jms ssh k8s-work-227 'sudo journalctl -u buildkit -n 200 --no-pager'
# 跟随
jms ssh k8s-work-227 'sudo journalctl -u buildkit -f'
```

### 重启 buildkitd

```bash
jms ssh k8s-work-227 'sudo systemctl restart buildkit && sudo systemctl status buildkit --no-pager | head'
```

正常 5 秒内 ready。重启不会丢现有 cache（cache 在 root 目录里，systemd 重启不动文件）。

## 故障识别快速 checklist

如果 build 卡死、慢得离谱、build 失败：

1. 看 `pgrep -af buildctl` 是否有 stuck 进程
2. `ps -p <pid> -o stat,wchan` 看 wait channel；`futex_wait_queue` = 锁死
3. 看 `lsof -p <pid>` 是不是 hold 着 NFS 上的文件
4. 看 `cat /proc/mounts | grep -E "buildkit|Data"` 确认 root 在哪
5. **检查 `/etc/buildkit/buildkitd.toml` 的 `root` 字段** — **如果指向 `/Data/...` 立刻改回 `/var/lib/buildkit-local`**

## 回滚（不应该回，但万一）

如果有人坚持要回 NAS（**不推荐**）：

```bash
# 1. 备份当前
jms ssh k8s-work-227 'sudo cp /etc/buildkit/buildkitd.toml /etc/buildkit/buildkitd.toml.local'
# 2. 用 NAS 备份覆盖
jms ssh k8s-work-227 'sudo cp /etc/buildkit/buildkitd.toml.nas /etc/buildkit/buildkitd.toml'
# 3. 重启
jms ssh k8s-work-227 'sudo systemctl restart buildkit'
```

**警告**：第一次 build 大概率立刻死锁。这就是为什么"绝对不要回 NAS"的来源。

## 未来改进方向（不紧急）

- **监控**：把 `pgrep buildctl` 时长 + buildkit data root 用量加 Prometheus exporter
- **定期 prune**：cron 每周 `buildctl prune --keep-storage 30000` 防爆盘
- **NAS 改 NFSv4 + 真锁**：如果哪天 NFS 改成 v4 + lockd 跑起来，理论可以回 NAS（但要严格验证 BoltDB flock 行为）
- **container 化 buildkit**：跑成 K8s DaemonSet，配 emptyDir 当 root（仍然是本地盘），可以做多节点 build

## 不要做

- ❌ 不要把 root 改回 NAS（NFSv3 + BoltDB flock 死锁）
- ❌ 不要 `rm -rf /etc/buildkit/buildkitd.toml.nas`（历史证据）
- ❌ 不要把 buildkit 跑成 docker / nerdctl 容器+本地 SSD bind mount（systemd 已经稳定，没必要）
- ❌ 不要把 buildkit 装到非 build 节点（其他 worker 没准备这个 binary 和 config）

---

# Build 节点 = K8s NAS Jumphost（同节点的第二个用途）

## 关键事实

K8s build 节点 `k8s-work-227` 因为是 K8s worker，已经把阿里云 NAS server **整个 root** mount 到了 `/Data/`：

```
7e9554b917-wjh53.ap-southeast-1.nas.aliyuncs.com:/  →  /Data/   (NFSv3, rw)
```

每个 K8s PVC 对应 NAS 上的一个**子目录**（命名是 `nas-<uuid>`），所以**所有 PVC 数据都直接可读可写**：

| PVC | NAS 子目录 | build 节点直连路径 |
|---|---|---|
| `carher-shared-skills` | `nas-429a1fe7-c0cb-4cf4-9349-8268e39c9acb` | `/Data/nas-429a1fe7-c0cb-4cf4-9349-8268e39c9acb/` |
| `carher-dept-skills` | `nas-cfa26fb2-f559-4bee-8cc3-555f2bc5c981` | `/Data/nas-cfa26fb2-f559-4bee-8cc3-555f2bc5c981/` |
| `carher-shared-sessions` | `nas-2246fe6c-80ab-4845-b306-b7a7f03616d5` | `/Data/nas-2246fe6c-80ab-4845-b306-b7a7f03616d5/` |
| `carher-N-data` (per-her) | `nas-<each-pvc>-<uuid>` | `kubectl get pv $(kubectl -n carher get pvc carher-N-data -o jsonpath='{.spec.volumeName}') -o jsonpath='{.spec.csi.volumeAttributes.path}'` 拼到 `/Data/` 后面 |

## 何时用 NAS jumphost 模式

| 场景 | 走 NAS jumphost | 走 kubectl cp + temp pod |
|---|---|---|
| 给 carher-shared-skills 加新 skill（fleet 全员立即看到） | ✅ 直接写 NAS 子目录 | ❌ 多此一举 |
| 修复某个 her 的 PVC 数据（误删文件、修 corrupt SQLite） | ✅ 直接写 her 的 NAS 子目录 | ⚠️ 可，但不稳 |
| 大批量同步（>50MB / 上百 PVC） | ✅ 必走，避开 K8s API tunnel | ❌ kubectl cp 单文件慢且断 |
| 需要 strict atomicity（写完后 pod 立即看到） | ✅ NFS 是实时的，无 cache 延迟 | ✅ 也实时 |
| 操作不存在的 PVC（PV 还没创建） | ❌ NAS 子目录还没建 | ❌ 一样不行 |

## 为什么这条路比 kubectl cp 稳

`kubectl cp` 经过：
```
mac → jms tunnel → laoyang → K8s API server → kubelet exec → pod tar receive
```

而 NAS jumphost 经过：
```
mac → jms tunnel → k8s-work-227 (跳板) → 直接 NFS 写
```

少了 **K8s API server** 这一段（今天最不稳定的一段），少了 **temp pod 启动 + image pull + tar over exec stream** 这些 fail point。

## 操作模板

### 写 shared-skills（fleet 全员立即看到）

```bash
# 1. tar 源（mac 或 S1）
jms ssh JSZX-AI-01 'cd /home/cltx/.openclaw/skills && tar czf /tmp/skills.tar.gz <skill-1> <skill-2>'

# 2. scp 到 build 节点
jms scp JSZX-AI-01:/tmp/skills.tar.gz /tmp/  # 经过 mac，但 80KB 级别秒级
jms scp /tmp/skills.tar.gz k8s-work-227:/tmp/

# 3. 直接在 NAS 子目录解压
jms ssh k8s-work-227 'cd /Data/nas-429a1fe7-c0cb-4cf4-9349-8268e39c9acb && tar xzf /tmp/skills.tar.gz && ls'

# 4. 验证 her pod 立即看到（NFS 实时）
POD=$(kubectl -n carher get pods -l user-id=1000 --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
kubectl -n carher exec $POD -c carher -- ls /data/.openclaw/skills/

# 5. 清理
jms ssh k8s-work-227 'rm /tmp/skills.tar.gz'
rm /tmp/skills.tar.gz
```

### 修复某个 her 的 user-data PVC

```bash
# 1. 找到 her 的 PV → NAS 子目录
PV=$(kubectl -n carher get pvc carher-66-data -o jsonpath='{.spec.volumeName}')
NAS_SUB=$(kubectl get pv $PV -o jsonpath='{.spec.csi.volumeAttributes.path}')
echo "NAS path: /Data$NAS_SUB"

# 2. ssh build 节点直接操作
jms ssh k8s-work-227 "ls /Data$NAS_SUB"

# 3. 改 / 加 / 删，her pod 立即看到（NFS 实时）
```

## 注意事项

- **写文件用 root 身份**：build 节点上是 root 写，文件 owner 是 root；如果 her container 用非 root 身份运行，**ro mount 不影响读**，但**写时会权限拒绝**——大多数共享 skill 是 ro mount 给 her，pod 不需要写
- **NAS 不锁**（`local_lock=all`）：不要在 NAS 上跑 BoltDB / etcd / sqlite-with-flock 等需要真锁的应用——会跟 buildkit 一样死锁
- **不要 `rm -rf /Data/<wrong-path>`**：这是 fleet 共享 NAS，rm 错路径影响 200+ her。每次操作前先 `ls -la` 看清楚
- **build 节点磁盘**和这个 NAS root 是**独立**的：写 NAS 不占 build 节点本地 SSD（buildkit 用本地 SSD）

## 不要做（NAS jumphost 部分）

- ❌ 不要在 build 节点 `chmod -R` / `chown -R` 整个 NAS 子目录（per-her PVC 有特定 uid/gid）
- ❌ 不要在 NAS 上跑 BoltDB / sqlite-with-flock（参考 buildkit 死锁教训）
- ❌ 不要把 build 节点用作长期数据存储——它是 jumphost，操作完即走
