#!/usr/bin/env bash
# 灰度切流后的双尺子验证 + 容量体检。在 198 上跑。
#
# 用法: verify-rollout.sh <run_id> [观察分钟数，默认10]
#
# 两把尺子，缺一不可:
#   尺子一 = 映射表 sid 清单（证明"配置里有")
#   尺子二 = nginx 真流量 pool 分布（证明"请求真的走过去了"）
# 只有尺子一叫"脚本回显"，不算验证。
set -uo pipefail

RUN_ID="${1:-}"
MINS="${2:-10}"
[[ -n "$RUN_ID" ]] || { echo "用法: $0 <run_id> [分钟数]" >&2; exit 2; }

RUN="/root/litellm-gray-run/$RUN_ID"
S="$RUN/src-3914155/litellm-gray-rollout/scripts/gray-key-route.sh"
G=/var/log/nginx/cc-auto-link.gray.log

set -a
# shellcheck disable=SC1090
. "$RUN/nginx/frozen-env.sh"
set +a

echo "########## 尺子一：映射表 ##########"
OUT=/root/l3/live.sids
bash "$S" list 2>&1 | sed -n 's/.*sid=\([0-9a-f]*\).*/\1/p' | sort > "$OUT"
echo "sid 总数: $(wc -l < "$OUT")   已写入 $OUT"
echo "→ 拿它跟 <out>_expect.json 逐条比。判据 = 每个 expect sid 都在这里面。"
echo "  ⚠️ 判据不是 'sids == 原有+本批' —— 跟前批重叠的 key 返回 unchanged 不新增 sid。"
echo

echo "########## nginx 配置自检 ##########"
nginx -t 2>&1 | tail -1
echo

echo "########## 尺子二：近 ${MINS} 分钟真流量 ##########"
# 排除 30403（ws-ingress 长连接）—— 那些是 WebSocket，101 是正常握手，
# 混进来会把 pool 占比和错误率都算歪
awk -v cut="$(date -d "$MINS minutes ago" +%Y-%m-%dT%H:%M:%S)" '
  {split($1,a,"="); if(a[2]<cut) next
   if($0 ~ /upstream=127.0.0.1:30403/) next
   p="-"; for(i=1;i<=NF;i++) if($i ~ /^pool=/){split($i,x,"="); p=x[2]}
   c[p]++
   if($3 !~ /^2/ && $3 !~ /^3/) e[p" "$3]++
   if(p=="canary") d[++nc]=$4 }
  END{
    t=0; for(k in c) t+=c[k]
    if(t==0){print "  窗口内无推理流量（可能是深夜/无人使用）"; exit}
    for(k in c) printf "  pool %-12s %6d  (%.1f%%)\n",k,c[k],100*c[k]/t
    print "  --- 非 2xx/3xx ---"
    n=0; for(k in e){printf "    %s = %d\n",k,e[k]; n++}
    if(n==0) print "    无"
  }' "$G"
echo
echo "  ⚠️ 流量占比 != key 占比。按业务字段选人时偏差会很大"
echo "     （实测：切 20% 的 key 吃到 35% 流量，切 50% 吃到 50.8%）"
echo

echo "########## canary/stable 时延对照 ##########"
for pool in canary stable; do
  awk -v cut="$(date -d "$MINS minutes ago" +%Y-%m-%dT%H:%M:%S)" -v P="$pool" '
    {split($1,a,"="); if(a[2]<cut) next
     if($0 ~ /upstream=127.0.0.1:30403/) next
     if($0 !~ "pool="P) next
     print $4}' "$G" | sort -n | awk -v P="$pool" '
      {v[NR]=$1}
      END{if(NR>0) printf "  %-8s n=%-5d p50=%-9s p95=%-9s max=%s\n",P,NR,v[int(NR*0.5)+1],v[int(NR*0.95)+1],v[NR]
          else printf "  %-8s 无样本\n",P}'
done
echo
echo "  ⚠️ 两边 p50 差很多不等于新版本慢 —— 样本不是同一批人。"
echo "     判版本快慢必须同一批人前后自比 + 未迁移的人做对照组（SKILL.md §9）。"
echo

echo "########## 容量体检 ##########"
kubectl -n litellm-product get deploy litellm-proxy litellm-proxy-gray \
  -o custom-columns=NAME:.metadata.name,REP:.spec.replicas,READY:.status.readyReplicas,CPULIM:'.spec.template.spec.containers[0].resources.limits.cpu',MEMLIM:'.spec.template.spec.containers[0].resources.limits.memory' 2>&1
echo "--- 在服务的车道 pod（按路由标签，不按名字）---"
kubectl -n litellm-product top pod -l carher.net/litellm-production-route=enabled --no-headers 2>&1
kubectl -n litellm-product get pods -l carher.net/litellm-production-route=enabled \
  -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName,RST:.status.containerStatuses[0].restartCount --no-headers 2>&1
echo "--- 全部 proxy 类 pod（含已排空的车道，用来看谁还剩着）---"
kubectl -n litellm-product get pods -l 'app in (litellm-proxy,litellm-proxy-gray,litellm-proxy-guarded-old)' \
  -o custom-columns=POD:.metadata.name,READY:.status.containerStatuses[0].ready,NODE:.spec.nodeName --no-headers 2>&1
echo "--- 节点 ---"
kubectl top nodes 2>&1
echo "--- 节点压力（True 才是真有压力，看百分比会误判）---"
kubectl get nodes -o custom-columns=NAME:.metadata.name,MEM:'.status.conditions[?(@.type=="MemoryPressure")].status',DISK:'.status.conditions[?(@.type=="DiskPressure")].status' 2>&1
echo
echo "  ⚠️ gray 被 nodeSelector 硬钉在 standby 上，副本再多也只在那一台。"
echo "     整台机器挂了新版本就全没。198 那台内存常年 75%，永远别往上加东西。"
