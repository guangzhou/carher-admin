---
name: host-243-disk-ops
description: >-
  243（10.68.13.243，单节点 k3s + snap LXD，跑 myai.carher.net 和 Astrill 出口）的磁盘布局、巡检、
  以及「根盘满 ⇒ myai 挂」的排查和恢复。根盘只有 38G，会涨的东西都放在 500G 的 /Data。
  Use when: myai.carher.net 报 Cloudflare 1033 / 502、pod 全部 Pending 或 ErrImageNeverPull、
  节点 DiskPressure、「243 磁盘满了」、要往 243 导镜像、要新建或挪 LXD 容器、要动 243 的 k3s 数据目录。
---

# 243 磁盘运维

## §0 现在的布局（2026-10-18 起）

| 东西 | 在哪 | 怎么挂 |
|---|---|---|
| 根盘 `/` | `vda3` LVM 38G，**VFree=0，扩不了** | – |
| 数据盘 `/Data` | `vdb1` 500G ext4 | fstab `PARTUUID=…` |
| k3s 全部数据（containerd 镜像/快照、etcd `state.db`） | `/Data/rancher` | fstab bind → `/var/lib/rancher`，k3s drop-in `RequiresMountsFor=/var/lib/rancher` |
| myai 的 PV（postgres 19G、redis 2.8G…） | `/Data/k3s-storage/*` | hostPath PV（一直在这） |
| swap 4G | `/Data/swap.img` | fstab `nofail` |
| LXD 池 `data` | `/Data/lxd/pool` | fstab bind → `/mnt/lxd-data`，`snap.lxd.daemon` drop-in `RequiresMountsFor=/mnt/lxd-data` |
| LXD 池 `default`（还在根盘） | `/var/snap/lxd/common/lxd/storage-pools/default` | – |
| journald | 宿主上限 300M，astrill/test-lxd-2 容器内 150M | `/etc/systemd/journald.conf.d/99-size-cap.conf` |

容器所在池：astrill-4/5 在 `data`；**test-lxd-2 / astrill-2 / astrill-3（在服务）和 test-lxd（FROZEN）还在 `default`**。
所有 bind 和 swap 都带 `nofail,x-systemd.requires-mounts-for=/Data`：`/Data` 挂不上时，机器照样能起来、能 ssh，
k3s 和 LXD 不启动，而不是在空目录上起来。

## §1 脚本（全部在 `scripts/`，密码走环境变量 `HOST243_PW`，🔴 末位的单引号也是密码的一部分）

| 脚本 | 干什么 | 动不动东西 |
|---|---|---|
| `host-243-disk-check.sh` | **第 0 步**。检查用量、DiskPressure、各挂载源、RequiresMountsFor、swap、journald，以及所有 `imagePullPolicy=Never` 的镜像在不在、有没有 pin | 只读，rc=1 表示有 WARN |
| `host-243-image-import.sh <243上的/Data/…tar> [额外tag]` | 导入镜像，同时打 `io.cri-containerd.pinned`，回读确认 | 加/覆盖镜像引用，不碰 pod |
| `host-243-lxd-move-to-data.sh <容器>` | 先导出备份，再停 → 挪到 `data` 池 → 起 → 回读 | 停该容器 1~2 分钟。在服务的三个、以及 Frozen 状态的默认拒绝 |
| `host-243-k3s-datadir-move.sh` | 把 `/var/lib/rancher` 搬到数据盘（**已经做过**；再跑会因为已是挂载点而直接退出）。换机器或重装时用 | 停业务 ns，失败自动回滚 |
| `host-243-run.sh [VAR=val…] <file\|->` | 底层：把本地脚本拿到 243 上跑，里面用 `S <cmd>` 拿 root | 看 body |

```bash
export HOST243_PW='...'
scripts/host-243-disk-check.sh
```

## §2 myai 打不开时的排查顺序

1. **`host-243-disk-check.sh`**。1033 = 隧道没进程（cloudflared 没在跑），502 = 隧道通了但 dashboard 没起来
2. `DiskPressure=True` ⇒ 节点有 NoSchedule 污点，pod 全部 Pending；kubelet 还会**驱逐所有 pod（包括 cloudflared）**，
   同时跑 image GC（高水位 85%，降到 80%）。先腾空间（§3），污点会自动消失
3. pod 报 `ErrImageNeverPull` ⇒ 本地镜像被 GC 删了。`host-243-image-import.sh /Data/migration/image-*.tar`，
   导完 kubelet 会自己重试，不用重启 pod
