# CarHer JumpServer / S1-S3 / K8s Access Guide

面向需要登录堡垒机、访问内网 S1/S2/S3 Docker 服务器、访问阿里云 ACK K8s 的同事。

本指南只写标准入口和安全边界，不包含任何 AccessKey、密码、token、cookie、临时登录链接或 API key。

## 1. 一句话结论

所有 SSH、SCP、kubectl 隧道都统一从 `carher-admin` 仓库的 `scripts/jms` 进入 JumpServer：

```bash
cd ~/codes/carher-admin
scripts/jms list
scripts/jms ssh <asset> '<command>'
scripts/jms scp ./local-file <asset>:/tmp/local-file
scripts/jms proxy laoyang 16443 172.16.1.163 6443
```

不要再使用旧的公网直连 SSH，例如 `root@43.98.160.216`、`root@47.84.112.136 -p 1023`、`cltx@10.68.13.188`。

## 2. 需要准备什么

新同事本机需要具备：

| 项 | 用途 | 说明 |
| --- | --- | --- |
| 公司内网 / VPN | 访问 JumpServer KoKo 网关 | KoKo 内网地址是 `10.68.13.189:2222` |
| `~/codes/carher-admin` | 使用仓库内 `scripts/jms` | 所有示例默认在这个目录执行 |
| `~/.config/jms/key.json` | JumpServer AccessKey 配置 | 文件权限必须是 `600` |
| `~/.ssh/id_rsa.pub` | 用户 SSH 公钥 | 公钥需要已上传 JumpServer |
| `sshpass` | 给 `scripts/jms` 注入短期 token | macOS 可用 `brew install hudochenkov/sshpass/sshpass` |
| `kubectl` | 本地访问 ACK | kubeconfig 指向 `https://127.0.0.1:16443` |

`~/.config/jms/key.json` 模板如下，真实值找管理员发放，禁止写入 git：

```json
{
  "endpoint": "https://jump.auto-link.com.cn",
  "ssh_host": "10.68.13.189",
  "ssh_port": 2222,
  "key_id": "<JumpServer AccessKey ID>",
  "secret": "<JumpServer AccessKey Secret>"
}
```

配置好后执行：

```bash
chmod 600 ~/.config/jms/key.json
cd ~/codes/carher-admin
scripts/jms list
```

能列出资产就说明堡垒机入口可用。

## 3. Windows 同事怎么用

Windows 机器推荐统一走 **WSL2 Ubuntu**，不要在原生 PowerShell / CMD 里直接跑本文命令。原因是本文依赖 `sshpass`、`chmod 600`、`nohup`、`pgrep`、Unix 路径和 shell 引号语义；WSL2 下最接近 Mac/Linux，排障成本最低。

### 3.1 WSL2 标准准备

在 PowerShell（管理员或普通用户均可）安装 Ubuntu：

```powershell
wsl --install -d Ubuntu
```

安装后进入 Ubuntu，准备依赖：

```bash
sudo apt update
sudo apt install -y git python3 openssh-client sshpass curl ca-certificates
```

安装 `kubectl` 可按公司内部标准包源执行；如果只是先验证堡垒机和 S1/S2/S3，`kubectl` 可以稍后再装。

克隆仓库建议放在 WSL 文件系统内，不要放在 `/mnt/c/...`：

```bash
mkdir -p ~/codes
cd ~/codes
git clone <carher-admin-repo-url> carher-admin
cd ~/codes/carher-admin
```

配置 JumpServer：

```bash
mkdir -p ~/.config/jms ~/.ssh
vi ~/.config/jms/key.json
chmod 600 ~/.config/jms/key.json
```

验证：

```bash
cd ~/codes/carher-admin
scripts/jms list
scripts/jms ssh laoyang 'hostname; uptime'
```

### 3.2 Windows 上的网络和浏览器注意

- 必须让 Windows 主机先连上公司内网 / VPN；WSL2 会复用 Windows 网络。
- 如果 `nc -vz 10.68.13.189 2222` 不通，先查 Windows VPN，不要先怀疑 JumpServer。
- WSL2 里启动 `scripts/jms proxy laoyang 16443 ...` 后，`kubectl` 也建议在 WSL2 里跑。
- 如果要从 Windows 浏览器访问 WSL2 中 `kubectl port-forward` 的 `localhost:8080`，通常新版 WSL2 可直接访问；不通时在 WSL2 里用 `curl http://127.0.0.1:8080` 先确认服务是否起来。
- 不要把 `~/.config/jms/key.json` 放到 Windows 桌面、微信、飞书聊天文件或共享盘。

