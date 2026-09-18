#!/usr/bin/env bash
# 9router 镜像投递 —— 现役唯一可用路径:188 构建 → Mac 中转 → 198 本地 registry → set image。
#
# ⛔ 别再走 sideload.sh 里那条特权 nodedbg-225 + kubectl cp + `k3s ctr images import`:
#    225 没有 SSH,sideload 的 step5 是坏的。见 memory
#    feedback_9router_image_ships_via_198_local_registry。
# ⛔ 也别试这两条,都实测失败:
#    - 188 直推 198:5000  → `http: server gave HTTP response to HTTPS client`
#    - 188 ↔ 198 直接 scp → 不通(必须以 Mac 为中转,两段都走 jms scp)
#
# ⚠️⚠️ 生产硬中断:deploy/9router 是 **replicas=1 + strategy: Recreate**。
#    换镜像 = 旧 Pod 先死再起新的,期间反代整条腿不可用(几十秒)。**动之前先跟用户说**。
#    `imagePullPolicy: IfNotPresent` 必须保持 —— 本地 registry 拉取依赖它。
#
# ⚠️ 磁盘:188 根分区常年 ~93%。每个 tar ≈ 713M。写到 /Data,跑完就删,别攒。
#
# 用法:
#   TAG=fc-20260918a MARKER='some_new_unique_literal' ./ship-via-198-registry.sh          # 只投递不切
#   TAG=fc-20260918a MARKER='...' APPLY=1 ./ship-via-198-registry.sh                      # 投递并 set image
#
# MARKER = 本次改动里一个**全新的唯一字符串字面量**。open-sse 是 webpack 构建期打包的,
# 改源码不重建 = 运行时零效果。MARKER 存在于编译产物 = 唯一可信的"真的编译进去了"门禁。
# 最好再配一个**差分门禁**:旧镜像里该字面量计数=0,新镜像=1;外加一个不存在的
# 假字面量做负对照,确认 grep 本身不是恒真。

set -eu

TAG="${TAG:?need TAG, e.g. fc-20260918a}"
MARKER="${MARKER:?need MARKER: a NEW unique string literal from this change}"
NEGCTL="${NEGCTL:-__bogus_marker_that_must_not_exist__}"
SRC="${SRC:-/Data/9router-build/src}"
BUILD_HOST="${BUILD_HOST:-10.68.13.188}"
BASTION_IP="${BASTION_IP:-10.68.13.198}"
REG="${REG:-127.0.0.1:5000}"
NS="${NS:-litellm-product}"
BUNDLE="${BUNDLE:-/app/.next/server/chunks/318.js}"
APPLY="${APPLY:-0}"
JMS="${JMS:-$(cd "$(dirname "$0")/../.." && pwd)/scripts/jms}"
TAR="/Data/tmp_9router-${TAG}.tar"
LOCAL_TAR="/tmp/tmp_9router-${TAG}.tar"

# jms 是 PTY,带登录噪声,禁在命令行上叠引号 ⇒ 一律写脚本、scp、再 bash。
jms_run() {  # jms_run <host> <script-text>
  local host="$1"; shift
  local f; f="$(mktemp /tmp/tmp_ship_XXXXXX.sh)"
  printf '%s\n' "$*" > "$f"
  jms_retry scp "$f" "${host}:${f}" >/dev/null
  jms_retry ssh "$host" "bash ${f}; rc=\$?; rm -f ${f}; exit \$rc"
  local rc=$?; rm -f "$f"; return $rc
}
# jms 偶发 `Permission denied (password,publickey)`,基本是假红 ⇒ 3 次重试。
jms_retry() {
  local i=1 out rc
  while [ "$i" -le 3 ]; do
    out="$("$JMS" "$@" 2>&1)"; rc=$?
    if ! echo "$out" | grep -q 'Permission denied (password,publickey)'; then
      printf '%s\n' "$out"; return $rc
    fi
    i=$((i+1))
  done
  printf '%s\n' "$out"; return 1
}
# PTY 噪声会污染 sha 比较 ⇒ 只取第一个 64 位 hex。
hex64() { grep -Eo '[0-9a-f]{64}' | head -1; }

echo "== step1/6: 188 build =="
jms_run "$BUILD_HOST" "set -e
cd '$SRC'
docker build -t 9router:${TAG} . >/tmp/tmp_build_${TAG}.log 2>&1 || { tail -40 /tmp/tmp_build_${TAG}.log; exit 1; }
echo BUILT=9router:${TAG}
df -h / | tail -1 | sed 's/^/DISK_188=/'"

