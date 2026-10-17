#!/bin/bash
# 用 10-17 run c 的真实 generation 序列造一个只读沙盒（不含 key 明文），驾驶台指向它做回放。
set -uo pipefail
SB=/root/l3/console-sandbox; rm -rf $SB; mkdir -p -m 700 $SB/gray-route/generations $SB/gray-route/evidence $SB/run/monitor $SB/run/console
RUN=litellm-198-v1051-20261017c; WANT=${1:-10}
GENS=$(grep -l "^run_id=$RUN\$" /etc/nginx/gray-route/generations/*/state.env | xargs -n1 dirname | sort)
CUR=""
for d in $GENS; do
  n=$(basename $d); mkdir -p $SB/gray-route/generations/$n
  grep -vE 'checksum' $d/state.env > $SB/gray-route/generations/$n/state.env
  for m in force-gray force-prod protected-prod key-sid; do
    c=$(grep -cvE '^\s*(#|$)' $d/$m.map 2>/dev/null || echo 0)
    : > $SB/gray-route/generations/$n/$m.map; for i in $(seq 1 $c); do echo "x$i 1;" >> $SB/gray-route/generations/$n/$m.map; done
  done
  s=$(awk -F= '$1=="split"{print $2}' $d/state.env); p=$(awk -F= '$1=="phase"{print $2}' $d/state.env)
  if [ "$s" = "$WANT" ] && [ "$p" = normal_gray ]; then CUR=$n; break; fi
done
[ -n "$CUR" ] || { echo "no generation at split=$WANT"; exit 1; }
ln -s $SB/gray-route/generations/$CUR $SB/gray-route/active
# 让「当前档开始时间」= 31 分钟前：把这一档的 generation 名字改成现在-31min
NEWT=$(date -u -d '31 minutes ago' +%Y%m%dT%H%M%SZ)
NEWN="g${NEWT}-1-1"; mv $SB/gray-route/generations/$CUR $SB/gray-route/generations/$NEWN; ln -sfn $SB/gray-route/generations/$NEWN $SB/gray-route/active
sed -i "s/^generation=.*/generation=$NEWN/" $SB/gray-route/generations/$NEWN/state.env
for i in 1 2 3 4 5 6; do echo "$(date -u -d "$((31-5*i)) minutes ago" +%Y-%m-%dT%H:%M:%SZ) cycle rc=0 split=$WANT phase=normal_gray" >> $SB/run/monitor/monitor-loop.log; done
printf '%s' "$SB/run" > $SB/ptr
printf '#!/bin/bash\necho "[sandbox] step $@ (no-op, 不碰任何路由)"; sleep 2\n' > $SB/run/step.sh
grep -E '^(phase|split|generation)=' $SB/gray-route/active/state.env
echo "sandbox ok: $SB"
