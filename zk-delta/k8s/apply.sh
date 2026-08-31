#!/usr/bin/env bash
# zk-delta/k8s/apply.sh —— 把 zk-delta 部署/更新到 198 集群
#
# 为什么是脚本而不是 kubectl apply -f 一把梭：
#   源码是用 ConfigMap 挂进容器的，改了 .js 就必须重建 ConfigMap，
#   而且要让 Deployment 知道源码变了（否则 pod 不会重启，你会对着旧代码调半天）。
#   这里用 pod template 上的 zk-delta/src-sha 注解来携带源码指纹，指纹变了才滚。
#
# 注意：这套流程只碰 zk-delta 自己的 Service/Deployment/ConfigMap。
#       litellm-proxy 的任何东西都不在这个脚本的射程内（仓库 manifest 长期陈旧，
#       对 litellm-proxy 用 apply 会同时回退 image 和内嵌 ConfigMap —— 那是另一条铁律）。
#
# 用法：
#   ./zk-delta/k8s/apply.sh              部署/更新
#   ./zk-delta/k8s/apply.sh --dry-run    只打印会做什么
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
NS=litellm-product
DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY=1

SSH_HOST="${ZKD_SSH_HOST:-cltx@10.68.13.198}"
SSH_PASS="${ZKD_SSH_PASS:-}"
if [[ -z "$SSH_PASS" ]]; then
  echo "需要 ZKD_SSH_PASS（198 的 ssh 口令）"; exit 2
fi

r198 () { sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 "$SSH_HOST" "$@"; }
K () { r198 "echo '$SSH_PASS' | sudo -S k3s kubectl -n $NS $*" 2>/dev/null; }

# ---- 源码指纹 ----
SRC_SHA="$(cat "$ROOT/common/framing.js" "$ROOT/server/server.js" | shasum -a 256 | cut -c1-16)"
echo "源码指纹 = $SRC_SHA"

if [[ -n "$DRY" ]]; then
  echo "[dry-run] 会做："
  echo "  1. 在 198 上重建 ConfigMap zk-delta-src（framing.js + server.js）"
  echo "  2. apply zk-delta.yaml，把 src-sha 注解换成 $SRC_SHA"
  echo "  3. rollout status deploy/zk-delta"
  echo "  4. 打 /healthz 自检"
  exit 0
fi

# ---- 1. 传源码 + 建 ConfigMap ----
# 走 base64 而不是直接 heredoc：源码里有引号、反斜杠、中文注释，heredoc 一路转义太脆。
# 语法在本地查（198 上没装 node），传过去之后比 sha256 —— 那才是这一跳真正要证的事：
# 落在 198 上的字节和本地一模一样。
echo "==> 本地语法检查"
node --check "$ROOT/common/framing.js" || { echo "framing.js 语法不过"; exit 1; }
node --check "$ROOT/server/server.js"  || { echo "server.js 语法不过"; exit 1; }
echo "  两个文件都过了 node --check"

echo "==> 传源码到 198"
B64_F="$(base64 < "$ROOT/common/framing.js" | tr -d '\n')"
B64_S="$(base64 < "$ROOT/server/server.js" | tr -d '\n')"
SHA_F="$(shasum -a 256 < "$ROOT/common/framing.js" | cut -d' ' -f1)"
SHA_S="$(shasum -a 256 < "$ROOT/server/server.js" | cut -d' ' -f1)"
r198 "mkdir -p /tmp/zkd-src && echo '$B64_F' | base64 -d > /tmp/zkd-src/framing.js && echo '$B64_S' | base64 -d > /tmp/zkd-src/server.js"
REMOTE_SHA="$(r198 "sha256sum /tmp/zkd-src/framing.js /tmp/zkd-src/server.js | cut -d' ' -f1 | tr '\n' ' '")"
if [[ "$REMOTE_SHA" != *"$SHA_F"* || "$REMOTE_SHA" != *"$SHA_S"* ]]; then
  echo "!! 传过去的字节和本地不一致，停"
  echo "   本地 framing=$SHA_F server=$SHA_S"
  echo "   198  $REMOTE_SHA"
  exit 1
fi
echo "  sha256 与本地一致"

echo "==> 重建 ConfigMap zk-delta-src"
K "create configmap zk-delta-src --from-file=framing.js=/tmp/zkd-src/framing.js --from-file=server.js=/tmp/zkd-src/server.js --dry-run=client -o yaml" > /tmp/zkd-cm.yaml
[[ -s /tmp/zkd-cm.yaml ]] || { echo "生成 ConfigMap yaml 失败"; exit 1; }
B64_CM="$(base64 < /tmp/zkd-cm.yaml | tr -d '\n')"
r198 "echo '$B64_CM' | base64 -d > /tmp/zkd-cm.yaml && echo '$SSH_PASS' | sudo -S k3s kubectl -n $NS apply -f /tmp/zkd-cm.yaml" 2>/dev/null

# ---- 2. apply Deployment/Service ----
echo "==> apply Deployment / Service"
sed "s#REPLACED_BY_APPLY_SH#$SRC_SHA#" "$HERE/zk-delta.yaml" > /tmp/zkd-deploy.yaml
B64_D="$(base64 < /tmp/zkd-deploy.yaml | tr -d '\n')"
r198 "echo '$B64_D' | base64 -d > /tmp/zkd-deploy.yaml && echo '$SSH_PASS' | sudo -S k3s kubectl -n $NS apply -f /tmp/zkd-deploy.yaml" 2>/dev/null

# ---- 3. 等就绪 ----
echo "==> 等 rollout"
K "rollout status deploy/zk-delta --timeout=120s"

# ---- 4. 自检 ----
echo "==> 自检"
K "get pod -l app=zk-delta -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount"
echo -n "  经 nginx 打 /zkd/healthz: "
curl -fsS -m 10 https://cc.auto-link.com.cn/zkd/healthz || echo "打不通（检查 nginx 里的 location ^~ /zkd/）"
echo
echo "完成。指纹 $SRC_SHA"