echo "== step2/6: MARKER 硬门禁(在 188 的镜像里验,别等上线才发现没编译进去) =="
# 门禁必须带 sentinel 前缀:jms 登录噪声会把"非空输出"填满,拿"非空"当门禁 ⇒ 永绿。
GATE="$(jms_run "$BUILD_HOST" "set -e
H=\$(docker run --rm --entrypoint sh 9router:${TAG} -c \"grep -c '${MARKER}' ${BUNDLE} 2>/dev/null || echo 0\")
NC=\$(docker run --rm --entrypoint sh 9router:${TAG} -c \"grep -c '${NEGCTL}' ${BUNDLE} 2>/dev/null || echo 0\")
echo BUNDLEHIT:\$H
echo NEGCTL:\$NC")"
echo "$GATE" | grep -E 'BUNDLEHIT:|NEGCTL:' || true
H="$(echo "$GATE" | sed -n 's/.*BUNDLEHIT:\([0-9]*\).*/\1/p' | head -1)"
NC="$(echo "$GATE" | sed -n 's/.*NEGCTL:\([0-9]*\).*/\1/p' | head -1)"
[ "${H:-0}" -ge 1 ] || { echo "GATE=RED MARKER 没编译进 bundle,投递中止"; exit 1; }
[ "${NC:-1}" -eq 0 ] || { echo "GATE=RED 负对照命中了 ⇒ 这个 grep 恒真,门禁无效"; exit 1; }
echo "GATE=GREEN (hit=$H negctl=$NC)"

echo "== step3/6: 188 save + sha =="
SHA188="$(jms_run "$BUILD_HOST" "set -e
docker save 9router:${TAG} -o ${TAR}
sha256sum ${TAR}" | hex64)"
echo "SHA188=$SHA188"

echo "== step4/6: 188 → Mac → 198 (两段 jms scp;直连不通) =="
jms_retry scp "${BUILD_HOST}:${TAR}" "$LOCAL_TAR" >/dev/null
SHAMAC="$(shasum -a 256 "$LOCAL_TAR" | hex64)"
echo "SHAMAC=$SHAMAC"
[ "$SHA188" = "$SHAMAC" ] || { echo "SHA_MISMATCH 188 vs Mac"; exit 1; }
jms_retry scp "$LOCAL_TAR" "${BASTION_IP}:${TAR}" >/dev/null

echo "== step5/6: 198 load → tag → push 本地 registry =="
jms_run "$BASTION_IP" "set -e
sha256sum ${TAR} | sed 's/^/SHA198=/'
docker load -i ${TAR}
docker tag 9router:${TAG} ${REG}/9router:${TAG}
docker push ${REG}/9router:${TAG} >/tmp/tmp_push_${TAG}.log 2>&1 || { tail -20 /tmp/tmp_push_${TAG}.log; exit 1; }
echo PUSHED=${REG}/9router:${TAG}
curl -s http://${REG}/v2/9router/tags/list | sed 's/^/TAGS=/'
rm -f ${TAR}; echo CLEANED_198_TAR=yes"
rm -f "$LOCAL_TAR"
jms_run "$BUILD_HOST" "rm -f ${TAR}; echo CLEANED_188_TAR=yes; df -h / | tail -1 | sed 's/^/DISK_188=/'"

if [ "$APPLY" != "1" ]; then
  echo
  echo "== step6/6: SKIPPED (APPLY!=1) =="
  echo "镜像已在 ${REG}/9router:${TAG}。切流是硬中断,确认后再跑:"
  echo "  kubectl -n ${NS} set image deploy/9router 9router=${REG}/9router:${TAG}"
  echo "  kubectl -n ${NS} rollout status deploy/9router --timeout=180s"
  echo "回滚:把 TAG 换成上一个可用 tag,同样一条 set image。"
  exit 0
fi

echo "== step6/6: set image(⚠️ Recreate+1副本,这里开始硬中断) =="
jms_run "$BASTION_IP" "set -e
PREV=\$(kubectl -n ${NS} get deploy/9router -o jsonpath='{.spec.template.spec.containers[0].image}')
echo ROLLBACK_TO=\$PREV
kubectl -n ${NS} set image deploy/9router 9router=${REG}/9router:${TAG}
kubectl -n ${NS} rollout status deploy/9router --timeout=180s
POD=\$(kubectl -n ${NS} get pod -l app=9router -o jsonpath='{.items[0].metadata.name}')
echo LIVE_POD=\$POD
kubectl -n ${NS} exec \$POD -- sh -c \"grep -c '${MARKER}' ${BUNDLE}\" | sed 's/^/LIVE_MARKER=/'"
echo
echo "DONE. 现在按顺序验:"
echo "  1) trace-9router.sh          POST==DONE / restarts=0 / sessions_history=0"
echo "  2) probe-fold.sh             f7/f8 折叠三条腿"
echo "  3) acceptance-feishu-doc.sh  真实工具链 + verify-doc-by-control.sh 对照读回"
