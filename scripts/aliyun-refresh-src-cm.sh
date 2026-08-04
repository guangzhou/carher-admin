#!/usr/bin/env bash
# aliyun-refresh-src-cm.sh — 用**仓库 HEAD 版**的 oauth.py / toggle.py 重建阿里云上的
# onboard 源码 ConfigMap,并把 SRC_CM 名字打印出来给 aliyun-eip-onboard-via-jms.sh 用。
#
# 为什么需要它:via-jms 路径默认复用集群里现存的 `cgpt-onboard-src-131`(2026-07-26 那批
# 留下的快照)。**改了仓库里的 toggle.py/oauth.py 不会自动生效** —— Job 跑的是 CM 里的
# 旧副本。不重建 CM 就以为修好了,是这条链路最容易骗自己的地方。
# 本地 kubectl 隧道死时(见 memory feedback_jms_relay_dead_use_tty_kubeconfig_on_226),
# --from-file 用不了,所以走 push226.sh(sha256 断言)把文件推到节点再在节点上建 CM。
#
# 用法: bash scripts/aliyun-refresh-src-cm.sh [TAG]
#   默认**只推改动过的 toggle.py**,oauth.py 从 BASE_CM(默认 cgpt-onboard-src-131)在节点上
#   原地取 —— 142KB 的 oauth.py 压完仍有 52KB/18 块,链路塌陷时要一小时;而它这次没改。
#   要连 oauth.py 一起重推:PUSH_OAUTH=1 bash scripts/aliyun-refresh-src-cm.sh
#   产出 CM 名:cgpt-onboard-src-<TAG>
#   之后: SRC_CM=cgpt-onboard-src-<TAG> bash scripts/aliyun-eip-onboard-via-jms.sh toggle <N> <csv>
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ASSET="${KVIA_ASSET:-k8s-work-226}"
NS=carher
BASE_CM="${BASE_CM:-cgpt-onboard-src-131}"
PUSH_OAUTH="${PUSH_OAUTH:-0}"
OAUTH="$REPO/scripts/chatgpt-onboard/chatgpt-litellm-oauth.py"
TOGGLE="$REPO/scripts/chatgpt-onboard/chatgpt-enable-codex-toggle.py"
for f in "$OAUTH" "$TOGGLE"; do [ -s "$f" ] || { echo "FATAL: 缺 $f"; exit 1; }; done

TSHA=$(shasum -a 256 "$TOGGLE" | cut -c1-8)
OSHA=$(shasum -a 256 "$OAUTH" | cut -c1-8)
# CM 名必须诚实反映里面到底是什么:复用 oauth 时名字只能承诺 toggle 的版本。
if [ "$PUSH_OAUTH" = "1" ]; then
  TAG="${1:-$(cat "$OAUTH" "$TOGGLE" | shasum -a 256 | cut -c1-8)}"
else
  TAG="${1:-${TSHA}tgl}"
fi
CM="cgpt-onboard-src-$TAG"
echo "[src-cm] $CM  (toggle sha=$TSHA, oauth sha=$OSHA, PUSH_OAUTH=$PUSH_OAUTH base=$BASE_CM)"

bash "$REPO/scripts/push226.sh" "$TOGGLE" /tmp/src-toggle-$TAG.py  || exit 1
if [ "$PUSH_OAUTH" = "1" ]; then
  bash "$REPO/scripts/push226.sh" "$OAUTH" /tmp/src-oauth-$TAG.py  || exit 1
  OAUTH_SRC="/tmp/src-oauth-$TAG.py"
else
  # 从既有 CM 里原地取 oauth.py(不过链路)。⚠ 这意味着 oauth.py 用的是 BASE_CM 的版本,
  # 改过 oauth.py 就必须 PUSH_OAUTH=1。字节数不等时下面会显式告警(2026-08-04 实测:
  # BASE_CM 的 141937B ≠ 仓库工作区 142276B —— 工作区有没部署的改动)。
  echo "  [reuse] oauth.py ← $BASE_CM(节点本地取, 不过链路)"
  OLEN=$("$REPO/scripts/jms" ssh --tty --timeout 180 "$ASSET" \
    "kubectl -n $NS get cm $BASE_CM -o jsonpath='{.data.oauth\.py}' > /tmp/src-oauth-$TAG.py; echo OLEN=\$(wc -c < /tmp/src-oauth-$TAG.py)" 2>&1 \
    | grep -oE 'OLEN=[0-9]+' | tail -1 | cut -d= -f2)
  LLEN=$(wc -c < "$OAUTH" | tr -d ' ')
  echo "    oauth.py: BASE_CM=${OLEN:-?}B  仓库=${LLEN}B"
  [ "${OLEN:-0}" = "$LLEN" ] || echo "    ⚠️ 两者不一致 —— 本次改动若涉及 oauth.py, 必须改用 PUSH_OAUTH=1 重跑"
  OAUTH_SRC="/tmp/src-oauth-$TAG.py"
fi

# 建 CM 后**回查 key 数与 sha**,不信任 apply 的输出(PTY 通道会静默吞成败行)
OUT=$("$REPO/scripts/jms" ssh --tty --timeout 300 "$ASSET" "
kubectl -n $NS create cm $CM \
  --from-file=oauth.py=$OAUTH_SRC \
  --from-file=toggle.py=/tmp/src-toggle-$TAG.py \
  --dry-run=client -o yaml | kubectl -n $NS apply -f - >/dev/null 2>&1
echo KEYS=\$(kubectl -n $NS get cm $CM -o go-template='{{len .data}}' 2>/dev/null)
kubectl -n $NS get cm $CM -o jsonpath='{.data.toggle\.py}' 2>/dev/null | sha256sum | cut -c1-16 | sed 's/^/CMTOGGLESHA=/'
" 2>&1)
KEYS=$(echo "$OUT" | grep -oE 'KEYS=[0-9]+' | tail -1 | cut -d= -f2)
CMSHA=$(echo "$OUT" | grep -oE 'CMTOGGLESHA=[0-9a-f]+' | tail -1 | cut -d= -f2)
WANT=$(shasum -a 256 "$TOGGLE" | cut -c1-16)
echo "  keys=$KEYS toggle_sha_in_cm=$CMSHA want=$WANT"
[ "$KEYS" = "2" ] && [ "$CMSHA" = "$WANT" ] || { echo "❌ $CM 校验失败 — 别拿它去跑 Job"; exit 1; }
echo "✓ $CM 就绪。用法: SRC_CM=$CM bash scripts/aliyun-eip-onboard-via-jms.sh toggle <N> <csv>"
