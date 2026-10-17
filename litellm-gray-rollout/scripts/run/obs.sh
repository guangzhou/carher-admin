#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。来源：2026-10-16~17 v1.105.1 升级逐字搬出，只把写死的
# run 目录 / 版本号 / 签字说明参数化。run 目录由 /root/l3/.gray-run-latest 指向（上一轮是 .up1051-latest）。
# 必需环境：$D/target.env（TARGET_TAG/TARGET_DIGEST/SRC/BACKUP_DIR）、$D/nginx/frozen-env.sh。
# 签字人与授权说明从 $D/signer.env 读（SIGNER=… / SIGN_NOTE=…），不在代码里写死。
# 只读观察：pilot vs control 失败率（SpendLogs，按 key×model 拆）、nginx 池、gray worker 被杀
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
SINCE=${1:-15}
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest})
{ echo "set statement_timeout='90s'; CREATE TEMP TABLE s(h text); COPY s FROM STDIN;"; cat $D/pilot/pilot.sids; echo '\.'
cat <<SQL
SELECT CASE WHEN api_key IN (SELECT h FROM s) THEN 'pilot' ELSE 'control' END g, count(*) n,
 count(*) FILTER (WHERE status='failure') fail, round(100.0*count(*) FILTER (WHERE status='failure')/nullif(count(*),0),2) pct
FROM "LiteLLM_SpendLogs" WHERE "startTime" > now()-interval '$SINCE minutes' GROUP BY 1 ORDER BY 1;
SELECT 'pilot-fail', left(api_key,8), model_group, count(*) FROM "LiteLLM_SpendLogs" WHERE "startTime" > now()-interval '$SINCE minutes'
 AND api_key IN (SELECT h FROM s) AND status='failure' GROUP BY 2,3 ORDER BY 4 DESC LIMIT 8;
SQL
} | kubectl -n litellm-product exec -i litellm-db-0 -- psql -U litellm -d litellm -q -A -F' | ' 2>&1 | grep -vE '^(CREATE|COPY|SET)'
awk -v cut="$(date -d "$SINCE minutes ago" +%Y-%m-%dT%H:%M:%S)" '{split($1,a,"=");if(substr(a[2],1,19)<cut)next;if($0~/upstream=127.0.0.1:30403/)next;p="-";for(i=1;i<=NF;i++)if($i~/^pool=/){split($i,x,"=");p=x[2]};c[p]++;if($3~/^5/)e[p" "$3]++}END{for(k in c)printf "nginx pool %-8s %6d\n",k,c[k];for(k in e)printf "  5xx %s=%d\n",k,e[k]}' /var/log/nginx/cc-auto-link.gray.log
for P in $(kubectl -n litellm-product get po -l app=litellm-proxy-gray --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'); do
  echo "gray $P worker_killed=$(kubectl -n litellm-product logs $P -c litellm --since=${SINCE}m 2>/dev/null | grep -cE 'Child process \[[0-9]+\] died') rst=$(kubectl -n litellm-product get po $P -o jsonpath='{.status.containerStatuses[0].restartCount}') tb=$(kubectl -n litellm-product logs $P -c litellm --since=${SINCE}m 2>/dev/null | grep -c Traceback)"
done
grep -E 'cycle rc|FAIL' $D/monitor/monitor-loop.log | tail -2 | cut -c1-140
