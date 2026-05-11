---
name: k8s-build-buildkit-config
description: |
  k8s-work-227 上 buildkitd 的配置约束 + 故障历史 + 操作命令。Use when the
  user is debugging slow / hung / failing builds on the K8s build node, asks
  about buildkitd config, futex_wait_queue / NFS / BoltDB / flock issues, or
  is tempted to move buildkit data root back to NAS. Includes the 2026-05-11
  NFS+BoltDB deadlock root cause + why root MUST stay on local NVMe.
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