### 3.3 原生 Windows 不推荐的原因

原生 PowerShell / CMD 会遇到这些差异：

- `sshpass` 不属于 Windows 标准工具；
- `chmod 600` 权限语义不同；
- heredoc、单引号、管道、后台进程写法和 Bash 不一致；
- `nohup` / `pgrep` / `pkill` 不可用；
- `scripts/jms` 虽然是 Python 脚本，但周边 SSH/SCP/token 注入流程按 Unix 工具链设计。

因此同事如果是 Windows 机器，默认口径是：**先打开 WSL2 Ubuntu，再从第 1 节开始执行本文命令**。

## 4. 资产速查

| 资产名 | 内网地址 / 主机名 | 角色 | 典型用途 |
| --- | --- | --- | --- |
| `laoyang` | `172.16.0.228` | 网关 / 工具节点 | 默认 kubectl apiserver 代理出口、docker、cloudflared、ansible |
| `k8s-work-227` | `172.16.0.227` | K8s worker + 主构建机 | `/root/carher`、`/root/carher-admin`、`nerdctl`、NAS `/Data` |
| `k8s-work-226` | `172.16.0.226` | K8s worker + 备构建机 | `/root/carher-admin`、`nerdctl`、NAS `/Data` |
| `k8s-work-229` | `172.16.0.229` | K8s worker | `nerdctl`、NAS `/Data` |
| `JSZX-AI-01` | `jszx-ai-186` / `10.68.13.186` | S1 老 Docker 主机 | `hermestest-*`、本地 Redis / registry / fallback nginx |
| `JSZX-AI-02` | `jszx-ai-187` / `10.68.13.187` | S2 老 Docker 主机 | Dify raw stack、`dify-bootstrap`、`carher-*` 老实例 |
| `JSZX-AI-03` | `jszx-ai-188` / `10.68.13.188` | S3 老 Docker 主机 | H75 参考实例、ChatGPT / Anthropic / Claude Max proxy、cloudflared |
| `JSZX-AI-Skills` | `10.68.13.190` | 旧 skills 相关资产 | 旧线 skills 运维参考 |

注意：

- `laoyang` 没有 `kubectl` / `nerdctl`，不要登录上去跑 `kubectl`。
- 构建 admin/operator 镜像默认上 `k8s-work-227`，不能在本地 Mac 构建生产镜像。
- 每个资产都是 JumpServer 的独立资产，`scripts/jms ssh JSZX-AI-03` 不会先跳到 `laoyang`。

## 5. `scripts/jms` 常用命令

| 命令 | 用途 |
| --- | --- |
| `scripts/jms list` | 列出当前账号有权限的资产 |
| `scripts/jms resolve <asset>` | 查看资产解析结果、账号、UUID |
| `scripts/jms ssh <asset>` | 登录交互式 shell |
| `scripts/jms ssh <asset> '<command>'` | 在远端执行一次性命令 |
| `scripts/jms scp <local> <asset>:/remote` | 上传文件或目录 |
| `scripts/jms scp <asset>:/remote <local>` | 下载文件 |
| `scripts/jms proxy <asset> <local-port> <remote-host> <remote-port>` | 本地端口代理到资产可访问的远端地址 |
| `scripts/jms tunnel <asset> -L L:localhost:R` | 只适合同一资产的 localhost 端口转发 |

`proxy` 和 `tunnel` 的区别很重要：

- 转发到资产自己的 `localhost`：可以用 `tunnel`。
- 转发到资产能访问的其他主机，例如 K8s apiserver `172.16.1.163:6443`：必须用 `proxy`。

## 6. 登录堡垒机资产

登录 `laoyang`：

```bash
cd ~/codes/carher-admin
scripts/jms ssh laoyang
scripts/jms ssh laoyang 'uname -a; uptime'
scripts/jms ssh laoyang 'docker ps'
scripts/jms ssh laoyang 'systemctl status cloudflared'
```

登录 K8s 构建机：

