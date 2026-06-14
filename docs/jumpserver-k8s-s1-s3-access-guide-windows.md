# CarHer 堡垒机 / S1-S3 / K8s 访问指南（Windows 版）

本文面向使用 Windows 电脑的同事，用于访问 JumpServer 堡垒机、内网 S1/S2/S3 Docker 服务器，以及阿里云 ACK K8s。

推荐做法：**Windows 上安装 WSL2 Ubuntu，所有运维命令都在 WSL 里执行**。不要直接在 PowerShell 里硬跑 `scripts/jms`，因为它依赖 `sshpass`、OpenSSH、Linux shell、文件权限等能力，WSL 是最稳的路径。

本文不包含任何 AccessKey、密码、token、cookie、飞书 app secret、临时登录链接或 API key。

## 1. 总结

在 Windows 上的标准路径是：

```text
Windows 11/10
  -> WSL2 Ubuntu
    -> 公司 VPN / 办公网可访问 10.68.13.189:2222
      -> ~/codes/carher-admin/scripts/jms
        -> JumpServer KoKo
          -> laoyang / k8s-work-* / JSZX-AI-01/02/03
```

所有 SSH、SCP、kubectl 隧道都统一走：

```bash
cd ~/codes/carher-admin
scripts/jms list
scripts/jms ssh <asset> '<command>'
scripts/jms scp ./local-file <asset>:/tmp/local-file
scripts/jms proxy laoyang 16443 172.16.1.163 6443
```

不要使用旧公网 SSH 直连，例如：

```text
root@43.98.160.216
root@47.84.112.136 -p 1023
cltx@10.68.13.188
```

## 1.1 仓库地址和凭据获取

需要两个仓库：

| 仓库 | 地址 | 用途 |
| --- | --- | --- |
| `carher-admin` | `git@github.com:guangzhou/carher-admin.git` | Admin 后端/前端、Operator、K8s manifests、`scripts/jms` |
| `CarHer` | `git@github.com:guangzhou/CarHer.git` | CarHer 主程序、bot runtime、旧线 Compose / 镜像构建相关内容 |
| `CarHer upstream` | `git@github.com:buyitsydney/CarHer.git` | 上游参考仓，默认不要直接改 |

JumpServer AccessKey/Secret 由管理员单独发放，只写入本机 WSL 的 `~/.config/jms/key.json`，不要贴到飞书文档、群聊、PR、skill 或脚本里。

## 2. Windows 机器准备

### 2.1 安装 WSL2 Ubuntu

用管理员 PowerShell 执行：

```powershell
wsl --install -d Ubuntu
```

安装完成后重启电脑，打开 Ubuntu，创建自己的 Linux 用户。

检查 WSL 版本：

```powershell
wsl -l -v
```

期望 Ubuntu 是 `VERSION 2`。如果不是：

```powershell
wsl --set-version Ubuntu 2
```

### 2.2 在 WSL Ubuntu 里安装依赖

后续命令都在 Ubuntu 终端里执行：

```bash
sudo apt update
sudo apt install -y git python3 python3-pip openssh-client sshpass curl ca-certificates jq netcat-openbsd
```

安装 `kubectl`：

```bash
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl
sudo mv kubectl /usr/local/bin/kubectl
kubectl version --client
```

如果公司发了内部 `kubectl` 安装包，也可以按内部包安装；关键是 WSL 里能运行 `kubectl`。

### 2.3 克隆 carher-admin 仓库

```bash
mkdir -p ~/codes
cd ~/codes
git clone git@github.com:guangzhou/carher-admin.git carher-admin
git clone git@github.com:guangzhou/CarHer.git CarHer
cd ~/codes/carher-admin
```

如果仓库已经在 Windows 磁盘，例如 `C:\Users\xxx\codes\carher-admin`，WSL 里会看到 `/mnt/c/Users/xxx/codes/carher-admin`。但更推荐直接 clone 到 WSL 的 Linux 文件系统 `~/codes/carher-admin`，文件权限和执行速度更稳定。

如需配置 CarHer 上游参考仓：

```bash
cd ~/codes/CarHer
git remote add upstream git@github.com:buyitsydney/CarHer.git
git remote -v
```

### 2.4 配置 JumpServer AccessKey

在 WSL 里创建配置文件：

```bash
mkdir -p ~/.config/jms
chmod 700 ~/.config/jms
nano ~/.config/jms/key.json
```

填入管理员发放的配置，真实值不要写进文档或群消息：

