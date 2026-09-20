#!/usr/bin/env bash
# 198 LiteLLM「GPT 变慢」分层归因量具（只读，不改任何配置）。
#
#   ./latency-triage.sh hourly [HOURS]     逐小时总耗时 / 输入输出规模（不是 TTFT 分解，见下）
#   ./latency-triage.sh controls [MIN]     同窗口对照组:非 chatgpt 上游 + 同账号其他模型
#   ./latency-triage.sh census [HOURS]     母 router 日志普查:WA 决策分布 + 报错模板 + cooldown
#   ./latency-triage.sh collect [HOURS]    抓全池叶子 ws_incr 日志到 /tmp（喂给 ws-log-analyze.py）
#   ./latency-triage.sh deadlegs           找「在被派活但从不成功」的死号
#   ./latency-triage.sh infra              节点/pod/DB 体检（排除"是我们机器"）
#   ./latency-triage.sh all
#
# 判据口径写在 SKILL.md（.claude/skills/litellm-198-latency-triage）。踩过的坑:
#   * psql 必须走 DATABASE_URL 里的真凭据。`-U root` 会 FATAL 但 kubectl exec 吞掉后
#     看起来只是"空结果"——空 ≠ 零请求。所以每个查询前先跑阳性对照。
#   * model_id = 落点部署(chatgpt-acct-N-gpt-5.6-sol)；model / model_group = 请求名。别混。
#   * metadata->'...' 的 jsonb 解引用在 34GB 的 SpendLogs 上会超时，本脚本一律不碰 metadata。
#   * 叶子容器日志高负载下只留 ~25 分钟，`--since=2h` 拿到的是"截至保留边界"，不是 2 小时。
#   * completionStartTime ≠ 首 token 时刻(它约等于 endTime)。SpendLogs 里没有真 TTFT，
#     别拿它拆"等待 vs 吐字"。见记忆 feedback_litellm_completionstarttime_not_ttft。
set -uo pipefail

H198=${H198:-cltx@10.68.13.198}
NS=${NS:-litellm-product}
SSH="ssh -o ConnectTimeout=30 $H198"
CMD=${1:-all}; ARG=${2:-}

remote() { $SSH "NS=$NS bash -s" <<REMOTE_EOF
$(cat)
REMOTE_EOF
}

# 远端通用前奏:解出真 DSN，定义 q()，并强制阳性对照
_dsn_preamble() {
cat <<'PRE'
DU=$(sudo kubectl -n $NS get secret litellm-secrets -o jsonpath='{.data.DATABASE_URL}' 2>/dev/null | base64 -d)
U=$(echo "$DU"|sed -E 's#^[a-z+]+://([^:]+):.*#\1#')
PW=$(echo "$DU"|sed -E 's#^[a-z+]+://[^:]+:([^@]+)@.*#\1#')
D=$(echo "$DU"|sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')
q(){ sudo kubectl -n $NS exec litellm-db-0 -- env PGPASSWORD="$PW" psql -h 127.0.0.1 -U "$U" -d "$D" "$@"; }
# ---- 阳性对照:这一步必须是个正整数，否则下面所有"空结果"都不可信 ----
CTL=$(q -t -A -c "SELECT count(*) FROM \"LiteLLM_SpendLogs\" WHERE \"startTime\">now()-interval '30 minutes';" 2>&1)
case "$CTL" in ''|*[!0-9]*) echo "FATAL 阳性对照失败，凭据/连接不对，下面的空结果不许当读数: $CTL"; exit 2;; esac
echo "[阳性对照] 近 30 分钟 SpendLogs 行数 = $CTL"
PRE
}