```bash
scripts/jms ssh k8s-work-227
scripts/jms ssh k8s-work-227 'cd /root/carher-admin && git status'
```

登录 S1/S2/S3：

```bash
scripts/jms ssh JSZX-AI-01 'docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'
scripts/jms ssh JSZX-AI-02 'docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'
scripts/jms ssh JSZX-AI-03 'docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'
```

复杂多行命令建议用 heredoc：

```bash
scripts/jms ssh k8s-work-227 'bash -s' <<'EOF'
set -euo pipefail
cd /root/carher-admin
git status
hostname
EOF
```

## 7. 文件上传和下载

上传文件：

```bash
scripts/jms scp ./local-file.txt k8s-work-227:/tmp/local-file.txt
scripts/jms scp ./my-dir JSZX-AI-03:/tmp/my-dir
```

下载文件：

```bash
scripts/jms scp JSZX-AI-03:/tmp/report.txt ./report.txt
scripts/jms scp k8s-work-227:/var/log/syslog ./syslog.local
```

通过标准输入 / 输出传输：

```bash
cat data.json | scripts/jms scp - k8s-work-227:/tmp/data.json
scripts/jms scp k8s-work-227:/etc/hosts -
```

不要直接用系统 `scp` 连接 JumpServer。KoKo 的 SFTP 子系统对 token 会话有限制，标准入口是 `scripts/jms scp`。

## 8. K8s 访问方式

### 8.1 启动 kubectl 隧道

本地 kubeconfig 指向 `https://127.0.0.1:16443`，先用 `laoyang` 作为默认代理出口：

```bash
cd ~/codes/carher-admin
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &

sleep 2
kubectl get nodes
```

常用 alias：

```bash
alias kc='kubectl --kubeconfig ~/.kube/config -n carher'
kc get pods
```

关闭隧道：

```bash
pkill -f 'jms.*proxy laoyang'
```

### 8.2 K8s 操作边界

| 操作 | 推荐入口 | 原因 |
| --- | --- | --- |
| 创建 / 删除实例 | Admin Web / Admin API | 会联动 SQLite、Cloudflare、灰度记录 |
| 升级实例镜像 | Admin Web / Admin API | 走 canary / wave / 健康门 |
| 修改 owner / appSecret | Admin Web / Admin API | Operator reconcile 可能覆盖直接改动 |
| 看 Pod / CRD / 日志 | `kubectl` | 诊断用途 |
| 进入 Pod 调试 | `kubectl exec` | 诊断用途 |
| 紧急 patch CRD | `kubectl` | 应急，事后必须同步回 Admin / git |
| 直接改 Deployment | 禁止作为正式变更 | Operator 会 reconcile 覆盖 |

常用 kubectl：

```bash
kc get herinstance
kc get herinstance her-75 -o yaml
kc get pods -o wide
kc logs -l app=carher-operator --tail=100
kc logs deploy/carher-admin --tail=100
kc logs deploy/carher-75 -c carher --tail=100
kc exec -it deploy/carher-75 -c carher -- sh
kc get pvc
```

Admin Web 本地端口转发：

```bash
kc port-forward svc/carher-admin 8080:80
# 浏览器访问 http://localhost:8080
```

### 8.3 K8s 构建 / 镜像规则

admin/operator 镜像只能在构建服务器上构建：

```bash
scripts/jms ssh k8s-work-227 'cd /root/carher-admin && git status'
scripts/jms ssh k8s-work-227 'cd /root/carher-admin && nerdctl --version'
```

生产镜像必须推到 ACR VPC 地址：

```text
cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com
```

禁止在 K8s Deployment / Job 中直接引用 `ghcr.io`、`docker.io` 等公网仓库。

## 9. S1/S2/S3 Docker 服务器用法

S1/S2/S3 是旧线 Docker / Compose 运行环境，和 K8s 是双轨并行。新建和正式生命周期操作优先走 K8s + Admin；只有明确要查旧线、迁移、救援、对比参考行为时，才操作 S1/S2/S3。

### 9.1 先确认实例在哪台

```bash
N=75
scripts/jms ssh JSZX-AI-01 "docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E '(^|-)${N}($|-)' || true"
scripts/jms ssh JSZX-AI-02 "docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E '(^|-)${N}($|-)' || true"
scripts/jms ssh JSZX-AI-03 "docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E '(^|-)${N}($|-)' || true"
```

