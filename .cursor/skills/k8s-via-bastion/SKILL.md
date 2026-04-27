---
name: k8s-via-bastion
description: >-
  CarHer 项目通过 JumpServer 堡垒机访问阿里云 K8s 的标准方式：建立 kubectl
  隧道、在构建服务器/控制节点执行命令、文件传输。所有 SSH/SCP/kubectl
  访问都必须经过本 skill 描述的 `scripts/jms` 包装器，**禁止再使用旧的直连
  方式**（`root@47.84.112.136:1023`、`root@43.98.160.216`）。 Use when the user
  asks about kubectl tunnel, ssh to build server, file transfer to k8s, 或者
  其他 skill 引用 `scripts/jms`、`jms proxy`、堡垒机、bastion、JumpServer。
---

# K8s 访问的唯一通道：JumpServer 堡垒机

## 背景

2026-04 起公网直连 SSH 入口（`47.84.112.136:1023`、`43.98.160.216`）下线，
所有运维入口收敛到 JumpServer 堡垒机：

```
[Mac (本地)] → 公司联通 IDC 内网 (10.68.x.x) → JumpServer KoKo 网关
              (10.68.13.189:2222) → 资产 (laoyang / k8s-work-* / JSZX-AI-*)
```

公网 `jump.auto-link.com.cn:2222` **不开放**，必须走内网。Mac 只要在
公司网络里（办公室 / VPN）就能直连 `10.68.13.189`。

## 一次性配置（已完成，新机器需重建）

| 文件 | 内容 |
|------|------|
| `~/.config/jms/key.json` | JumpServer AccessKey + 内网 ssh_host=`10.68.13.189` |
| `~/.ssh/id_rsa{.pub}` | 用户 SSH 密钥（公钥已上传 JumpServer） |
| `scripts/jms` | 堡垒机操作包装器（仓库内） |
| `sshpass` | macOS: `brew install hudochenkov/sshpass/sshpass` |

`~/.config/jms/key.json` 模板：

```json
{
  "endpoint": "https://jump.auto-link.com.cn",
  "ssh_host": "10.68.13.189",
  "ssh_port": 2222,
  "key_id": "<JumpServer AccessKey ID>",
  "secret": "<JumpServer AccessKey Secret>"
}
```

权限必须 600：`chmod 600 ~/.config/jms/key.json`

## 资产对照表（必读）

| Asset 名 | 公网 IP | 内网 IP | 角色 | 用途 |
|----------|---------|---------|------|------|
| **`laoyang`** | 43.98.160.216 | 172.16.0.228 | 阿里云 ECS（**网关 / 工具节点**） | **唯一能路由到 apiserver** `172.16.1.163:6443`；自带 docker / git / python3 / cloudflared / ansible，但**没有 kubectl / nerdctl**；本地 ext4 `/Data`（不是 NAS） |
| **`k8s-work-227`** | 47.84.112.136 | 172.16.0.227 | K8s worker + 主构建机 | `/root/carher` + `/root/carher-admin` + `nerdctl` + NAS `/Data` |
| `k8s-work-226` | 47.84.112.136 | 172.16.0.226 | K8s worker + 备构建机 | `/root/carher-admin` + `nerdctl` + NAS `/Data`（无 carher 主仓） |
| `k8s-work-229` | 47.84.112.136 | 172.16.0.229 | K8s worker | `nerdctl` + NAS `/Data`（无 git 仓） |
| `JSZX-AI-01/02/03/Skills` | — | 10.68.13.186/187/188/190 | 联通 IDC 旧 Docker 主机 | S3 老用户运行环境，迁移源 |

> 旧的"216 那台"（`43.98.160.216`）= 现在的 `laoyang`。
> 老的 `root@43.98.160.216 -p 22` 公网入口已下线，登录 / 端口转发都走堡垒机。
>
> **构建镜像默认上 `k8s-work-227`**（carher 主程序 + admin 都有仓库）。
> **kubectl 隧道 / 任何需要"接入 K8s 控制面"的端口转发** 都默认走 `laoyang`。
> **laoyang 上不要直接跑 kubectl**——它没装；如要在该机上执行 kubectl，
> 改用本地 Mac + `jms proxy laoyang` 隧道（场景 1）。

## scripts/jms 速查

