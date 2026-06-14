---
name: carher-windows-bastion-k8s-access
version: 1.0.0
description: >-
  Windows/WSL users access CarHer JumpServer, S1/S2/S3 Docker hosts, and ACK K8s
  through the repository `scripts/jms` wrapper. Use when a teammate is on
  Windows and needs bastion login, JMS SSH/SCP, kubectl tunnel, K8s diagnostics,
  or S1/S2/S3 Docker access.
metadata:
  requires:
    bins: ["wsl", "git", "python3", "sshpass", "kubectl", "nc"]
---

# CarHer Windows Bastion / K8s Access

> Standard workflow for Windows teammates: run all operational commands inside WSL2 Ubuntu, then use `scripts/jms` as the only JumpServer entry.

## Scope

Use this skill for Windows users who need to:

- log in to JumpServer assets such as `laoyang`, `k8s-work-227`, `JSZX-AI-01/02/03`;
- upload or download files through the bastion;
- open a local kubectl tunnel to ACK;
- inspect CarHer K8s resources;
- inspect old-line S1/S2/S3 Docker containers.

Do not use this skill for creating, deleting, upgrading, or migrating production instances. Those operations require the corresponding Admin/K8s/S3 runbook.

## Rules

- All commands run in WSL2 Ubuntu unless explicitly marked as PowerShell.
- All SSH/SCP/kubectl tunnel access goes through `{repo}/scripts/jms`.
- Do not use old direct SSH paths such as `root@43.98.160.216`, `root@47.84.112.136 -p 1023`, or `cltx@10.68.13.188`.
- Do not paste AccessKey, passwords, cookies, Feishu app secrets, API keys, or full CSV rows into docs, chats, skills, or PRs.
- Do not build production admin/operator images on Windows. Production builds happen on `k8s-work-227`.

## Repositories And Credentials

| Repo | URL | Purpose |
| --- | --- | --- |
| `carher-admin` | `git@github.com:guangzhou/carher-admin.git` | Admin backend/frontend, Operator, K8s manifests, `scripts/jms` |
| `CarHer` | `git@github.com:guangzhou/CarHer.git` | Main CarHer runtime, bot code, old-line Compose and image build references |
| `CarHer upstream` | `git@github.com:buyitsydney/CarHer.git` | Upstream reference; do not edit directly by default |

JumpServer AccessKey/Secret must be issued separately by an administrator and stored only in the user's WSL `~/.config/jms/key.json`.

## One-Time Windows Setup

1. Install WSL2 Ubuntu from administrator PowerShell:

```powershell
wsl --install -d Ubuntu
wsl -l -v
```

If Ubuntu is not version 2:

```powershell
wsl --set-version Ubuntu 2
```

2. Install WSL dependencies:

```bash
sudo apt update
sudo apt install -y git python3 python3-pip openssh-client sshpass curl ca-certificates jq netcat-openbsd
```

3. Install kubectl in WSL:

```bash
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl
sudo mv kubectl /usr/local/bin/kubectl
kubectl version --client
```

4. Clone the repo inside WSL Linux filesystem:

```bash
mkdir -p ~/codes
cd ~/codes
git clone git@github.com:guangzhou/carher-admin.git carher-admin
git clone git@github.com:guangzhou/CarHer.git CarHer
cd ~/codes/carher-admin
```

Prefer `~/codes/carher-admin` over `/mnt/c/...` for stable permissions and performance.

Optional upstream remote:

```bash
cd ~/codes/CarHer
git remote add upstream git@github.com:buyitsydney/CarHer.git
git remote -v
```

5. Configure JMS AccessKey:

```bash
mkdir -p ~/.config/jms
chmod 700 ~/.config/jms
nano ~/.config/jms/key.json
chmod 600 ~/.config/jms/key.json
```

Template:

```json
{
  "endpoint": "https://jump.auto-link.com.cn",
  "ssh_host": "10.68.13.189",
  "ssh_port": 2222,
  "key_id": "<JumpServer AccessKey ID>",
  "secret": "<JumpServer AccessKey Secret>"
}
```

6. Create or verify an SSH public key in WSL:

```bash
ssh-keygen -t rsa -b 4096 -C "your-name@company"
cat ~/.ssh/id_rsa.pub
```

Ask an administrator to upload only the public key to JumpServer.

## Connectivity Checks

From PowerShell:

```powershell
Test-NetConnection 10.68.13.189 -Port 2222
```

From WSL:

```bash
nc -vz 10.68.13.189 2222
cd ~/codes/carher-admin
scripts/jms list
scripts/jms ssh laoyang 'hostname; uptime'
```

If PowerShell can reach `10.68.13.189:2222` but WSL cannot:

```powershell
wsl --shutdown
```

Reconnect VPN, reopen Ubuntu, then retry `nc -vz 10.68.13.189 2222`.

## Asset Map

| Asset | Role | Typical use |
| --- | --- | --- |
| `laoyang` | Gateway / tools node | Default kubectl apiserver proxy, docker, cloudflared, ansible |
| `k8s-work-227` | K8s worker + main build host | `/root/carher`, `/root/carher-admin`, `nerdctl`, NAS `/Data` |
| `k8s-work-226` | K8s worker + backup build host | `/root/carher-admin`, `nerdctl`, NAS `/Data` |
| `k8s-work-229` | K8s worker | `nerdctl`, NAS `/Data` |
| `JSZX-AI-01` | S1 old Docker host | `hermestest-*`, Redis, local registry, fallback nginx |
| `JSZX-AI-02` | S2 old Docker host | Dify raw stack, `dify-bootstrap`, `carher-*` containers |
| `JSZX-AI-03` | S3 old Docker host | H75 reference, proxy shards, cloudflared |