```json
{
  "endpoint": "https://jump.auto-link.com.cn",
  "ssh_host": "10.68.13.189",
  "ssh_port": 2222,
  "key_id": "<JumpServer AccessKey ID>",
  "secret": "<JumpServer AccessKey Secret>"
}
```

保存后执行：

```bash
chmod 600 ~/.config/jms/key.json
```

### 2.5 准备 SSH key

如果没有 WSL 内的 SSH key：

```bash
ssh-keygen -t rsa -b 4096 -C "your-name@company"
cat ~/.ssh/id_rsa.pub
```

把公钥交给管理员上传到 JumpServer。不要上传私钥。

## 3. 网络连通性检查

Windows 必须处于公司办公网络或 VPN 内。

在 PowerShell 里检查：

```powershell
Test-NetConnection 10.68.13.189 -Port 2222
```

在 WSL Ubuntu 里检查：

```bash
nc -vz 10.68.13.189 2222
```

如果 Windows 能通、WSL 不通，通常是 VPN 客户端没有把路由透给 WSL。处理方式：

- 先重启 WSL：`wsl --shutdown`，再重新打开 Ubuntu；
- 确认 VPN 已连接后再启动 WSL；
- 仍不通时联系 IT 检查 VPN 对 WSL2 的路由支持。

## 4. 第一次验证堡垒机

在 WSL 里执行：

```bash
cd ~/codes/carher-admin
scripts/jms list
```

能看到资产列表，就说明 JumpServer AccessKey 和网络都正常。

继续验证登录：

```bash
scripts/jms ssh laoyang 'hostname; uptime'
```

## 5. 资产速查

| 资产名 | 角色 | 用途 |
| --- | --- | --- |
| `laoyang` | 网关 / 工具节点 | 默认 kubectl apiserver 代理出口、docker、cloudflared、ansible |
| `k8s-work-227` | K8s worker + 主构建机 | `/root/carher`、`/root/carher-admin`、`nerdctl`、NAS `/Data` |
| `k8s-work-226` | K8s worker + 备构建机 | `/root/carher-admin`、`nerdctl`、NAS `/Data` |
| `k8s-work-229` | K8s worker | `nerdctl`、NAS `/Data` |
| `JSZX-AI-01` | S1 老 Docker 主机 | `hermestest-*`、本地 Redis / registry / fallback nginx |
| `JSZX-AI-02` | S2 老 Docker 主机 | Dify raw stack、`dify-bootstrap`、`carher-*` 老实例 |
| `JSZX-AI-03` | S3 老 Docker 主机 | H75 参考实例、ChatGPT / Anthropic / Claude Max proxy、cloudflared |
| `JSZX-AI-Skills` | 旧 skills 相关资产 | 旧线 skills 运维参考 |

注意：

- `laoyang` 没有 `kubectl` / `nerdctl`，不要登录上去跑 `kubectl`。
- admin/operator 生产镜像只能在构建服务器上构建，不能在 Windows 本机或 Mac 本机构建。
- 每个资产都是 JumpServer 的独立资产，`scripts/jms ssh JSZX-AI-03` 不会先跳到 `laoyang`。

## 6. `scripts/jms` 常用命令

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

`proxy` 和 `tunnel` 的区别：

- 转发到资产自己的 `localhost`：可以用 `tunnel`。
- 转发到资产能访问的其他主机，例如 K8s apiserver `172.16.1.163:6443`：必须用 `proxy`。

## 7. 登录资产

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

## 8. 文件上传和下载

WSL 内文件上传：

```bash
scripts/jms scp ./local-file.txt k8s-work-227:/tmp/local-file.txt
scripts/jms scp ./my-dir JSZX-AI-03:/tmp/my-dir
```

下载到 WSL：

```bash
scripts/jms scp JSZX-AI-03:/tmp/report.txt ./report.txt
scripts/jms scp k8s-work-227:/var/log/syslog ./syslog.local
```

如果要上传 Windows 桌面文件，路径类似：

```bash
scripts/jms scp /mnt/c/Users/<你的Windows用户名>/Desktop/file.txt JSZX-AI-03:/tmp/file.txt
```

如果要把远端文件下载到 Windows 桌面：

```bash
scripts/jms scp JSZX-AI-03:/tmp/report.txt /mnt/c/Users/<你的Windows用户名>/Desktop/report.txt
```

不要直接用 Windows 的 `scp.exe` 或 WinSCP 连接 JumpServer。标准入口是 WSL 内的 `scripts/jms scp`。

## 9. K8s 访问方式

### 9.1 准备 kubeconfig

把管理员发放的 kubeconfig 放在 WSL：

```bash
mkdir -p ~/.kube
nano ~/.kube/config
chmod 600 ~/.kube/config
```