| 命令 | 用途 |
|------|------|
| `scripts/jms list` | 列出我有权限的全部 asset |
| `scripts/jms resolve <asset>` | 看 asset 的 UUID/account 解析 |
| `scripts/jms ssh <asset> [cmd...]` | 远程命令 / 交互式 shell |
| `scripts/jms scp <local> <asset>:/remote` | 上传文件或目录（自动 tar）|
| `scripts/jms scp <asset>:/remote <local>` | 下载文件 |
| `scripts/jms tunnel <asset> -L L:localhost:R [-f]` | ssh -L 端口转发（**目标必须是 asset 的 localhost**） |
| `scripts/jms proxy <asset> L H P` | TCP 代理：本地 L 端口 → asset 上 nc 到 H:P（**适合非 localhost 目标**） |

实现细节：每次调用先用 AccessKey 走 API 申请 5 分钟 connection-token，
再用 sshpass 注入 token 作为密码登录 `JMS-<token-id>@10.68.13.189`。
不依赖用户密码、不依赖手动续期。

> ⚠️ **`tunnel` vs `proxy`**：KoKo 的 `ssh -L` 只允许目标为 asset 的
> localhost（同机端口转发）；要 forward 到 asset 看到的其他主机
> （比如 apiserver `172.16.1.163`），必须用 `proxy` 子命令——
> 它走 SSH exec channel + 远端 `nc` 中转，KoKo 不限制。

---

## 场景 1：建立 kubectl 隧道（最常用）

本地 `~/.kube/config` 指向 `https://127.0.0.1:16443`。后台开 proxy：

```bash
nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 \
  > /tmp/jms-proxy.log 2>&1 &

sleep 2 && kubectl get nodes   # 验证
```

第一次会自动 `accept-new` host key 并申请 token。后续每次连接（kubectl
每次调用都是新连接）会自动申请新 token，不需要人工续期。

**关掉 proxy**：

```bash
pkill -f 'jms.*proxy laoyang'
```

**检查是否已经在跑**：

```bash
pgrep -af 'jms.*proxy laoyang' && echo "running" || echo "not running"
```

**多个隧道并存**（不同本地端口即可）：

```bash
nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/p1.log 2>&1 &
nohup scripts/jms proxy k8s-work-227 25432 127.0.0.1 5432 > /tmp/p2.log 2>&1 &
```

---

## 场景 1.5：直接登录 / 在 laoyang 上跑命令

旧的 `ssh root@43.98.160.216` 入口已下线，统一走堡垒机：

```bash
scripts/jms ssh laoyang                            # 交互式 shell（登上去人工调试）
scripts/jms ssh laoyang 'uname -a; uptime'         # 一次性命令
scripts/jms ssh laoyang 'docker ps'                # docker 仍然可用
scripts/jms ssh laoyang 'systemctl status cloudflared'   # cloudflared 在该机
scripts/jms ssh laoyang 'ansible-playbook /root/playbooks/xxx.yml'   # ansible
```

> ⚠️ laoyang 上**没有 kubectl**——不要在它上面跑 `kubectl get pods`。
> 任何 kubectl 需求都用 Mac 本地 + `jms proxy laoyang` 隧道（场景 1）。

文件传输同样支持：

```bash
scripts/jms scp /tmp/something.tar.gz laoyang:/Data/something.tar.gz
scripts/jms scp laoyang:/var/log/cloudflared.log ./
```

---

## 场景 2：在构建服务器执行命令

主构建机 `k8s-work-227`（有 `/root/carher` + `/root/carher-admin`）：

```bash
scripts/jms ssh k8s-work-227 'cd /root/carher && git status'

scripts/jms ssh k8s-work-227 \
  'cd /root/carher && git fetch && git checkout main && git pull'

scripts/jms ssh k8s-work-227 \
  'cd /root/carher && nerdctl build -f Dockerfile.carher -t cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:my-tag . 2>&1 | tail -50'
```

**ACR VPC 仓库地址**：`cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com`
（必须用 `-vpc` 后缀，从 K8s 节点免外网拉镜像）

**多行/复杂命令**：用 heredoc + `bash -s`：

```bash
scripts/jms ssh k8s-work-227 'bash -s' <<'EOF'
set -e
cd /root/carher
git fetch origin
git checkout fix-something
nerdctl build -t cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:fix-something .
nerdctl push cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:fix-something
EOF
```

---

## 场景 3：文件上传 / 下载

```bash
scripts/jms scp ./local-file.txt k8s-work-227:/tmp/x.txt

scripts/jms scp ./mydir k8s-work-227:/tmp/mydir-uploaded   # tar pipeline 自动处理子目录

scripts/jms scp k8s-work-227:/var/log/syslog ./syslog.local

cat data.json | scripts/jms scp - k8s-work-227:/tmp/data.json

scripts/jms scp k8s-work-227:/etc/hosts -
```

