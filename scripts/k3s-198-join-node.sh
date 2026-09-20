#!/bin/bash
# k3s-198-join-node.sh — 把一台新内网机器加入 198 K3s 集群（离线方式，无需外网）
#
# 在 Mac 上跑（编排机）。实证来源：2026-08-25 加入 10.68.13.242（aiyjy-litellm-242）。
#
# 关键坑（每条都实际踩过/规避过）：
# 1. 新机 hostname 可能与 master 撞名（242 出厂 hostname 就是 AIYJY-litellm），
#    k3s 节点名小写化后会顶掉 master —— 必须显式 --node-name，禁止依赖 hostname。
# 2. 内网机器普遍无 GitHub 出口（get.k3s.io 000），k3s 二进制从 198 scp 中转，
#    sha256 校验一致才算搬运成功（保证与集群同版本 v1.30.4+k3s1）。
# 3. 198 的 k3s data-dir 是 /Data/rancher（非默认路径），token 在
#    /Data/rancher/server/node-token，不在 /var/lib/rancher/。
# 4. 新节点必须带独立 NoSchedule taint 加入（dedicated=new-node），否则调度器
#    可能立刻把 prod pod 排上未验证节点，违反零中断纪律。
# 5. registries.yaml 照抄 225 standby：127.0.0.1:5000 → http://10.68.13.198:5000
#    （本地 registry），docker.io 等走 daocloud 镜像源（内网机器可达）。
#
# 用法：
#   P198='<198密码>' NEW_PASS='<新机密码>' ./k3s-198-join-node.sh <new_ip> [node_name]
#   # node_name 缺省 aiyjy-litellm-<ip末段>
#   # 密码见 memory: reference_198_direct_ssh / project_198_k3s_node_242_joined
set -euo pipefail

NEW_IP=${1:?usage: NEW_IP required}
NODE_NAME=${2:-aiyjy-litellm-${NEW_IP##*.}}
: "${P198:?env P198 (198 cltx password) required}"
: "${NEW_PASS:?env NEW_PASS (new host cltx password) required}"
M198=10.68.13.198
SSH198() { sshpass -p "$P198" ssh -o StrictHostKeyChecking=no cltx@$M198 "$@"; }
SSHNEW() { sshpass -p "$NEW_PASS" ssh -o StrictHostKeyChecking=no cltx@$NEW_IP "$@"; }

echo "== [1/6] 新机预检（sudo / 6443连通 / /Data盘 / daocloud+本地registry可达）=="
SSHNEW 'echo "'"$NEW_PASS"'" | sudo -S true && echo SUDO_OK
  timeout 5 bash -c "echo > /dev/tcp/10.68.13.198/6443" && echo PORT6443_OK
  df -h /Data | tail -1 || echo "WARN: 无/Data盘,将落系统盘"
  curl -s -o /dev/null -w "daocloud=%{http_code}\n" --max-time 8 https://docker.m.daocloud.io/v2/
  curl -s -o /dev/null -w "registry5000=%{http_code}\n" --max-time 5 http://10.68.13.198:5000/v2/
  echo "hostname=$(hostname)  # 若与 aiyjy-litellm 撞名,靠 --node-name 规避,已内置"'

echo "== [2/6] 中转 k3s 二进制 198→Mac→新机 + sha256 校验 =="
sshpass -p "$P198" scp -o StrictHostKeyChecking=no cltx@$M198:/usr/local/bin/k3s /tmp/k3s-relay
SUM=$(shasum -a 256 /tmp/k3s-relay | awk '{print $1}')
sshpass -p "$NEW_PASS" scp -o StrictHostKeyChecking=no /tmp/k3s-relay cltx@$NEW_IP:/tmp/k3s
SSHNEW "sha256sum /tmp/k3s | grep -q $SUM && echo SHA256_MATCH || { echo SHA256_MISMATCH; exit 1; }"

echo "== [3/6] 取 join token（data-dir=/Data/rancher，非默认路径）=="
TOKEN=$(SSH198 'echo "'"$P198"'" | sudo -S cat /Data/rancher/server/node-token' | tail -1)
[ -n "$TOKEN" ] || { echo "token 为空"; exit 1; }

echo "== [4/6] 写配置 + 起 k3s-agent（unit 照抄 225 standby 模式）=="
SSHNEW 'echo "'"$NEW_PASS"'" | sudo -S bash -s' <<EOF
set -e
install -m 755 /tmp/k3s /usr/local/bin/k3s
mkdir -p /etc/rancher/k3s /Data/rancher
cat > /etc/rancher/k3s/registries.yaml <<'REG'
mirrors:
  127.0.0.1:5000:
    endpoint:
    - http://10.68.13.198:5000
  docker.io:
    endpoint:
    - https://docker.m.daocloud.io
    - https://registry.cn-hangzhou.aliyuncs.com
  gcr.io:
    endpoint:
    - https://gcr.m.daocloud.io
  ghcr.io:
    endpoint:
    - https://ghcr.m.daocloud.io
  quay.io:
    endpoint:
    - https://quay.m.daocloud.io
  registry.k8s.io:
    endpoint:
    - https://k8s.m.daocloud.io
REG
cat > /etc/systemd/system/k3s-agent.service.env <<ENV
K3S_TOKEN='$TOKEN'
K3S_URL='https://10.68.13.198:6443'
ENV
chmod 600 /etc/systemd/system/k3s-agent.service.env
cat > /etc/systemd/system/k3s-agent.service <<'UNIT'
[Unit]
Description=Lightweight Kubernetes
Documentation=https://k3s.io
Wants=network-online.target
After=network-online.target

[Install]
WantedBy=multi-user.target

[Service]
Type=notify
EnvironmentFile=-/etc/systemd/system/k3s-agent.service.env
KillMode=process
Delegate=yes
User=root
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
TimeoutStartSec=0
Restart=always
RestartSec=5s
ExecStartPre=-/sbin/modprobe br_netfilter
ExecStartPre=-/sbin/modprobe overlay
ExecStart=/usr/local/bin/k3s \\
    agent \\
	'--node-name' \\
	'__NODE_NAME__' \\
	'--node-ip' \\
	'__NEW_IP__' \\
	'--data-dir' \\
	'/Data/rancher' \\
	'--node-taint' \\
	'dedicated=new-node:NoSchedule' \\
	'--node-label' \\
	'role=new-node'
UNIT
sed -i "s/__NODE_NAME__/$NODE_NAME/; s/__NEW_IP__/$NEW_IP/" /etc/systemd/system/k3s-agent.service
systemctl daemon-reload
systemctl enable --now k3s-agent
sleep 5; systemctl is-active k3s-agent
EOF

echo "== [5/6] master 侧验证节点 Ready =="
sleep 15
SSH198 'echo "'"$P198"'" | sudo -S kubectl get node '"$NODE_NAME"' -o wide'

echo "== [6/6] 确认 taint 生效、无 pod 被调度 =="
SSH198 'echo "'"$P198"'" | sudo -S sh -c "kubectl get node '"$NODE_NAME"' -o jsonpath=\"{.spec.taints}\"; echo; kubectl get pods -A --field-selector spec.nodeName='"$NODE_NAME"' 2>/dev/null"'
echo "DONE: $NODE_NAME 已加入（taint dedicated=new-node:NoSchedule，放负载前先跑 egress preflight + canary）"