do_hourly() {
  local hrs=${ARG:-20}
  echo "=== 逐小时(gpt-5.6 家族, status=success, 近 ${hrs}h) ==="
  echo "!! ⛔ ttft_s / gen_s 两列不许当证据:completionStartTime 不是首 token 时刻,"
  echo "!!    它几乎等于 endTime(09-09 实测 45% 完全相等、gen p90 0.24s)。gen≈0 是记账假象。"
  echo "!!    打印它们只是为了让你认出这个假象。承重的列是 total_s / in_ktok / out_tok:"
  echo "!!    输入没涨而 total_s 涨 = 单位输入变贵了 ⇒ 上游嫌疑。要拆等待vs吐字用叶子直打探针。"
  { _dsn_preamble; cat <<SQL
q -c "SET statement_timeout='150s';
SELECT to_char(date_trunc('hour',\"startTime\"),'MM-DD HH24') h, count(*) n,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY request_duration_ms)::numeric/1000,1) total_s,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from (\"completionStartTime\"-\"startTime\")))::numeric,1) ttft_s,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch from (\"endTime\"-\"completionStartTime\")))::numeric,1) gen_s,
  round(avg(prompt_tokens)::numeric/1000,0) in_ktok,
  round(avg(completion_tokens)::numeric,0) out_tok
FROM \"LiteLLM_SpendLogs\"
WHERE \"startTime\" > now()-interval '${hrs} hours' AND status='success'
  AND model_group ILIKE '%5.6%' AND \"completionStartTime\" IS NOT NULL
GROUP BY 1 ORDER BY 1;"
echo '--- 尺子自检:完全相等的比例 / 生成时长的真实分位 ---'
q -c "SET statement_timeout='150s';
SELECT count(*) n,
  count(*) FILTER (WHERE \"completionStartTime\"=\"endTime\") eq_endtime,
  round(percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch from (\"endTime\"-\"completionStartTime\")))::numeric,2) gen_p90,
  round(max(extract(epoch from (\"endTime\"-\"completionStartTime\")))::numeric,2) gen_max
FROM \"LiteLLM_SpendLogs\"
WHERE \"startTime\">now()-interval '90 minutes' AND status='success' AND model_group ILIKE '%5.6%';"
SQL
  } | remote
}

do_controls() {
  local m=${ARG:-90}
  echo "=== 对照组(近 ${m} 分钟) —— 用来证伪「是我们的 proxy 慢」和「是这个模型慢」 ==="
  { _dsn_preamble; cat <<SQL
echo '--- 同一批 chatgpt 账号叶子上，按落点模型分列(model_id 才是部署) ---'
q -c "SET statement_timeout='150s';
SELECT regexp_replace(model_id,'^chatgpt-acct-[0-9]+-','') AS landing, count(*) n,
 round(percentile_cont(0.5) WITHIN GROUP (ORDER BY request_duration_ms)::numeric/1000,1) p50_s,
 round(percentile_cont(0.9) WITHIN GROUP (ORDER BY request_duration_ms)::numeric/1000,1) p90_s,
 round(avg(prompt_tokens)::numeric/1000,0) in_ktok
FROM \"LiteLLM_SpendLogs\"
WHERE \"startTime\">now()-interval '${m} minutes' AND status='success' AND model_id LIKE 'chatgpt-acct-%'
GROUP BY 1 HAVING count(*)>20 ORDER BY p50_s DESC;"
echo '--- 同一台 proxy 上的非 chatgpt 上游(输入 token 要一起看，否则不可比) ---'
q -c "SET statement_timeout='150s';
SELECT model_group, count(*) n,
 round(percentile_cont(0.5) WITHIN GROUP (ORDER BY request_duration_ms)::numeric/1000,1) p50_s,
 round(avg(prompt_tokens)::numeric/1000,0) in_ktok
FROM \"LiteLLM_SpendLogs\"
WHERE \"startTime\">now()-interval '${m} minutes' AND status='success' AND model_id NOT LIKE 'chatgpt-acct-%'
GROUP BY 1 HAVING count(*)>40 ORDER BY n DESC LIMIT 12;"
SQL
  } | remote
}

do_census() {
  local hrs=${ARG:-2}
  echo "=== 母 router 日志普查(近 ${hrs}h) ==="
  remote <<CENSUS
F=/tmp/mom-triage-\$\$.txt
for p in \$(sudo kubectl -n \$NS get pods -o name 2>/dev/null | grep litellm-proxy); do
  sudo kubectl -n \$NS logs "\$p" --since=${hrs}h --tail=-1 2>/dev/null
done > \$F
echo "日志行数 \$(wc -l < \$F)"
echo
echo '--- WA 亲和决策分布(HIT / pin不健康 / pin缺失 —— 黏性命中率看这里) ---'
grep -oE "WeightedAffinityRouter.*" \$F \
 | sed -E 's/chatgpt-acct-[0-9]+-[a-z0-9.\-]+/<DEP>/g; s/[0-9a-f]{8,}/<HASH>/g; s/[0-9]+\.[0-9]+/<F>/g; s/\b[0-9]+\b/<N>/g' \
 | sort | uniq -c | sort -rn | head -20
echo
echo '--- 异常类型分布(谁在触发 cooldown) ---'
grep -oE "litellm\.[A-Za-z]*Error" \$F | sort | uniq -c | sort -rn | head -10
echo
echo '--- 上游原话(报错脱敏会把大部分打成 ***，这几条是漏网的真文本) ---'
grep -oiE "servers are currently overloaded|Unknown parameter: '[^']*'|reasoning_text[^\"]{0,60}|Concurrency limit[^\"]{0,40}|stream ended with no terminal event[^\"]{0,30}" \$F \
 | sed -E "s/input\[[0-9]+\]/input[N]/" | sort | uniq -c | sort -rn | head -12
echo
echo '--- 当前 cooldown 名单(按账号) ---'
grep -oE "cooldown_list=\[[^]]*\]" \$F | tail -1 | tr ',' '\n' \
 | grep -oE "chatgpt-acct-[0-9]+" | sort | uniq -c | sort -rn | head -15
echo
echo "(原始日志留在远端 \$F，要深挖自己去 grep)"
CENSUS
}

do_collect() {
  local hrs=${ARG:-2}
  echo "=== 抓全池叶子 ws_incr 日志(带 acct 标签) ==="
  echo "!! 高负载 pod 只留 ~25 分钟日志，--since=${hrs}h 拿到的是保留边界内的全部，不是真 ${hrs}h"
  remote <<COLLECT
OUT=/tmp/wspck-\$(date +%H%M).txt
for p in \$(sudo kubectl -n \$NS get pods -o name 2>/dev/null | grep chatgpt-acct); do
  a=\$(echo "\$p" | sed -E 's#pod/chatgpt-acct-([0-9]+)-.*#\1#')
  sudo kubectl -n \$NS logs "\$p" --since=${hrs}h --tail=-1 2>/dev/null | grep -E "^ws_incr mode=" | sed "s/^/acct=\$a /"
done > \$OUT
wc -l \$OUT
echo "下一步: scp \$H198:\$OUT /tmp/ && python3 ws-log-analyze.py /tmp/\$(basename \$OUT)"
COLLECT
}

do_deadlegs() {
  echo "=== 死号排查:在被 WA 派活、但从不成功的账号 ==="
  echo "!! 判据是「成功数 = 0」，不是「失败数很大」——SpendLogs 严重漏记失败"
  echo "!! (实测 2h 只记 79 条，而母 router 日志里有 5733 次 MidStreamFallbackError)。"
  echo "!! ⚠️ 成功数为 0 的号在 GROUP BY 里**根本不出现一行**，所以必须做集合差，"
  echo "!!    不能只看「升序列表的头几行」——那样恰好看不见真正的死号。"
  remote <<'DEAD'
DU=$(sudo kubectl -n $NS get secret litellm-secrets -o jsonpath='{.data.DATABASE_URL}' 2>/dev/null | base64 -d)
U=$(echo "$DU"|sed -E 's#^[a-z+]+://([^:]+):.*#\1#')
PW=$(echo "$DU"|sed -E 's#^[a-z+]+://[^:]+:([^@]+)@.*#\1#')
D=$(echo "$DU"|sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')
q(){ sudo kubectl -n $NS exec litellm-db-0 -- env PGPASSWORD="$PW" psql -h 127.0.0.1 -U "$U" -d "$D" "$@"; }
CTL=$(q -t -A -c "SELECT count(*) FROM \"LiteLLM_SpendLogs\" WHERE \"startTime\">now()-interval '30 minutes';" 2>&1)
case "$CTL" in ''|*[!0-9]*) echo "FATAL 阳性对照失败: $CTL"; exit 2;; esac
echo "[阳性对照] 近 30 分钟 SpendLogs 行数 = $CTL"

# 活着的 acct pod（分母）
sudo kubectl -n $NS get pods --field-selector=status.phase=Running -o name 2>/dev/null \
  | sed -nE 's#pod/chatgpt-acct-([0-9]+)-.*#\1#p' | sort -u > /tmp/_live_accts   # comm 要字典序，不能用 -n

# 近 2h 有过成功的 acct（分子）
q -t -A -c "SET statement_timeout='150s';
SELECT DISTINCT substring(model_id from 'chatgpt-acct-([0-9]+)-')
FROM \"LiteLLM_SpendLogs\"
WHERE \"startTime\">now()-interval '2 hours' AND status='success' AND model_id LIKE 'chatgpt-acct-%';" \
  | grep -E '^[0-9]+$' | sort -u > /tmp/_ok_accts

echo "  活 pod $(wc -l < /tmp/_live_accts) 个 / 近 2h 有过成功的 $(wc -l < /tmp/_ok_accts) 个"

# WA 近 2h 的实际派单数 —— 零成功 + 被派单 才叫「在烧请求」；
# 零成功 + 零派单 只是没摘干净的僵尸 pod，跟延迟无关，别混为一谈。
F=/tmp/_mom_pick.txt
for p in $(sudo kubectl -n $NS get pods -o name 2>/dev/null | grep litellm-proxy); do
  sudo kubectl -n $NS logs "$p" --since=2h --tail=-1 2>/dev/null
done | grep -oE "weighted-pick deployment=chatgpt-acct-[0-9]+" > $F

echo
echo "--- ★ 真死号：零成功 且 仍在被 WA 派单（每次派单 = 一次必然失败 + 一次 cooldown） ---"
FOUND=0
for a in $(comm -23 /tmp/_live_accts /tmp/_ok_accts); do
  pk=$(grep -c "acct-$a\$" $F)
  [ "$pk" -eq 0 ] && continue
  p=$(sudo kubectl -n $NS get pods -o name 2>/dev/null | grep "chatgpt-acct-$a-" | head -1)
  u=$(sudo kubectl -n $NS logs "$p" --since=2h --tail=-1 2>/dev/null \
      | grep -ac "401 Unauthorized' for url 'https://chatgpt.com/backend-api/codex/responses'")
  echo "  acct-$a  被派单 ${pk} 次  成功 0 次  叶子 401 ${u} 次"
  FOUND=1
done
[ "$FOUND" = 0 ] && echo "  (空 = 没有真死号)"

echo
echo "--- 僵尸 pod：零成功 且 零派单（路由里已经没有它们，不影响延迟，只占资源） ---"
Z=""
for a in $(comm -23 /tmp/_live_accts /tmp/_ok_accts); do
  [ "$(grep -c "acct-$a\$" $F)" -eq 0 ] && Z="$Z $a"
done
echo "  acct:${Z:- 无}"
DEAD
}

do_infra() {
  echo "=== 机器体检(证伪「是我们扛不住」) ==="
  remote <<'INFRA'
echo '--- 盘 ---'; df -h / /Data 2>/dev/null
echo '--- 负载 ---'; uptime; echo "cores=$(nproc)"
echo '--- 母 proxy 资源 + 重启次数 ---'
sudo kubectl -n $NS top pod 2>/dev/null | grep -E "NAME|litellm-proxy"
sudo kubectl -n $NS get pods 2>/dev/null | grep litellm-proxy
echo '--- limits ---'
sudo kubectl -n $NS get deploy litellm-proxy -o jsonpath='{.spec.template.spec.containers[0].resources}{"\n"}'
INFRA
  echo
  echo "--- DB 体积(09-08 盘满 502 的同一源头:STORE_PROMPTS_IN_SPEND_LOGS) ---"
  { _dsn_preamble; cat <<'SQL'
q -t -A -F'|' -c "SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid))
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 6;"
SQL
  } | remote
  $SSH "sudo kubectl -n $NS get deploy litellm-proxy -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{\"\n\"}{end}'" 2>/dev/null | grep -iE "STORE_PROMPTS|LITELLM_LOG|AFFINITY"
}

case "$CMD" in
  hourly)   do_hourly ;;
  controls) do_controls ;;
  census)   do_census ;;
  collect)  do_collect ;;
  deadlegs) do_deadlegs ;;
  infra)    do_infra ;;
  all)      do_infra; echo; ARG=20 do_hourly; echo; ARG=90 do_controls; echo; ARG=2 do_census ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
