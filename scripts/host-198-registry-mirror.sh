#!/bin/bash
# 把公网多架构镜像搬进 198 本地 registry(127.0.0.1:5000),只要 linux/amd64。
#
#   scripts/host-198-registry-mirror.sh <src-ref> <dst-ref>
#   scripts/host-198-registry-mirror.sh ghcr.nju.edu.cn/open-webui/open-webui:v0.12.3 127.0.0.1:5000/open-webui:v0.12.3
#
# 为什么不 docker pull/tag/push(10-24 open-webui、sub2api 各踩一次):
#   198 docker 29 开了 containerd snapshotter,拉多架构镜像只落 amd64 层,tag 出来仍是整个 index ⇒
#   push 报 `does not provide any platform`;加 --platform 报 `does not provide the specified platform`;
#   按 amd64 digest 拉也一样(报 `content digest ... not found`)。
#   k3s 自带的 ctr 能 `push --platform` 只推 amd64 单 manifest。
# 最后 docker pull 回 dst,方便接着 `docker run` 看镜像内容;打印 dst 的 digest。
# 退出码: 0=推好且 registry 能读到 manifest;2=用法;3=失败
set -uo pipefail
[ $# -eq 2 ] || { sed -n '2,13p' "$0"; exit 2; }
SRC=$1 DST=$2
H198=${H198:-cltx@10.68.13.198}

ssh -n -o BatchMode=yes -o ConnectTimeout=30 -o ClearAllForwardings=yes "$H198" "
set -e
C='sudo -n k3s ctr -n k8s.io'
\$C images pull --platform linux/amd64 '$SRC' >/dev/null
\$C images tag --force '$SRC' '$DST' >/dev/null
\$C images push --plain-http --platform linux/amd64 '$DST' >/dev/null
sudo -n docker pull -q '$DST' >/dev/null
R='$DST'; R=\${R#*/}; REPO=\${R%:*}; TAG=\${R##*:}
curl -sfI http://127.0.0.1:5000/v2/\$REPO/manifests/\$TAG \
  -H 'Accept: application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
  | tr -d '\r' | grep -i '^docker-content-digest'
" || { echo "FAIL 镜像搬运 $SRC → $DST" >&2; exit 3; }
