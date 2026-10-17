#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。来源：2026-10-16~17 v1.105.1 升级逐字搬出，只把写死的
# run 目录 / 版本号 / 签字说明参数化。run 目录由 /root/l3/.gray-run-latest 指向（上一轮是 .up1051-latest）。
# 必需环境：$D/target.env（TARGET_TAG/TARGET_DIGEST/SRC/BACKUP_DIR）、$D/nginx/frozen-env.sh。
# 签字人与授权说明从 $D/signer.env 读（SIGNER=… / SIGN_NOTE=…），不在代码里写死。
# 用法: ramp.sh <percent> <window-start-UTC> "<依据一句话>"
set -uo pipefail
PCT=$1; WS=$2; BASIS=$3
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); N=$D/nginx; . $D/target.env
set -a; . $N/frozen-env.sh; set +a
EV=/etc/nginx/gray-route/evidence; A=/etc/nginx/gray-route/active; umask 077
RUN=$(awk -F= '$1=="run_id"{print $2}' $A/state.env); GEN=$(awk -F= '$1=="generation"{print $2}' $A/state.env); CS=$(awk -F= '$1=="config_checksum"{print $2}' $A/state.env)
python3 $SRC/scripts/check-monitor-continuity.py --ledger $EV/monitor-heartbeat.jsonl --run-id "$RUN" --generation "$GEN" \
  --config-checksum "$CS" --window-start "$WS" --cycle-interval-seconds 300 --output $EV/split_monitor_continuity.json >/dev/null 2>&1
chmod 600 $EV/split_monitor_continuity.json
python3 -c "import json;d=json.load(open('$EV/split_monitor_continuity.json'));print('continuity',d['status'],d['errors'],'cycles',d.get('cycles_in_window'))"
bash $D/sign.sh split_sample "$BASIS" >/dev/null
bash $SRC/scripts/gray-split-update.sh $PCT 2>&1 | grep -vE 'render-production-nginx|syntax is ok|test is successful' | tail -2
bash $SRC/scripts/gray-phase.sh current | grep -E '^(split|generation)='
echo "health=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -H 'Host: cc.auto-link.com.cn' http://127.0.0.1/pro/health/liveliness) at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
NOW=$(awk -F= '$1=="split"{print $2}' /etc/nginx/gray-route/active/state.env); [ "$NOW" = "$PCT" ] || { echo "STOP: split 未变成 $PCT（现 $NOW）"; exit 1; }
