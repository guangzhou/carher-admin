#!/usr/bin/env bash
# aliyun-eip-node-reclaim.sh — 给 EIP 节点腾出 onboard Job 起得来的磁盘余量
#
# 为什么需要: 两个 CF-clean EIP 节点(.86 / .122)的 nodefs 常年**贴着 kubelet 驱逐阈值
# (31.6G available)**上下飘。onboard/renew Job 一拉 1.4G 镜像就把节点推过线, 结果是
#   · 准入拒绝 —— pod phase=Failed/reason=Evicted, conditions 和 containerStatuses **双双为空**
#   · 或运行时驱逐 —— 容器起过, exit 137, `kubectl logs` 取不回来
# 两种都长得像"号有问题", 其实是节点问题。判据见 aliyun-eip-onboard.sh 顶部注释。
#
# ⚠️ 节点上**没有多少可回收的东西**, 别指望这脚本变出几十 G:
#   · /var/lib/containerd 74G —— 79 个 pod 在用, `crictl rmi --prune` 实测 **0 字节**
#   · /var/lib/kubelet/pods/* 每个约 1.7G × 79 —— 是 carher 实例的合法数据, 不许动
#     (⚠ 量它必须 `du -x`: 不加 -x 会走进 NAS 挂载, 在一块 211G 的盘上报出 275G)
#   真正能拿回来的只有两样, 加起来 3~5G, 但够跑一轮:
#     1) 退出态容器 —— 每个 Job 跑完都留一份, 攥着自己的可写层不放
#     2) journald 归档日志 —— 实测一次 vacuum 放出 3.5G
#
# ⚠️ 只对 **.122** 有效: jms 资产 `dify` 就是它(EIP 47.84.85.100), 有 root shell。
#    .86 没有 ssh 资产, 只能靠 kubectl 观测, 回收不了 —— 所以 .122 是首选落脚点。
#
# 用法:
#   ./scripts/aliyun-eip-node-reclaim.sh            # 看余量, 不够就回收
#   RECLAIM_FORCE=1 ./scripts/aliyun-eip-node-reclaim.sh   # 无条件回收
set -uo pipefail

NODE_122=ap-southeast-1.172.16.16.122
JMS_ASSET="${RECLAIM_ASSET:-dify}"
THRESH=31646917660          # kubelet nodefs 硬驱逐阈值(实测取自驱逐事件原文)
WANT_GB="${RECLAIM_WANT_GB:-4}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

headroom_gb(){
  kubectl get --raw "/api/v1/nodes/$1/proxy/stats/summary" 2>/dev/null | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except Exception: print('-1'); raise SystemExit
print('%.2f' % ((d['node']['fs']['availableBytes'] - $THRESH) / 1e9))"
}

H=$(headroom_gb "$NODE_122")
echo "[reclaim] .122 余量=${H}G (目标 ≥${WANT_GB}G)"
if [ "${RECLAIM_FORCE:-0}" != "1" ] && python3 -c "import sys; sys.exit(0 if float('$H')>=float('$WANT_GB') else 1)"; then
  echo "[reclaim] 余量够, 不动手。"
  exit 0
fi

# ── 1) 本命名空间自己的终态 Job pod(它们的可写层要删掉 pod 才还) ──────────────
VICTIMS=$(kubectl -n carher get pods --no-headers \
  -o custom-columns='N:.metadata.name,P:.status.phase' 2>/dev/null \
  | awk '$1 ~ /^cgpt-(onboard|billing)-/ && $2 != "Running" && $2 != "Pending" {print $1}')
if [ -n "$VICTIMS" ]; then
  echo "[reclaim] 删终态 onboard pod: $(echo "$VICTIMS" | tr '\n' ' ')"
  # confirmed-zero-downtime-override: 全是本套脚本自建的一次性 Job pod, 不属于任何 Deployment
  # shellcheck disable=SC2086
  kubectl -n carher delete pod --wait=false $VICTIMS >/dev/null 2>&1
  sleep 10
fi

# ── 2) 节点侧: 退出态容器 + journald 归档 ───────────────────────────────────
# ⚠ 不许写 `2>/dev/null`: 吞掉 stderr 会让"命令压根没执行"读起来像"执行了没事"。
#   出错就要看得见(memory feedback_helper_2devnull_hides_the_error_and_fakes_success)。
"$REPO/scripts/jms" ssh --tty --timeout 500 "$JMS_ASSET" '
ex=$(crictl ps -a --state Exited -q | wc -l)
crictl ps -a --state Exited -q | xargs -r -n20 crictl rm > /tmp/reclaim-rm.log 2>&1
rmerr=$(grep -ci error /tmp/reclaim-rm.log)
vac=$(journalctl --vacuum-size=500M 2>&1 | grep -o "freed [0-9.]*[MG]" | tail -1)
sleep 2
echo "RES exited_removed=$ex rm_errors=$rmerr journal_$vac avail=$(df -B1 --output=avail / | tail -1 | tr -d " ")"
' 2>&1 | grep -o 'RES .*' | tail -1

H2=$(headroom_gb "$NODE_122")
echo "[reclaim] .122 余量 ${H}G → ${H2}G"
python3 -c "import sys; sys.exit(0 if float('$H2')>=float('$WANT_GB') else 1)" || {
  echo "[reclaim] ⚠ 仍不足 ${WANT_GB}G — 节点是真的被合法负载占满了, 别硬跑 Job,"
  echo "          先考虑把 onboard 挪到别的 CF-clean 出口, 或给节点扩盘。"
  exit 1
}