容器命名差异：

| 节点 | 典型容器名 | 备注 |
| --- | --- | --- |
| S1 / `JSZX-AI-01` | `hermestest-{N}` | 也可能有少量 legacy `carher-*` |
| S2 / `JSZX-AI-02` | `carher-{N}` | S2 不用 `hermestest-{N}` 命名 |
| S3 / `JSZX-AI-03` | `hermestest-{N}` | H75 参考常见在这里 |

### 9.2 常用 Docker 诊断

```bash
ASSET=JSZX-AI-03
CONTAINER=hermestest-75

scripts/jms ssh "$ASSET" "docker ps --filter name=$CONTAINER"
scripts/jms ssh "$ASSET" "docker logs --tail 100 $CONTAINER"
scripts/jms ssh "$ASSET" "docker stats --no-stream $CONTAINER"
scripts/jms ssh "$ASSET" "docker inspect $CONTAINER --format 'image={{.Config.Image}} image_id={{.Image}}'"
```

S2 示例：

```bash
ASSET=JSZX-AI-02
CONTAINER=carher-221
scripts/jms ssh "$ASSET" "docker logs --tail 100 $CONTAINER"
```

### 9.3 S3 运行态路径

S3 上一个 runtime 实例常见由三棵目录树和两个 docker volume 组成：

```text
/Data/carher-runtime/deploy/carher-N/
  compose.runtime.yaml
  openclaw.runtime.json5

/Data/CarHer/deploy/carher-N/
  secrets.env

/Data/hermestest/deploy/carher-N/
  data-hermes/
  data-engine/

docker volumes:
  carher-N-home
  carher-N-data
```

S3 用户配置 CSV：

```bash
scripts/jms ssh JSZX-AI-03 "grep '^75,' /Data/CarHer/docker/users.csv"
```

不要把 CSV 输出随手贴到群里，里面可能包含飞书 app secret。

### 9.4 S3 / S1 / S2 救援原则

旧线实例慢、半天不回复、`model idle timeout`、session lock 超时时，先走诊断，不要一上来重启：

```bash
ASSET=JSZX-AI-03
CONTAINER=hermestest-75

scripts/jms ssh "$ASSET" "docker logs --since 24h $CONTAINER | grep -E 'All models failed|SessionWriteLockTimeoutError|model idle timeout|eventLoopDelayMaxMs' | tail -50"
scripts/jms ssh "$ASSET" "docker stats --no-stream $CONTAINER"
```

常见根因包括：

- `/data/.openclaw/memory/main.sqlite` 过大；
- FTS5 index corrupt；
- session jsonl 膨胀到几十 MB；
- event loop 被同步 IO block。

救援前必须做 integrity check 和大小评估。不要删除 `chunks` 表，它是用户记忆本体；相对安全的清理对象是旧 session 轨迹和 `embedding_cache`。

### 9.5 S3 到 K8s 迁移方向

迁移只迁两类东西：

- 用户数据：`/data/.openclaw/` 下的记忆、会话、SQLite；
- 用户配置：飞书凭证、owner、bot open id、模型选择等。

镜像不迁移，K8s 使用当前线上镜像。

典型链路：

```text
S1/S2/S3 Docker host --scripts/jms--> Mac --scripts/jms--> k8s-work-227 --NAS /Data--> K8s PVC
```

迁移期间同一个飞书 App ID 不能同时被 S3 容器和 K8s Pod 连接 WS；必须按 runbook 控制暂停、停止、传输、启动、验证顺序。

## 10. Cloudflare / DNS 边界

| 用户在哪 | DNS / Tunnel | 谁维护 |
| --- | --- | --- |
| S1/S2/S3 旧线 | `carher-s{1,2,3}` tunnel | 老线 / Docker 主机侧 |
| K8s 新线 | `carher-k8s` tunnel | Admin 自动维护 |
| S3 迁 K8s | 短时间切换 | 走 Admin 的服务等待和验证 |

禁止把旧线和 K8s 共用同一个 tunnel connector。历史上同 tunnel 多 connector 会造成部分请求 404。

## 11. 常见故障

### `Connection refused` 到 `10.68.13.189:2222`

本机不在公司内网或 VPN 未连通：

