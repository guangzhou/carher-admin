#!/bin/bash
# litellm-198-proxy-migrate-offmaster.sh — 把 litellm-proxy 副本滚动迁离 198 master
#
# 在 198 host 上以 sudo 跑。实证来源：2026-08-25 迁移后 198 内存 65Gi→52.4Gi（-12Gi）。
#
# 机制说明：
# - 软亲和（preferred, weight=100, role In [standby,new-node]）而非硬亲和：
#   standby/242 全挂时 proxy 仍可回落 198，不牺牲容灾。
#   实测 4 副本滚完全部落 225×2 + 242×2，198 清零（242 空节点资源打分本来就高）。
# - tolerations 是无 mergeKey 的 list，strategic patch 整体替换 —— 必须把
#   已有的 standby toleration 一起写全，只写新增项会把旧的顶掉。
# - 滚动策略沿用 deploy 自带 maxSurge=0/maxUnavailable=1（一次一个，零中断）。
# - nginx 入口不受影响：litellm-proxy-nodeport 是 externalTrafficPolicy=Cluster，
#   198 上 0 个 proxy 也能被 kube-proxy 转发（迁移前已验证，勿再猜）。
#
# 前置（缺一不许跑）：
# 1. scripts/litellm-198-node-egress-preflight.sh 目标机 vs 198 逐行同码
# 2. scripts/litellm-198-proxy-node-canary.sh 全绿
set -euo pipefail

NS=litellm-product

kubectl -n $NS patch deploy litellm-proxy --type=strategic -p '
spec:
  template:
    spec:
      tolerations:
      - {key: dedicated, operator: Equal, value: standby, effect: NoSchedule}
      - {key: dedicated, operator: Equal, value: new-node, effect: NoSchedule}
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            preference:
              matchExpressions:
              - {key: role, operator: In, values: ["standby", "new-node"]}
'
kubectl -n $NS rollout status deploy/litellm-proxy --timeout=600s

echo "=== 分布（预期 198 上 0 个）==="
kubectl -n $NS get pods -l app=litellm-proxy -o wide
echo "=== 入口冒烟 ==="
for i in 1 2 3 4; do curl -s -o /dev/null -w "%{http_code} " --max-time 10 http://127.0.0.1:30402/health/liveliness; done; echo
echo "=== 节点内存 ==="
kubectl top nodes
echo "# 收尾验证：等 1-2 分钟后看新节点 proxy 日志有真实流量且无 error 刷屏"
echo "#   kubectl -n $NS logs <新节点pod> --tail=100 --since=3m"
