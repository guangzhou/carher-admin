#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。来源：2026-10-16~17 v1.105.1 升级逐字搬出，只把写死的
# run 目录 / 版本号 / 签字说明参数化。run 目录由 /root/l3/.gray-run-latest 指向（上一轮是 .up1051-latest）。
# 必需环境：$D/target.env（TARGET_TAG/TARGET_DIGEST/SRC/BACKUP_DIR）、$D/nginx/frozen-env.sh。
# 签字人与授权说明从 $D/signer.env 读（SIGNER=… / SIGN_NOTE=…），不在代码里写死。
# 用法: sign.sh <gate> "<basis 1>" ["<basis 2>" ...]   —— 按当前 active state 签一份 gate evidence（0600 root）
set -euo pipefail
G=$1; shift
[ -f "$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest})/signer.env" ] && { set -a; . "$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest})/signer.env"; set +a; }
: "${SIGNER:?signer.env 里没有 SIGNER（必须是真人名）}"
EV=/etc/nginx/gray-route/evidence; A=/etc/nginx/gray-route/active; umask 077
python3 - "$EV/$G.json" "$G" "$(awk -F= '$1=="run_id"{print $2}' $A/state.env)" "$(awk -F= '$1=="generation"{print $2}' $A/state.env)" "$(awk -F= '$1=="config_checksum"{print $2}' $A/state.env)" "$@" <<'PY'
import json,sys,datetime,os
p,g,run,gen,cs,*basis=sys.argv[1:]
basis=basis+([os.environ["SIGN_NOTE"]] if os.environ.get("SIGN_NOTE") else [])
json.dump({"gate":g,"status":"PASS","signer":os.environ["SIGNER"],"run_id":run,"generation":gen,"config_checksum":cs,
 "captured_at":datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),"basis":basis},open(p,"w"),ensure_ascii=False)
PY
chmod 600 "$EV/$G.json"; echo "signed $G"
