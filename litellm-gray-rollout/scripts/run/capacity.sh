#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。run 目录由 /root/l3/.gray-run-latest 指向。
# 用法: capacity.sh <target_split 50|100>
# 现量一份绑定当前 run + generation 的 split_capacity.json（与 step.sh ≥50 档同一条命令、同一组参数）。
#
# 为什么单独拎出来（10-26 v1.106.4）：每次 split / 名单事务都会轮转 generation，上一档量的容量证据随之作废，
# 驾驶台就停在「容量还没测（升 50/100 前必须测）」——它只读不量。手工拼命令容易漏参数（并发、impute class）。
# 只写 evidence/split_capacity.json，不碰路由。
set -uo pipefail
TARGET=${1:?用法: capacity.sh <50|100>}
[[ "$TARGET" =~ ^(50|100)$ ]] || { echo "target 只能是 50 或 100" >&2; exit 2; }
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); . $D/target.env; M=$D/monitor
EV=/etc/nginx/gray-route/evidence; A=/etc/nginx/gray-route/active; umask 077
RUN=$(awk -F= '$1=="run_id"{print $2}' $A/state.env); GEN=$(awk -F= '$1=="generation"{print $2}' $A/state.env)
CS=$(awk -F= '$1=="config_checksum"{print $2}' $A/state.env)
tail -c 80000000 /var/log/nginx/cc-auto-link.gray.log | tail -n +2 > $M/raw/cap-access.log
python3 $SRC/scripts/check-split-capacity.py --access-log $M/raw/cap-access.log --readiness $M/raw/readiness.json --run-id "$RUN" --generation "$GEN" \
  --config-checksum "$CS" --target-split $TARGET \
  --per-container-concurrency ${GRAY_PER_CONTAINER_CONCURRENCY:?实测单 pod 并发上限（10-17 生产实测 30~49 并发 / ~1 核）⇒ 一般给 50} \
  --headroom-fraction 0.3 --window-seconds 7200 \
  $(for k in ${GRAY_IMPUTE_CLASSES:-messages embedding responses}; do printf -- '--impute-class-cost-from-stable %s ' $k; done) \
  --output $EV/split_capacity.json > $M/cap.out 2>&1
chmod 600 $EV/split_capacity.json 2>/dev/null; shred -u $M/raw/cap-access.log
python3 -c "import json,sys;d=json.load(open('$EV/split_capacity.json'));print('capacity',d['status'],d['errors'],'max_split',d.get('max_supportable_split'),'util',d.get('utilisation'),'bound',d['run_id']=='$RUN' and d['generation']=='$GEN');sys.exit(0 if d['status']=='PASS' else 1)" \
  || { tail -3 $M/cap.out; exit 1; }