> 二进制文件已用 SHA-256 校验完整性 ✓。
> 限制：每次传输每端只能引用一个 asset（token 与 asset 绑定）。

---

## 场景 4：从老 S3 Docker 主机迁移数据

S3 主机就是 JumpServer 资产 `JSZX-AI-03`（`10.68.13.188`）。
不需要保留 `cltx@10.68.13.188 + 密码` 这种直连方式：

```bash
scripts/jms ssh JSZX-AI-03 'docker ps --filter name=carher- --format "{{.Names}}"'

scripts/jms scp JSZX-AI-03:/tmp/carher-186-data.tar.gz /tmp/

scripts/jms scp /tmp/carher-186-data.tar.gz k8s-work-227:/tmp/

scripts/jms ssh k8s-work-227 \
  'tar xzf /tmp/carher-186-data.tar.gz -C /Data/<pv-name>/'
```

---

## 场景 5：访问数据库 / Redis（非 localhost 目标）

`litellm-db` Pod 内监听 5432，但 K8s ClusterIP 服务 `litellm-db.carher.svc`
在 worker 上是 iptables 规则。要从 Mac 直连：先开 kubectl 隧道，再用
`kubectl port-forward` 或 `jms proxy` 接力。

或者直连 K8s worker（如 `k8s-work-227` 已挂载 NAS / 能 resolve 集群 DNS）：

```bash
nohup scripts/jms proxy k8s-work-227 25432 <pod-ip> 5432 > /tmp/db.log 2>&1 &
psql -h 127.0.0.1 -p 25432 -U llmproxy -d litellm
```

---

## 故障排查

### `Permission denied (password,publickey)`
- token 是单次消耗的，连续测试时每次都会重发新 token，无需手动处理
- 若反复 deny：检查 `~/.config/jms/key.json` 的 key_id/secret 是否还有效
  （JumpServer 上 → 个人信息 → 密钥管理 → AccessKey 列表）

### `Connection refused` to `10.68.13.189:2222`
- 你不在公司内网，Mac 没有走 VPN/办公网络
- 确认本地能 ping 通 10.68.x.x：`nc -vz 10.68.13.189 2222`

### `kubectl ... read: connection reset by peer`
- 用了 `jms tunnel ... -L 16443:172.16.1.163:6443` 这种**非 localhost 目标**
- KoKo 拒绝 → 必须改用 `jms proxy laoyang 16443 172.16.1.163 6443`

### `scripts/jms ssh laoyang 'kubectl ...'` 报 `kubectl: command not found`
- laoyang **没装** kubectl/helm/nerdctl，它纯粹是网关 + 工具节点
- 把 kubectl 操作搬回 Mac，前提是 `jms proxy laoyang` 起着

### `scp: dest open ...: Failure`
- KoKo 的 SFTP 子系统对 token-bound 会话只读
- 用 `scripts/jms scp`（基于 `ssh "cat > ..."` 流式），**不要**直接用 `scp` + 堡垒机

### `sftp> ls` 一片空白 / 写入失败
- 同上，KoKo SFTP 限制；用 `scripts/jms scp` 替代

### token 过期（5 分钟）
- `scripts/jms` 每次调用都是新申请新 token，对短命令完全透明
- 长 tunnel 下，连接断开后 kubectl 重连会自动用新 token——无需手动处理
- 如果是 `tunnel` 形态（持久 ssh -N -f），单次 token 会限制实际可用时长，
  此时改用 `proxy` 模式即可

---

## 严禁的旧写法（已下线，必须替换）

```bash
# 已废弃 1：直连 laoyang 做 kubectl 端口转发（216）
SSHPASS='uGTdq>hn4ps4gwivjs' sshpass -e ssh -L 16443:172.16.1.163:6443 -N root@43.98.160.216 &

# 已废弃 2：直接 SSH 登录 laoyang 跑命令（216）
ssh root@43.98.160.216 "docker ps"
ssh root@43.98.160.216 "systemctl status cloudflared"

# 已废弃 3：直连构建服务器（公网 1023）
SSHPASS='5ip0krF>qazQjcvnqc' sshpass -e ssh -p 1023 root@47.84.112.136 "cmd"

# 已废弃 4：直连 S3 老 Docker 主机（明文密码）
sshpass -p 'f{Zv30fCeqnw' ssh cltx@10.68.13.188 "cmd"
```

正确替换：

```bash
nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &

scripts/jms ssh laoyang "docker ps"
scripts/jms ssh laoyang "systemctl status cloudflared"

scripts/jms ssh k8s-work-227 "cmd"

scripts/jms ssh JSZX-AI-03 "cmd"
```
