#!/usr/bin/env bash
# Build a pinned, hardened muse2api image on 188 and roll it out on 198 k3s (ns muse2api, node 242).
#
# Steps:
#   1. 188: download upstream at MUSE2API_SHA, apply harden.py, docker build
#   2. stream the image 188 -> 198, push to the 198 registry 127.0.0.1:5000
#   3. 198: create Secret muse2api-env (MUSE2API_KEY) if missing, kubectl apply k8s.yaml
#
# 198 is the only place it runs. 188 is a build box only; nothing is left running there.
# Rollback: set image back to the previous tag
#   sudo kubectl -n muse2api set image deploy/muse2api muse2api=127.0.0.1:5000/muse2api:<old-tag>
# Account cookies live on PVC muse2api-data; deleting the namespace deletes them.
#
# The pod needs coredns-custom zones metaaivm.server + muse.server (forward to 8.8.8.8/1.1.1.1);
# without them cluster DNS returns poisoned IPs for hatch.metaaivm.com and chat fails with
# "云端 VM 连接超时 (Still sending)". Added 2026-10-13; backup in 198:~/backups/coredns-custom-*.
set -euo pipefail

BUILD="${MUSE2API_BUILD_HOST:-cltx@10.68.13.188}"
K3S="${MUSE2API_K3S_HOST:-cltx@10.68.13.198}"
# Default is the tag the cluster runs: upstream v1.6.4 + harden h10
# (h10 = h9 + harden_av #11「视频不再全挤在一个号上」；h9 = h8 + 听写走共用隧道；
#  h8 = h7 + #7f「app.py 自己那两处续签也走该号的腿」；h7 = harden.py #1~#8 和
#  harden_av.py #9~#10；h6 = harden.py only, no audio/video).
# 🔴 这个默认值必须跟着线上走：h7 的镜像还在 registry 里，下面那段「已有 tag 就跳过构建」
# 会直接 set image 到 h7 ⇒ 默认值落后一级就是静默把线上回滚掉（2026-10-30 复核发现）。
SHA="${MUSE2API_SHA:-5b8bba9561fdbe6402fbc08012d144699760afd0}"   # upstream v1.6.4
REV="${MUSE2API_HARDEN_REV:-h10}"
# MUSE2API_BUILD_ONLY=1 stops after the registry push: no Secret, no kubectl apply.
BUILD_ONLY="${MUSE2API_BUILD_ONLY:-0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="${SHA:0:12}-$REV"
IMG="127.0.0.1:5000/muse2api:$TAG"

if ssh "$K3S" "curl -fs http://127.0.0.1:5000/v2/muse2api/tags/list | grep -q '\"$TAG\"'"; then
  echo "image $TAG already in 198 registry, skip build"
else
  # harden.py reads proxy_bridge.py from its own directory (harden patch #7).
  # harden_av.py runs second, on the same checkout: patches #9 (audio/video in) and
  # #10 (OpenAI-shaped /v1/videos). It is a separate file so the whole media feature can
  # be dropped without touching #1~#8; both must succeed or the build fails.
  scp -q "$HERE/harden.py" "$BUILD:/tmp/muse2api-harden.py"
  scp -q "$HERE/harden_av.py" "$BUILD:/tmp/muse2api-harden-av.py"
  scp -q "$HERE/proxy_bridge.py" "$BUILD:/tmp/proxy_bridge.py"
  ssh "$BUILD" SHA="$SHA" TAG="$TAG" 'bash -s' << 'REMOTE'
set -euo pipefail
work="$(mktemp -d)"
trap 'rm -rf "$work" /tmp/muse2api-harden.py /tmp/muse2api-harden-av.py /tmp/proxy_bridge.py' EXIT
curl -fsSL --max-time 120 "https://codeload.github.com/czg86389-hub/muse2api/tar.gz/$SHA" | tar -xz -C "$work"
src="$(echo "$work"/muse2api-*)"
python3 /tmp/muse2api-harden.py "$src"
python3 /tmp/muse2api-harden-av.py "$src"
python3 -m py_compile "$src/app.py" "$src/engine.py"   # a patched file that won't import = CrashLoop
docker build -q -t "muse2api:$TAG" "$src" >/dev/null
REMOTE
  ssh "$BUILD" "docker save muse2api:$TAG && docker rmi muse2api:$TAG >/dev/null" \
    | ssh "$K3S" "cat > /tmp/muse2api.tar \
        && sudo docker load -i /tmp/muse2api.tar >/dev/null \
        && sudo docker tag muse2api:$TAG $IMG \
        && sudo docker push $IMG >/dev/null \
        && sudo docker rmi muse2api:$TAG $IMG >/dev/null; rm -f /tmp/muse2api.tar"
  echo "pushed $IMG"
fi

if [ "$BUILD_ONLY" = "1" ]; then
  echo "MUSE2API_BUILD_ONLY=1: image is in the registry, nothing applied."
  echo "roll out later with: sudo kubectl -n muse2api set image deploy/muse2api muse2api=$IMG"
  exit 0
fi

sed "s#image: 127.0.0.1:5000/muse2api:.*#image: $IMG#" "$HERE/k8s.yaml" | ssh "$K3S" 'cat > /tmp/muse2api-k8s.yaml'
ssh "$K3S" 'bash -s' << 'REMOTE'
set -euo pipefail
sudo kubectl get ns muse2api >/dev/null 2>&1 || sudo kubectl create ns muse2api
if ! sudo kubectl -n muse2api get secret muse2api-env >/dev/null 2>&1; then
  sudo kubectl -n muse2api create secret generic muse2api-env \
    --from-literal=MUSE2API_KEY="m2a_$(python3 -c 'import secrets;print(secrets.token_hex(24))')" >/dev/null
fi
sudo kubectl apply -f /tmp/muse2api-k8s.yaml
rm -f /tmp/muse2api-k8s.yaml
sudo kubectl -n muse2api rollout status deploy/muse2api --timeout=300s
REMOTE