```bash
nc -vz 10.68.13.189 2222
```

Windows 同事先在 WSL2 Ubuntu 里执行这条命令；如果不通，回到 Windows 侧检查 VPN / 办公网。

### `Permission denied (password,publickey)`

检查 `~/.config/jms/key.json` 是否存在、权限是否为 `600`、AccessKey 是否有效。

### `kubectl` 卡住或拒连

大概率是本地 `16443` 没有 proxy：

```bash
pgrep -af 'jms.*proxy laoyang' || echo "not running"
nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

### `kubectl ... read: connection reset by peer`

如果用了 `scripts/jms tunnel ... -L 16443:172.16.1.163:6443`，改成 `proxy`：

```bash
scripts/jms proxy laoyang 16443 172.16.1.163 6443
```

### `scripts/jms ssh laoyang 'kubectl ...'` 报 `kubectl: command not found`

这是预期：`laoyang` 没有 kubectl。kubectl 在 Mac 本地跑，前提是 `scripts/jms proxy laoyang ...` 隧道已启动。

### 文件上传失败或 SFTP 空目录

不要直接用系统 `scp` / `sftp` 走 KoKo，改用：

```bash
scripts/jms scp ./file.txt JSZX-AI-03:/tmp/file.txt
```

## 12. 严禁清单

- 禁止把 AccessKey、密码、cookies、飞书 app secret、API key 写入文档、skill、PR、群消息。
- 禁止使用旧公网 SSH 直连入口。
- 禁止在本地 Mac 构建生产 admin/operator 镜像。
- 禁止在 K8s manifest 里直接引用公网镜像仓库。
- 禁止手动 `kubectl delete pod` 正在服务的 Pod；部署变更走 rolling / Admin / Operator 流程。
- 禁止用 `kubectl edit deploy carher-N` 作为正式变更方式。
- 禁止把 S1/S2/S3 旧线操作当成 K8s 操作，二者生命周期和回滚路径不同。
- Windows 机器禁止把 AccessKey 放在桌面、下载目录、聊天文件或共享盘；统一放 WSL2 的 `~/.config/jms/key.json`。

## 13. 对应 skill 摘要

| skill | 什么时候用 | 重点 |
| --- | --- | --- |
| `k8s-via-bastion` | 任何堡垒机、SSH、SCP、kubectl tunnel、构建机访问 | JumpServer 是唯一入口，`scripts/jms proxy` 是 kubectl 隧道标准方式 |
| `carher-k8s-ops` | 查 ACK K8s、HerInstance、Operator、Admin Pod、PVC | 区分 Admin Web 和 kubectl 边界，kubectl 主要用于诊断 |
| `s3-hermestest-memory-rescue` | S1/S2/S3 旧 Docker 实例慢、不回复、timeout | S1/S3 多为 `hermestest-N`，S2 是 `carher-N`；先诊断再救援 |
| `clone-carher-on-s3-runtime` | 明确要在 S3 旧线克隆 / 新建实例 | 非首选，新实例优先 K8s；S3 runtime 有特殊目录和端口公式 |
| `migrate-s3-to-k8s` | 从 S1/S2/S3 旧线迁移用户到 ACK | 迁移数据和配置，不迁镜像；要控制飞书 WS 冲突 |
| `upgrade-fleet-bridge` | 同时涉及 K8s 新线和 S1/S2/S3 旧线 | 先判路径，避免两个部署体系混用 |

## 14. 新同事最小验证流程

Windows 同事先打开 WSL2 Ubuntu，再执行下面命令。

```bash
cd ~/codes/carher-admin

# 1. 堡垒机权限
scripts/jms list

# 2. 能登录工具节点
scripts/jms ssh laoyang 'hostname; uptime'

# 3. 能查 S1/S2/S3 Docker
scripts/jms ssh JSZX-AI-01 'docker ps --format "{{.Names}}" | head'
scripts/jms ssh JSZX-AI-02 'docker ps --format "{{.Names}}" | head'
scripts/jms ssh JSZX-AI-03 'docker ps --format "{{.Names}}" | head'

# 4. 能打开 K8s 隧道并查询 carher namespace
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2
kubectl -n carher get pods
```

以上四步通过，就具备只读诊断入口。任何创建、删除、升级、迁移、救援操作，先确认对应 runbook 和负责人。
