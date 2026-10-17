#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。来源：2026-10-16~17 v1.105.1 升级逐字搬出，只把写死的
# run 目录 / 版本号 / 签字说明参数化。run 目录由 /root/l3/.gray-run-latest 指向（上一轮是 .up1051-latest）。
# 必需环境：$D/target.env（TARGET_TAG/TARGET_DIGEST/SRC/BACKUP_DIR）、$D/nginx/frozen-env.sh。
# 签字人与授权说明从 $D/signer.env 读（SIGNER=… / SIGN_NOTE=…），不在代码里写死。
# 用法: freeze.sh <gray|prod|guarded-old> <inputs-dir> <digest> <out-values> <live-values-for-replicas>
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); . $D/target.env; umask 077
LANE=$1 I=$2 DIG=$3 OUT=$4 LIVEV=$5
case $LANE in gray) PROF=k8s/values-gray.yaml MODE=disabled SRC_EV="clone:litellm-clone-c-sched-suppressed-20261002";;
  guarded-old) PROF=k8s/values-guarded-old.yaml MODE=disabled SRC_EV="clone:litellm-clone-c-sched-suppressed-20261002";;
  prod) PROF=k8s/values-prod.yaml MODE=primary SRC_EV="direct:litellm-198-prod-reset-budget-owner";; esac
cd $SRC
COMMON=(--profile $PROF --repository 127.0.0.1:5000/litellm-carher --digest $DIG
  --config $I/snap/config/config.yaml --callbacks-dir $I/snap/callbacks --callbacks-volume callbacks
  --snapshot hooks=$I/snap/hooks --snapshot chatgpt-noauth=$I/snap/chatgpt-noauth --snapshot deepcopy-patch=$I/snap/deepcopy-patch
  --snapshot streaming-handler-patch=$I/snap/streaming-handler-patch
  --snapshot passthrough-streaming-handler-patch=$I/snap/passthrough-streaming-handler-patch
  --snapshot anthropic-logging-patch=$I/snap/anthropic-logging-patch
  --deployment $I/prod-deployment.yaml --secret-metadata $D/go-inputs/secret-metadata.json
  --ingress-cidr 10.42.0.0/32 --ingress-cidr 10.42.0.1/32 --ingress-cidr 10.42.0.0/24 --ingress-cidr 10.42.1.0/24
  --ingress-cidr 10.42.2.0/24 --ingress-cidr 10.68.13.0/24 --ingress-cidr 10.42.3.0/24 --ingress-cidr 10.68.10.37/32 --termination-grace-seconds 600)
EV=$OUT.scheduler-evidence-$(date -u +%H%M%SZ).json
python3 scripts/collect-scheduler-evidence.py --mode $MODE --source "$SRC_EV" \
  --duplicate-scheduler-runs 0 --duplicate-background-jobs 0 --unexpected-control-writes 0 --evidence-output $EV "${COMMON[@]}" | tail -1 | cut -c1-160
python3 scripts/prepare-values.py "${COMMON[@]}" --scheduler-evidence $EV --output $OUT.new | tail -1 | cut -c1-250
python3 - $OUT.new $LIVEV $MODE <<'PY'
import yaml,sys
v=yaml.safe_load(open(sys.argv[1])); old=yaml.safe_load(open(sys.argv[2])); v["replicas"]=old["replicas"]
assert v["schedulerSafety"]["mode"]==sys.argv[3]
c=yaml.safe_load(v["config"]["data"]["config.yaml"])
print("FROZEN replicas",v["replicas"],"digest",v["image"]["digest"][:19],"route",v.get("productionRouteEnabled"),"bg",v["backgroundTasks"],"sched",v["schedulerSafety"]["mode"],
 "drb",c["general_settings"].get("disable_reset_budget"),"lrr",c["litellm_settings"].get("log_raw_request_response"),"efb",c["general_settings"].get("enforce_fallback_budget"),"bec",c["litellm_settings"].get("budget_exceeded_status_code"),"ncb",len(v["callbacks"]["data"]))
print("top-level keys differing vs live values:",sorted(k for k in set(v)|set(old) if v.get(k)!=old.get(k)))
for k in ("extraEnv","resources","nodeSelector","tolerations","affinity","topologySpreadConstraints","service","networkPolicy","podLabels"):
    if v.get(k)!=old.get(k): print("  DIFF",k,"\n    old=",str(old.get(k))[:300],"\n    new=",str(v.get(k))[:300])
open(sys.argv[1],"w").write(yaml.safe_dump(v,sort_keys=False))
PY
mv $OUT.new $OUT; chmod 600 $OUT
