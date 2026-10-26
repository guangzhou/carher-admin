#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。run 目录由 /root/l3/.gray-run-latest 指向。
# 用法: gates.sh <release> <deployment> <target-manifest> <workdir> [--approve "<consumer>"]
#   例: gates.sh litellm-product-gray litellm-proxy-gray $D/gray/target-manifest.yaml $D/gray
#       gates.sh litellm-product-proxy litellm-proxy $D/prod-target/prod-target-manifest.yaml $D/prod-target --approve "litellm-proxy（§7 收敛）"
# 抓 live（helm manifest + kubectl 活对象 + 挂载的 CM）→ 跑删除集 + Pod 形状两道门禁。
#   不带 --approve：只出 diff（非 inert 项逐条打印），FAIL 是预期的 —— 先看清楚。
#   带 --approve：按下面的固定口径给每条非 inert 项写批准（签字人取 $D/signer.env 的 SIGNER），再跑一遍，必须 PASS。
# 固定口径之外的项（例如 env、probe、resources 变了）一律不自动批，脚本 STOP —— 那是意外差异，要人看。
# 10-26 v1.106.4：gray / prod / guarded-old 三次手写批准文件，口径全一样，所以收进这里。
set -uo pipefail
REL=$1 DEP=$2 TGT=$3 W=$4; shift 4; CONSUMER=""
[[ "${1:-}" == "--approve" ]] && CONSUMER="${2:?--approve 要给 consumer 说明}"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; NS=litellm-product
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); . $D/target.env; umask 077
[ -f $D/signer.env ] && { set -a; . $D/signer.env; set +a; }
A=/etc/nginx/gray-route/active
RUN=$(awk -F= '$1=="run_id"{print $2}' $A/state.env); GEN=$(awk -F= '$1=="generation"{print $2}' $A/state.env)
helm get manifest $REL -n $NS > $W/live-manifest.yaml
kubectl -n $NS get deploy $DEP -o yaml > $W/live-deploy.yaml
python3 - $W/live-deploy.yaml $W/live-cms.yaml $TGT $W/target-cms.yaml <<'PY'
import yaml,sys,subprocess
d=yaml.safe_load(open(sys.argv[1]))
names=sorted({v["configMap"]["name"] for v in d["spec"]["template"]["spec"]["volumes"] if "configMap" in v})
open(sys.argv[2],"w").write(yaml.safe_dump_all([yaml.safe_load(subprocess.run(["kubectl","-n","litellm-product","get","cm",n,"-o","yaml"],capture_output=True,text=True,check=True).stdout) for n in names]))
open(sys.argv[4],"w").write(yaml.safe_dump_all([x for x in yaml.safe_load_all(open(sys.argv[3])) if x and x.get("kind")=="ConfigMap"]))
PY
chmod 600 $W/*.yaml
run_gates() {
  local da=() sa=()
  [ -f $W/deletion-approval.json ] && da=(--approval $W/deletion-approval.json)
  [ -f $W/shape-approval.json ] && sa=(--approval $W/shape-approval.json)
  python3 $SRC/scripts/check-release-deletion-set.py --live-manifest $W/live-manifest.yaml --target-manifest $TGT --release $REL --namespace $NS \
    --run-id $RUN --generation $GEN "${da[@]}" --output $W/deletion-set.json >/dev/null 2>&1
  python3 $SRC/scripts/check-pod-spec-shape.py --live $W/live-deploy.yaml --target $TGT --live-configmaps $W/live-cms.yaml --target-configmaps $W/target-cms.yaml \
    --name $DEP --namespace $NS --run-id $RUN --generation $GEN "${sa[@]}" --output $W/shape.json >/dev/null 2>&1
}
rm -f $W/deletion-approval.json $W/shape-approval.json
run_gates
python3 - $W "$CONSUMER" "${SIGNER:-}" "$TARGET_DIGEST" "${PREV_DIGEST:-}" <<'PY'
import json,sys,yaml
W,consumer,signer,tgt,prev=sys.argv[1:]
live=yaml.safe_load(open(W+"/live-deploy.yaml")); sp=live["spec"]["template"]["spec"]
mounted={m["name"] for c in sp["containers"] for m in c.get("volumeMounts",[])}
d=json.load(open(W+"/deletion-set.json")); s=json.load(open(W+"/shape.json"))
print("DELETION",d["status"],"deleted:",[x["object"].rsplit("/",1)[-1] for x in d["deleted"]])
if any("/Service/" in x["object"] for x in d["deleted"]): print("STOP: 删除集里有 Service"); sys.exit(1)
print("SHAPE",s["status"],s["counts"])
def disposition(i):
    if i=="container/litellm/image": return "镜像换代（见 target.env：TARGET/PREV digest）"
    if i.startswith("volume/") and i.split("/",1)[1] not in mounted: return "只声明未挂载的旧卷（mounted=False，历次升级残留），去掉无害"
    if "/mount/" in i and i.endswith(".py") and not i.startswith("container/litellm/mount//app/.venv"):
        return "callback 挂载随快照增删 / 换 CM（内容见冻结差异）"
    if "streaming_handler.py" in i or "anthropic_passthrough_logging_handler" in i or i.startswith("volume/snapshot-"):
        return "整文件覆盖件换成本版本重做的那套（R10 转正后逐 pod 核）"
    if i in ("container/litellm/mount//app/config.yaml/config.yaml","volume/config","volume/callbacks"):
        return "config / callbacks 内容寻址 CM 换代（内容 = 快照）"
    return None
items=[it for k in ("changed","removed","added") for it in s[k] if isinstance(it,dict) and not it.get("inert")]
odd=[it["item"] for it in items if disposition(it["item"]) is None]
for it in items: print("  ",("??" if disposition(it["item"]) is None else "ok"),it["item"])
if not consumer: sys.exit(0)
if odd: print("STOP: 固定口径之外的形状变化，不自动批：",odd); sys.exit(1)
if not signer: print("STOP: signer.env 没有 SIGNER"); sys.exit(1)
json.dump({"deletions":[{"object":x["object"],"consumer":consumer,"disposition":"内容寻址 CM 换代：新 release 渲染同用途新 CM，旧 CM 由 helm 回收","approver":signer} for x in d["deleted"]]},
          open(W+"/deletion-approval.json","w"),ensure_ascii=False,indent=1)
json.dump({"shape_changes":[{"item":it["item"],"consumer":consumer,"disposition":disposition(it["item"]),"approver":signer} for it in items]},
          open(W+"/shape-approval.json","w"),ensure_ascii=False,indent=1)
print("approvals written:",len(d["deleted"]),"deletions,",len(items),"shape items")
PY
rc=$?; [ $rc = 0 ] || exit $rc
[ -n "$CONSUMER" ] || exit 0
run_gates
python3 -c "import json,sys;a=json.load(open('$W/deletion-set.json'))['status'];b=json.load(open('$W/shape.json'))['status'];print('after approval: deletion',a,'shape',b);sys.exit(0 if a==b=='PASS' else 1)"