Important:

- `laoyang` does not have `kubectl` or `nerdctl`; run kubectl locally in WSL through the proxy.
- Each asset is a direct JumpServer target; `scripts/jms ssh JSZX-AI-03` does not jump through `laoyang`.

## JMS Commands

```bash
scripts/jms list
scripts/jms resolve <asset>
scripts/jms ssh <asset>
scripts/jms ssh <asset> '<command>'
scripts/jms scp <local> <asset>:/remote
scripts/jms scp <asset>:/remote <local>
scripts/jms proxy <asset> <local-port> <remote-host> <remote-port>
```

Use `proxy` when the target is not the asset's own localhost, for example ACK apiserver `172.16.1.163:6443`.

## K8s Tunnel

Ensure WSL kubeconfig points to:

```text
https://127.0.0.1:16443
```

Start the tunnel:

```bash
cd ~/codes/carher-admin
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &

sleep 2
kubectl get nodes
```

Use a namespace-scoped alias:

```bash
alias kc='kubectl --kubeconfig ~/.kube/config -n carher'
kc get pods
```

Stop the tunnel:

```bash
pkill -f 'jms.*proxy laoyang'
```

## K8s Diagnostic Commands

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

K8s boundary:

- Create/delete/upgrade instances through Admin Web or Admin API.
- Use kubectl for diagnosis and emergency CRD patches only.
- Do not treat `kubectl edit deploy carher-N` as a formal change path; Operator reconcile may overwrite it.

## Windows Browser With WSL Port-Forward

From WSL:

```bash
kc port-forward svc/carher-admin 8080:80
```

Open in Windows browser:

```text
http://localhost:8080
```

If Windows browser cannot access it, verify inside WSL:

```bash
curl -I http://127.0.0.1:8080
```

Then restart WSL or check firewall/VPN policy.

## S1 / S2 / S3 Docker Checks

Find where an instance lives:

```bash
N=75
scripts/jms ssh JSZX-AI-01 "docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E '(^|-)${N}($|-)' || true"
scripts/jms ssh JSZX-AI-02 "docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E '(^|-)${N}($|-)' || true"
scripts/jms ssh JSZX-AI-03 "docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E '(^|-)${N}($|-)' || true"
```

Container naming:

| Host | Common container name |
| --- | --- |
| S1 / `JSZX-AI-01` | `hermestest-{N}` |
| S2 / `JSZX-AI-02` | `carher-{N}` |
| S3 / `JSZX-AI-03` | `hermestest-{N}` |

Inspect a container:

```bash
ASSET=JSZX-AI-03
CONTAINER=hermestest-75

scripts/jms ssh "$ASSET" "docker ps --filter name=$CONTAINER"
scripts/jms ssh "$ASSET" "docker logs --tail 100 $CONTAINER"
scripts/jms ssh "$ASSET" "docker stats --no-stream $CONTAINER"
scripts/jms ssh "$ASSET" "docker inspect $CONTAINER --format 'image={{.Config.Image}} image_id={{.Image}}'"
```

## File Transfer

From WSL:

```bash
scripts/jms scp ./local-file.txt k8s-work-227:/tmp/local-file.txt
scripts/jms scp JSZX-AI-03:/tmp/report.txt ./report.txt
```

Windows Desktop paths are under `/mnt/c`:

```bash
scripts/jms scp /mnt/c/Users/<WindowsUser>/Desktop/file.txt JSZX-AI-03:/tmp/file.txt
scripts/jms scp JSZX-AI-03:/tmp/report.txt /mnt/c/Users/<WindowsUser>/Desktop/report.txt
```

Do not use Windows `scp.exe` or WinSCP directly against JumpServer for this workflow.

## Troubleshooting

- **WSL cannot reach KoKo but PowerShell can**: run `wsl --shutdown`, reconnect VPN, reopen Ubuntu.
- **`sshpass not found`**: run `sudo apt install -y sshpass`.
- **`Permission denied (password,publickey)`**: check `~/.config/jms/key.json` permissions, AccessKey validity, and asset permissions.
- **kubectl hangs**: check `pgrep -af 'jms.*proxy laoyang'`; start the proxy if missing.
- **`read: connection reset by peer`**: replace `scripts/jms tunnel ...172.16.1.163:6443` with `scripts/jms proxy laoyang 16443 172.16.1.163 6443`.
- **Windows path fails in WSL**: convert `C:\Users\alice\Desktop\x` to `/mnt/c/Users/alice/Desktop/x`.

## Minimal Validation

Run from WSL:

```bash
cd ~/codes/carher-admin
scripts/jms list
scripts/jms ssh laoyang 'hostname; uptime'
scripts/jms ssh JSZX-AI-01 'docker ps --format "{{.Names}}" | head'
scripts/jms ssh JSZX-AI-02 'docker ps --format "{{.Names}}" | head'
scripts/jms ssh JSZX-AI-03 'docker ps --format "{{.Names}}" | head'
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2
kubectl -n carher get pods
```

Passing these checks means the teammate has read-only diagnostic access. Any write, rollout, migration, or rescue action still needs the dedicated runbook and owner approval.
