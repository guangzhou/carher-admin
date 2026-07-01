#!/usr/bin/env bash
# build-dev.sh — 在 198 (AIYJY-litellm) 本机 docker build + push 本地 registry 127.0.0.1:5000
#
# 198 是独立 K3s 集群, 跟阿里云 ACR 无关。镜像走本机 registry, 不出网。
#
# 用法:
#   ./build-dev.sh                  # 自动 tag dev-$(git short)
#   ./build-dev.sh --tag custom-1   # 自定义 tag
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMG_NAME=chatgpt-pool-gateway
REGISTRY=127.0.0.1:5000   # 198 本机 registry

TAG="dev-$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d-%H%M%S)"
if [ "${1:-}" = "--tag" ]; then TAG="$2"; fi
FULL="$REGISTRY/$IMG_NAME:$TAG"

echo "[build-dev] target: $FULL  (198 local registry, 不走阿里云)"

# 上传源码到 198 tmp
TARBALL=/tmp/cpg-$(date +%s).tar.gz
tar --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
  -czf "$TARBALL" -C "$REPO_DIR/.." chatgpt-pool-gateway

REMOTE_DIR=/tmp/cpg-build-$$
echo "[build-dev] uploading $(du -sh $TARBALL | awk '{print $1}') to 198..."
jms ssh AIYJY-litellm "mkdir -p $REMOTE_DIR && cat > $REMOTE_DIR/src.tar.gz" < "$TARBALL"

jms ssh AIYJY-litellm "
  set -euo pipefail
  cd $REMOTE_DIR
  tar -xzf src.tar.gz
  cd chatgpt-pool-gateway
  echo '[build-dev] docker build (首次 ~3-5min)...'
  docker build --network=host -t $FULL -f Dockerfile .
  echo '[build-dev] push 127.0.0.1:5000 (insecure local)...'
  docker push $FULL
  cd /tmp && rm -rf $REMOTE_DIR
"
rm -f "$TARBALL"

echo
echo "[build-dev] ✅ $FULL pushed to 198 local registry"
echo "[build-dev] 更新 manifest: sed -i '' 's|cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/chatgpt-pool-gateway:dev-PLACEHOLDER|$FULL|' chatgpt-pool-gateway/manifests/gateway-dev.yaml"