kubeconfig 里的 server 应指向：

```text
https://127.0.0.1:16443
```

### 9.2 启动 kubectl 隧道

在 WSL 里执行：

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

### 9.3 Windows 浏览器访问 port-forward

如果在 WSL 里执行：

```bash
kc port-forward svc/carher-admin 8080:80
```

一般可以直接在 Windows 浏览器打开：

```text
http://localhost:8080
```

如果打不开，先在 WSL 里验证：

```bash
curl -I http://127.0.0.1:8080
```

WSL 能访问但 Windows 浏览器不能访问时，重启 WSL 或检查 Windows 防火墙 / VPN 客户端策略。

### 9.4 K8s 操作边界

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

## 10. S1/S2/S3 Docker 服务器用法

S1/S2/S3 是旧线 Docker / Compose 运行环境，和 K8s 是双轨并行。新建和正式生命周期操作优先走 K8s + Admin；只有明确要查旧线、迁移、救援、对比参考行为时，才操作 S1/S2/S3。

### 10.1 先确认实例在哪台

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

### 10.2 常用 Docker 诊断

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

### 10.3 S3 用户配置和敏感信息

S3 用户配置 CSV：

```bash
scripts/jms ssh JSZX-AI-03 "grep '^75,' /Data/CarHer/docker/users.csv"
```

不要把 CSV 输出随手贴到群里，里面可能包含飞书 app secret。

## 11. S3 到 K8s 迁移方向

迁移只迁两类东西：

- 用户数据：`/data/.openclaw/` 下的记忆、会话、SQLite；
- 用户配置：飞书凭证、owner、bot open id、模型选择等。

镜像不迁移，K8s 使用当前线上镜像。

典型链路：

```text
S1/S2/S3 Docker host --scripts/jms--> WSL/Windows --scripts/jms--> k8s-work-227 --NAS /Data--> K8s PVC
```

迁移期间同一个飞书 App ID 不能同时被 S3 容器和 K8s Pod 连接 WS；必须按 runbook 控制暂停、停止、传输、启动、验证顺序。

## 12. Windows 常见问题

### 12.1 PowerShell 能连 VPN，但 WSL 不能访问 10.68.13.189

先执行：

```powershell
wsl --shutdown
```

重新连接 VPN，再打开 Ubuntu 测试：

```bash
nc -vz 10.68.13.189 2222
```

如果仍不通，联系 IT 检查 VPN 是否支持 WSL2 路由。

### 12.2 `Permission denied (password,publickey)`

检查：

```bash
ls -l ~/.config/jms/key.json
chmod 600 ~/.config/jms/key.json
cd ~/codes/carher-admin
scripts/jms resolve laoyang
```

如果 AccessKey 过期或资产权限被收回，联系管理员重新授权。

### 12.3 `sshpass not found`

在 WSL 里安装：

```bash
sudo apt update
sudo apt install -y sshpass
```

### 12.4 `kubectl` 卡住或拒连

检查 WSL 里本地 16443 端口是否有 proxy：

```bash
pgrep -af 'jms.*proxy laoyang' || echo "not running"
```

没有就启动：

```bash
cd ~/codes/carher-admin
nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

### 12.5 `kubectl ... read: connection reset by peer`

如果用了 `scripts/jms tunnel ... -L 16443:172.16.1.163:6443`，改成：

```bash
scripts/jms proxy laoyang 16443 172.16.1.163 6443
```

### 12.6 Windows 路径和 WSL 路径混淆

Windows 路径：

```text
C:\Users\alice\Desktop\file.txt
```

WSL 里对应：

```text
/mnt/c/Users/alice/Desktop/file.txt
```

所有 `scripts/jms scp` 命令都在 WSL 里执行，所以要使用 `/mnt/c/...` 形式。

## 13. 严禁清单

- 禁止把 AccessKey、密码、cookies、飞书 app secret、API key 写入文档、skill、PR、群消息。
- 禁止使用旧公网 SSH 直连入口。
- 禁止在 Windows 本机或 Mac 本机构建生产 admin/operator 镜像。
- 禁止在 K8s manifest 里直接引用公网镜像仓库。
- 禁止手动 `kubectl delete pod` 正在服务的 Pod；部署变更走 rolling / Admin / Operator 流程。
- 禁止用 `kubectl edit deploy carher-N` 作为正式变更方式。
- 禁止把 S1/S2/S3 旧线操作当成 K8s 操作，二者生命周期和回滚路径不同。

## 14. 新同事最小验证流程

在 WSL Ubuntu 里执行：

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
