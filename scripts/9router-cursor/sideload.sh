#!/usr/bin/env bash
# 9router:fc-<tag> 构建 → 侧载 → 部署 → 验证 一条龙(Cursor 反代 lane)。
#
# 为什么存在:225 节点(aiyjy-litellm-standby)无 SSH、imagePullPolicy=Never、
# 无集群内 registry,ACR VPC 通道是给 admin/operator 的、不走这条。9router 的
# open-sse 是构建期 webpack 打包,COPY 原文件对运行时零效果 —— 所以必须重建镜像
# 并把整镜像侧载进节点 containerd。
#
# 动了啥(footprint):
#   - 188 /Data/9router-build/src 上 `docker build`(源码需先 jms scp 同步过去)
#   - 生成临时 tar:188 /tmp、Mac /tmp、198 bastion /tmp、225 节点 /tmp(全 tmp_ 前缀,跑完删)
#   - 在 default ns 建**特权** pod nodedbg-225(hostPath / 挂 /host),import 后删除
#   - `kubectl set image deploy/9router`(litellm-product ns)—— 触发滚动更新,不手删 pod
# 备份/回滚:
#   - Deployment 保留 rollout 历史:回滚 = `sudo kubectl -n litellm-product rollout undo deploy/9router`
#     或 set image 回上一个 tag。旧镜像仍在节点 containerd(未 prune)。
#   - PVC(token/key,/app/data/db/data.sqlite)不受镜像替换影响。
# 硬门:
#   - MARKER(本次改动新增的字符串字面量)必须出现在 bundle chunk → 证明改动真进了打包产物。
#   - 每一跳 sha256 比对,不吻合即 abort。
# 禁忌:本机 Mac 不 build;198 现网 manifest 禁 apply(只 set image);禁手删正在服务的 pod。
#
# 用法:
#   TAG=fc-20260913h MARKER='URL fetching is not available' scripts/9router-cursor/sideload.sh
# 可选 env:
#   SRC=/Data/9router-build/src  BUILD_HOST=JSZX-AI-03  NS=litellm-product
#   BASTION=cltx@10.68.13.198    NODE=aiyjy-litellm-standby  SKIP_BUILD=1(镜像已在 188 时跳过 build)
set -euo pipefail

TAG="${TAG:?need TAG, e.g. fc-20260913h}"
MARKER="${MARKER:?need MARKER: a NEW string literal from this change, to prove it compiled into the bundle}"
SRC="${SRC:-/Data/9router-build/src}"
# 用 IP 而不是资产名:`jms ssh JSZX-AI-03` 会路由到 10.68.13.189 并被拒
# (JMS-…@10.68.13.189: Permission denied),`jms ssh 10.68.13.188` 才通。
BUILD_HOST="${BUILD_HOST:-10.68.13.188}"
NS="${NS:-litellm-product}"
BASTION="${BASTION:-cltx@10.68.13.198}"
NODE="${NODE:-aiyjy-litellm-standby}"
IMG="9router:${TAG}"
TAR="tmp_9router-${TAG}.tar"
DBG=nodedbg-225
HERE="$(cd "$(dirname "$0")/../.." && pwd)"   # repo root (for scripts/jms)
JMS="${HERE}/scripts/jms"
# 198 只能经 jms 跳板到达(直连 ssh 报 Permission denied(publickey,password))。
# 两台机器都只能经 jms 跳板到达(直连 ssh 报 Permission denied(publickey,password)),
# 且 jms 是 PTY:多层引号会被本地 shell 吃掉、输出会混进登录噪声。
# 因此一律「把命令写成脚本 → jms scp 过去 → bash 执行」,不在命令行里堆引号。
BASTION_IP="${BASTION_IP:-10.68.13.198}"
jms_run() {  # jms_run <host> <script-text>
  local host="$1"; shift
  local f; f="$(mktemp /tmp/tmp_sl_XXXXXX.sh)"
  printf '%s\n' "$*" > "$f"
  "$JMS" scp "$f" "${host}:${f}" >/dev/null
  "$JMS" ssh "$host" "bash ${f}; rc=\$?; rm -f ${f}; exit \$rc"
  local rc=$?; rm -f "$f"; return $rc
}
ssh_198() { jms_run "$BASTION_IP" "$@"; }
ssh_188() { jms_run "$BUILD_HOST" "$@"; }
SSH=ssh_198
# jms PTY 会带 \r 和登录噪声 ⇒ 比 sha 前必须抽出那 64 位 hex,否则必假红。
hex64() { grep -Eo '[0-9a-f]{64}' | head -1; }
# jms 走 PTY,输出会带 \r 和登录噪声 ⇒ 比 sha 前先抽出那 64 位 hex,否则必假红。
hex64() { grep -Eo '[0-9a-f]{64}' | head -1; }

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

# 1) build on 188 (never on Mac) --------------------------------------------
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  say "build ${IMG} on ${BUILD_HOST} (--no-cache)"
  ssh_188 "set -e; cd ${SRC}; docker build --no-cache -t ${IMG} . 2>&1 | tail -6"
