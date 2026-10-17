#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。来源：2026-10-16~17 v1.105.1 升级逐字搬出，只把写死的
# run 目录 / 版本号 / 签字说明参数化。run 目录由 /root/l3/.gray-run-latest 指向（上一轮是 .up1051-latest）。
# 必需环境：$D/target.env（TARGET_TAG/TARGET_DIGEST/SRC/BACKUP_DIR）、$D/nginx/frozen-env.sh。
# 签字人与授权说明从 $D/signer.env 读（SIGNER=… / SIGN_NOTE=…），不在代码里写死。
# 用法: step.sh <next_pct> <dwell_minutes>
# 当前档至少驻留 dwell 分钟 + 本 generation ≥5 个 monitor 周期全 rc=0 后：
#   判据（任一不过 ⇒ STOP，不升档，打印原因）：
#     gray 每 pod worker_killed=0、restart 不增、Traceback 不暴涨；pilot∪gray 池 5xx 率 ≤ stable 池 5xx 率 + 1pp；
#     monitor 最近 5 周期无 rc!=0
#   ≥50 先跑 check-split-capacity 并签 split_bridge（guarded-old 必须全 Ready）
set -uo pipefail
NEXT=$1; DWELL=$2
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); N=$D/nginx; . $D/target.env; M=$D/monitor
set -a; . $N/frozen-env.sh; set +a
A=/etc/nginx/gray-route/active; EV=/etc/nginx/gray-route/evidence; umask 077
GEN=$(awk -F= '$1=="generation"{print $2}' $A/state.env); CUR=$(awk -F= '$1=="split"{print $2}' $A/state.env)
T0=$(date -u +%s); WS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "[$WS] step $CUR -> $NEXT dwell=${DWELL}m gen=$GEN"
R0=$(kubectl -n litellm-product get po -l app=litellm-proxy-gray -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{" "}{end}')
while :; do
  el=$(( ($(date -u +%s)-T0)/60 ))
  ok=$(awk -v ws="$WS" '$1>=ws' $M/monitor-loop.log | grep -c 'cycle rc=0')
  bad=$(awk -v ws="$WS" '$1>=ws' $M/monitor-loop.log | grep -cE 'cycle rc=[1-9]|FAIL')
  grep -q "^phase=rolled_back" $A/state.env && { echo "STOP: run 已被自动回退（phase=rolled_back）"; exit 1; }
  [ "$bad" -gt 0 ] && { echo "STOP: monitor 有失败周期 ($bad)"; awk -v ws="$WS" '$1>=ws' $M/monitor-loop.log | grep -E 'rc=[1-9]|FAIL' | tail -3; exit 1; }
  [ "$el" -ge "$DWELL" ] && [ "$ok" -ge 5 ] && break
  sleep 60
done
SINCE=$(( ($(date -u +%s)-T0)/60 + 1 ))
K=0; for P in $(kubectl -n litellm-product get po -l app=litellm-proxy-gray --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'); do
  k=$(kubectl -n litellm-product logs $P -c litellm --since=${SINCE}m 2>/dev/null | grep -cE 'Child process \[[0-9]+\] died'); K=$((K+k)); done
R1=$(kubectl -n litellm-product get po -l app=litellm-proxy-gray -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{" "}{end}')
read GN G5 SN S5 <<<$(awk -v cut="$(date -d "$SINCE minutes ago" +%Y-%m-%dT%H:%M:%S)" '{split($1,a,"=");if(substr(a[2],1,19)<cut)next;if($0~/upstream=127.0.0.1:30403/)next;p="-";for(i=1;i<=NF;i++)if($i~/^pool=/){split($i,x,"=");p=x[2]};if(p=="canary"){g++;if($3~/^5/)g5++}else if(p=="stable"){s++;if($3~/^5/)s5++}}END{printf "%d %d %d %d",g,g5,s,s5}' /var/log/nginx/cc-auto-link.gray.log)
GR=$(python3 -c "print(round(100*$G5/max($GN,1),2))"); SR=$(python3 -c "print(round(100*$S5/max($SN,1),2))")
echo "  window ${SINCE}m: gray n=$GN 5xx=$G5 (${GR}%) | stable n=$SN 5xx=$S5 (${SR}%) | worker_killed=$K | restarts $R0-> $R1 | monitor ok=$ok"
[ "$K" -eq 0 ] || { echo "STOP: worker_killed=$K"; exit 1; }
[ "$R0" == "$R1" ] || { echo "STOP: gray restart 变了"; exit 1; }
python3 -c "import sys;sys.exit(0 if $GR <= $SR + 1.0 or $GN < 50 else 1)" || { echo "STOP: gray 5xx 率高于 stable 1pp 以上"; exit 1; }
BASIS="${CUR}% 档 ${SINCE} 分钟：gray n=$GN 5xx ${GR}% vs stable ${SR}%；worker_killed=0；restart 不变；monitor ${ok} 周期全 rc=0"
if [ "$NEXT" -ge 50 ]; then
  RUN=$(awk -F= '$1=="run_id"{print $2}' $A/state.env); CS=$(awk -F= '$1=="config_checksum"{print $2}' $A/state.env)
  rm -f $EV/split_capacity.json
  tail -c 80000000 /var/log/nginx/cc-auto-link.gray.log | tail -n +2 > $M/raw/cap-access.log
  python3 $SRC/scripts/check-split-capacity.py --access-log $M/raw/cap-access.log --readiness $M/raw/readiness.json --run-id "$RUN" --generation "$GEN" \
    --config-checksum "$CS" --target-split $NEXT --per-container-concurrency ${GRAY_PER_CONTAINER_CONCURRENCY:?实测单 pod 并发上限（10-17 生产实测 30~49 并发 / ~1 核）} --headroom-fraction 0.3 --window-seconds 7200 \
    $(for k in ${GRAY_IMPUTE_CLASSES:-messages embedding responses}; do printf -- '--impute-class-cost-from-stable %s ' $k; done) --output $EV/split_capacity.json > $M/cap.out 2>&1
  chmod 600 $EV/split_capacity.json 2>/dev/null; shred -u $M/raw/cap-access.log
  python3 -c "import json;d=json.load(open('$EV/split_capacity.json'));print('  capacity',d['status'],d['errors'],'max_split',d.get('max_supportable_split'),'util',d.get('utilisation'))" || { cat $M/cap.out | tail -3; exit 1; }
  python3 -c "import json,sys;d=json.load(open('$EV/split_capacity.json'));sys.exit(0 if d['status']=='PASS' and d['run_id']=='$RUN' and d['generation']=='$GEN' else 1)" || { echo "STOP: capacity"; exit 1; }
  GO=$(kubectl -n litellm-product get deploy litellm-proxy-guarded-old -o jsonpath='{.status.readyReplicas}/{.spec.replicas}')
  H=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:30406/health/liveliness)
  [ "$GO" == "2/2" ] && [ "$H" == "200" ] || { echo "STOP: guarded-old $GO health=$H"; exit 1; }
  bash $D/sign.sh split_bridge "guarded-old $GO Ready、30406 liveliness $H（回退时 bridge 承接）" >/dev/null
fi
bash $D/ramp.sh $NEXT $WS "$BASIS" 2>&1 | grep -vE 'syntax is ok|test is successful'