4. 镜像在、pod 却 CrashLoop，日志是 `LockNotAvailableError ... apply_usage_schema`：
   dashboard 启动时做 DDL，`lock_timeout=3s`；此时 sync worker 正在长事务里写库 ⇒ 等它跑完，dashboard 会自己起来（10-18 实测约 5 分钟）。**别去杀 pg 连接**
5. 都绿了，health 还是 `degraded`：`organization` 403 是常驻的，`usageRealtime` 的 her 积压是 her 迁去杭州网关导致的（老问题），跟磁盘无关。
   细节走 skill `cli-anything-myai`

## §3 腾空间（按安全程度排序）

```bash
S journalctl --vacuum-size=200M                          # 宿主
S lxc exec <c> -- journalctl --vacuum-size=100M </dev/null # 容器内（10-17 时 test-lxd-2/astrill-2 各有 1~2G）
S apt-get clean
k3s kubectl -n ai-token-dashboard delete pod --field-selector=status.phase=Failed   # Evicted 的尸体
```
⛔ 别用 `crictl rmi --prune`：它会删掉本地导入、当下没被 pod 引用的镜像，`Never` 拉取的删了就拉不回。
⛔ 别把 `/Data/migration` 里的东西挪到根盘，也别删：那是恢复材料。

## §4 规则

- **往 243 导任何镜像：tar 放在 `/Data/migration/`，用 `host-243-image-import.sh` 导，并保留 tar**。
  10-17 停机 19h 的根因：根盘到 95% ⇒ image GC 删了 `ai-token-dashboard:latest` 和 `fix-9f250f8`（本地导入 + `Never`，节点外没有副本）
  ⇒ 原 `latest`（10-10 导入，`e5fe9177`）**彻底丢失**，只能从 git 重建。
  🔴 `/Data/migration/dashboard-image.tar.gz` 是 10-08 的旧版（`e580a585`），不是线上版本
- 从 git 重建镜像要**锁依赖**：直接 `pip install -r requirements.txt` 会漂（10-18 实测 certifi、pluggy 两个包变了）。
  在 188 上用线上镜像的 `pip freeze` 作 `-c` 约束文件，构建完对 `pip freeze` 和源码 sha 都做 diff
- 新建或克隆 LXD 容器：`lxc launch/copy … -s data`
- **snap 版 LXD 的挂载命名空间里看不到 `/Data`**（实测 No such file）。池路径也**不能**放在 `/var/snap/lxd/common/lxd` 下面
  （`Source path ... is within the LXD directory`）⇒ 用 `/mnt/lxd-data` 做 bind。宿主上的 bind 能传播进 snap 的命名空间（实测）。
  `lxc export` 要写到 `/var/lib/snapd/hostfs/Data/...`
- 查 LXD 容器状态**用 `lxc query /1.0/instances/<name>`**，不要用 `lxc list <name>`：后者是**前缀过滤**，
  `test-lxd` 会同时命中 `test-lxd-2`。10-18 一个守卫就因为这个失效，冻结的 test-lxd 被停掉挪走了（已挪回并重新冻结，数据无损）
- `host-243-run.sh` 的 body 是从 stdin 读的：**`lxc exec` 不加 `</dev/null` 会吃掉后面的整段脚本**，而且不报错（实测：后续的 echo 全部消失）
- 动 k3s 数据目录前必须先把业务缩到 0：`k3s-killall.sh` 对 containerd-shim 用的是 `kill -9`
- 长操作（停 k3s、rsync）**用 `sudo systemd-run --unit=… --collect` 跑**，进度写到 `/Data/migration/*.log`。
  ssh 会话断掉不能把它杀在半路（10-18 当晚就断过一次 ssh）

## §5 还没做的

- 在服务的 test-lxd-2 / astrill-2 / astrill-3 还在根盘（各约 2G）。挪池要停容器，Astrill GUI 重启后活不过来
  （见 skill `astrill-243-exit-ops` §8），需要有人看着做：`host-243-lxd-move-to-data.sh <c> --i-know-it-serves-traffic`
- 机器**没有真正重启验证过**：开机挂载依赖用 `systemctl show … -p RequiresMountsFor` 查过，但没有实际重启测过
- `/Data/migration/rancher-old-20261018`（2.7G）是迁移前 k3s 目录的完整副本，确认长期稳定后可以删

## 关联
skill `cli-anything-myai`（myai 应用层）· `astrill-243-exit-ops`（出口容器）· `litellm-198-diskpressure-502`（198 同族问题）