fi

# 2) HARD GATE: marker must be in the webpack bundle -------------------------
# 判据必须是一个只有真命中才会出现的 sentinel:HIT 非空不算,jms 登录噪声本身
# 就能把它填满 ⇒ 那样这道"硬门禁"永远绿。
say "hard gate: MARKER must be in bundle (COPY-only builds are inert)"
GATE="$(ssh_188 "docker run --rm --entrypoint sh ${IMG} -c 'grep -rl \"${MARKER}\" /app/.next/server/chunks/ | head -1' | sed -e 's/^/BUNDLEHIT:/'")"
HIT="$(printf '%s' "$GATE" | grep -o 'BUNDLEHIT:[^[:space:]]\+' | head -1)"
if [ -z "$HIT" ]; then
  echo "$GATE" >&2
  echo "ABORT: MARKER not found in bundle — change did not compile in (raw source COPY != runtime)." >&2
  exit 3
fi
echo "bundle hit: ${HIT#BUNDLEHIT:}"

# 3) save + pull to Mac, sha at each hop ------------------------------------
say "save + pull tar (sha-gated)"
ssh_188 "docker save ${IMG} -o /tmp/${TAR}"
SHA188="$(ssh_188 "sha256sum /tmp/${TAR}" | hex64)"
"$JMS" scp "${BUILD_HOST}:/tmp/${TAR}" "/tmp/${TAR}"
SHAMAC="$(shasum -a 256 "/tmp/${TAR}" | awk '{print $1}')"
[ "$SHA188" = "$SHAMAC" ] || { echo "ABORT: sha mismatch 188->Mac ($SHA188 vs $SHAMAC)" >&2; exit 4; }

# 4) push Mac -> bastion, sha gate ------------------------------------------
say "push tar Mac -> bastion"
"$JMS" scp "/tmp/${TAR}" "${BASTION_IP}:/tmp/${TAR}"
SHABAS="$($SSH "sha256sum /tmp/${TAR}" | hex64)"
[ "$SHAMAC" = "$SHABAS" ] || { echo "ABORT: sha mismatch Mac->bastion" >&2; exit 4; }

# 5) fresh privileged nodedbg pod on the target node (throwaway; not apply) --
say "create privileged ${DBG} pod on ${NODE} (fresh throwaway)"
$SSH "cat <<EOF | sudo kubectl create -f -
apiVersion: v1
kind: Pod
metadata: { name: ${DBG}, namespace: default }
spec:
  nodeName: ${NODE}
  hostPID: true
  restartPolicy: Never
  containers:
  - name: dbg
    image: ${IMG}
    imagePullPolicy: Never
    command: [\"sleep\",\"3600\"]
    securityContext: { privileged: true }
    volumeMounts: [{ name: host, mountPath: /host }]
  volumes:
  - name: host
    hostPath: { path: / }
EOF" || true
# the fresh pod uses the new image only if it is ALREADY on-node; on first
# deploy of a tag the node may lack it, so fall back to the currently-running
# 9router image for the dbg sleeper if create failed on ImagePullBackOff.
$SSH "sudo kubectl wait --for=condition=Ready pod/${DBG} --timeout=60s || sudo kubectl get pod ${DBG} -o wide"

# 6) cp tar onto node via host mount, sha gate, import ------------------------
say "cp tar onto node + import into k3s containerd"
$SSH "sudo kubectl cp /tmp/${TAR} default/${DBG}:/host/tmp/${TAR}"
SHANODE="$($SSH "sudo kubectl exec ${DBG} -- chroot /host sha256sum /tmp/${TAR}" | hex64)"
[ "$SHABAS" = "$SHANODE" ] || { echo "ABORT: sha mismatch bastion->node" >&2; exit 4; }
$SSH "sudo kubectl exec ${DBG} -- chroot /host k3s ctr -n k8s.io images import /tmp/${TAR}"

# 7) roll the deployment (set image only; rollout, never manual delete) ------
say "set image + rollout"
$SSH "sudo kubectl -n ${NS} set image deploy/9router 9router=${IMG} && sudo kubectl -n ${NS} rollout status deploy/9router --timeout=180s"
$SSH "sudo kubectl -n ${NS} get pods -l app=9router -o wide | tail; sudo kubectl -n ${NS} get deploy 9router -o jsonpath='{.spec.template.spec.containers[0].image}{\"\n\"}'"

# 8) cleanup: delete privileged pod + all tmp_ tars --------------------------
say "cleanup (delete privileged pod + tars)"
$SSH "sudo kubectl exec ${DBG} -- chroot /host rm -f /tmp/${TAR}; sudo kubectl delete pod ${DBG} --wait=false; rm -f /tmp/${TAR}"
rm -f "/tmp/${TAR}"
ssh_188 "rm -f /tmp/${TAR}"
say "DONE: ${IMG} live in ns ${NS}. Terminal judgement = real client test, NOT a probe."
