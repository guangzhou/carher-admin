#!/usr/bin/env bash
# ws_incr_fleet_audit.sh — acct WS 增量传输全池体检（2026-08-25 LRU 事故后固化）
#
# 用法（在 198 host 上跑，或从 Mac 管道进去）：
#   ssh cltx@10.68.13.198 'export KUBECONFIG=/home/cltx/.kube/config; bash -s' < ws_incr_fleet_audit.sh
#   ssh ... 'bash -s' < ws_incr_fleet_audit.sh 6h      # 自定义日志窗口（默认 2h）
#
# 输出四段，每段对应一个真踩过的坑：
#   [1] per-pod 分桶 —— 命中率异常必须先看二态分布（全池均值会把"7台全灭"平均成21%骗过你）
#   [2] thrash 指纹 —— evict_lru:full ≈ 1:1 = LRU 被打穿（2026-08-25 实锤：默认32被95并发打穿）
#   [3] 出网削减估算 —— 增量实发字节 vs 全量帧均值折算（与收官报告同方法论）
#   [4] 缓存命中 + 昨日同窗对照 —— 归因必须过对照组（"早高峰天然高"就是这么被证伪的）
#
# 判据速查：
#   - 单 pod sends>50 且 hit<30% → WARN（二态分布下坠翼）
#   - evict_lru > 0 → LRU 上限又不够了（现全池 env CHATGPT_WS_MAX_SESSIONS=512，源码默认同步 512）
#   - distinct_pck 接近上限 → 提前扩
#   - 缓存归因：今日 vs 昨日同 UTC 小时、流量量级相当才可比
set -uo pipefail
WIN="${1:-2h}"
NS=litellm-product
# 凭据必须来自 env，不留硬编码兜底默认值：兜底值等于把真口令提交进仓库，
# 且口令轮转后老默认值会静默继续生效，认证失败看不出是"忘了设 env"还是"口令换了"。
PGPASS="${LITELLM_PG_PW:?需要 LITELLM_PG_PW（litellm-db-0 的 PG 口令，别写进文件/命令行历史）}"

echo "=== [1] per-pod (${WIN}): pod inc full hit% evict_lru distinct_pck ==="
TOT_I=0; TOT_F=0; TOT_E=0; TOT_B=0; WARN=""
for p in $(kubectl -n $NS get pods -o name | grep chatgpt-acct); do
  L=$(kubectl -n $NS logs "$p" --since="$WIN" --tail=-1 2>/dev/null | grep -E "ws_incr mode=|evict=lru")
  i=$(echo "$L" | grep -c "mode=incremental"); f=$(echo "$L" | grep -c "mode=full_ws")
  e=$(echo "$L" | grep -c "evict=lru")
  t=$((i+f)); [ $t -eq 0 ] && continue
  n=$(echo "$L" | grep "ws_incr mode=" | grep -o "pck=[0-9a-f]*" | sort -u | wc -l)
  b=$(echo "$L" | grep "mode=incremental" | grep -o "frame_bytes=[0-9]*" | cut -d= -f2 | awk '{s+=$1} END {print s+0}')
  r=$((100*i/t))
  echo "${p#pod/} inc=$i full=$f hit=${r}% evict_lru=$e pck=$n"
  [ $t -gt 50 ] && [ $r -lt 30 ] && WARN="$WARN ${p#pod/}(hit=${r}%)"
  TOT_I=$((TOT_I+i)); TOT_F=$((TOT_F+f)); TOT_E=$((TOT_E+e)); TOT_B=$((TOT_B+b))
done
echo "--- fleet: inc=$TOT_I full=$TOT_F evict_lru=$TOT_E"
[ $((TOT_I+TOT_F)) -gt 0 ] && echo "--- fleet hit: $((100*TOT_I/(TOT_I+TOT_F)))%"
[ -n "$WARN" ] && echo "⚠️ WARN 低命中 pod（查 evict/no_session/流量归属）:$WARN"
[ $TOT_E -gt 0 ] && echo "⚠️ WARN evict_lru>0：上限又不够，查 distinct_pck vs CHATGPT_WS_MAX_SESSIONS"

echo; echo "=== [2] fallback reasons (${WIN}) ==="
for p in $(kubectl -n $NS get pods -o name | grep chatgpt-acct); do
  kubectl -n $NS logs "$p" --since="$WIN" --tail=-1 2>/dev/null | grep -o "ws_incr_fallback reason=[a-z0-9_]*"
done | sort | uniq -c | sort -rn | head

echo; echo "=== [3] 出网削减估算 (${WIN}, 与收官报告同方法论: HTTP等价=inc次数×本窗全量帧均值) ==="
AF=$(for p in $(kubectl -n $NS get pods -o name | grep chatgpt-acct); do
  kubectl -n $NS logs "$p" --since="$WIN" --tail=-1 2>/dev/null | grep "mode=full_ws" | grep -o "frame_bytes=[0-9]*" | cut -d= -f2
done | awk '{s+=$1; n++} END {print (n>0)? int(s/n) : 0}')
echo "inc_bytes_actual=$((TOT_B/1048576))MB avg_full_frame=$((AF/1024))KB"
[ $AF -gt 0 ] && echo "http_equiv≈$((TOT_I*AF/1048576))MB → saved≈$(( (TOT_I*AF-TOT_B)/1048576 ))MB/${WIN}"

echo; echo "=== [4] 缓存命中逐时 + 昨日同窗对照 (SpendLogs cached_tokens 唯一口径) ==="
SQL="select date_trunc('hour',\"startTime\") as hr, count(*) as reqs,
 round(100.0*sum(coalesce((metadata->'usage_object'->'prompt_tokens_details'->>'cached_tokens')::bigint,0))/nullif(sum(prompt_tokens),0),1) as cache_pct
 from \"LiteLLM_SpendLogs\" where call_type='aresponses' and status='success'
 and (\"startTime\" > now()-interval '6 hours' or \"startTime\" between now()-interval '30 hours' and now()-interval '24 hours')
 group by 1 order by 1;"
echo "$SQL" | kubectl -n $NS exec -i litellm-db-0 -- env PGPASSWORD=$PGPASS psql -U litellm -d litellm -t -f -
echo "(上半段=昨日同窗对照, 下半段=近6h; 归因抬升前先比同UTC小时+流量量级)"
