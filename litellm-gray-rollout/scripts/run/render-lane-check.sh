#!/bin/bash
# 用升级 run 自己的「快照 → prepare-values → helm template」链渲染一条车道，**不 helm upgrade**，再和 live 逐项比。
# 证明：下一次任何人对这条车道走 helm（tune / swap / 整版本升级），结果 == 现在线上 ——
# 不会把别的会话的带外改动冲掉，也不会把 pod 指回已删的 secret。
#
#   render-lane-check.sh <gray|prod|guarded-old> [run_dir]      198 root；run_dir 缺省读 /root/l3/.gray-run-latest
#
# 比的项：image digest、envFrom、env（非 valueFrom）、mounts、callbacks CM 每个文件、config.yaml、replicas。
# 退出码 0 = RENDER == LIVE；1 = DIFF（逐项打印）或渲染失败。
# 输入约定（v1.106.4 run 的目录形状）：
#   prod        $D/prod-target/{snap,prod-deployment.yaml}  replicas 取 $D/prod-target/prod-target-values.yaml
#   gray        $D/gray-inputs/{snap,gray-deployment.yaml}  replicas 取 $D/gray/gray-values.yaml
#   guarded-old $D/go-inputs/{snap,prod-deployment.yaml}    replicas 取 $D/go/go-values.yaml
#   secret-metadata 一律 $D/go-inputs/secret-metadata.json；chart 取 $D/chart3/*.tgz
# 10-29 用它查出 up1064 快照落后 live 三处（secret 名 / 两个 callback / config）——回填见 snap-backfill-from-live.py。
set -uo pipefail
LANE=${1:?usage: $0 gray|prod|guarded-old [run_dir]}
R=${2:-$(cat /root/l3/.gray-run-latest)}
. $R/target.env
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
case "$LANE" in
  gray)        I=$R/gray-inputs; VAL=$R/gray/gray-values.yaml; REL=litellm-product-gray; PROF=k8s/values-gray.yaml; DEP=litellm-proxy-gray; SRCDEP=$I/gray-deployment.yaml; MODE=disabled; SRCC="clone:litellm-clone-c-sched-suppressed-20261002" ;;
  prod)        I=$R/prod-target; VAL=$I/prod-target-values.yaml; REL=litellm-product-proxy; PROF=k8s/values-prod.yaml; DEP=litellm-proxy; SRCDEP=$I/prod-deployment.yaml; MODE=primary; SRCC="direct:litellm-198-prod-reset-budget-owner" ;;
  guarded-old) I=$R/go-inputs; VAL=$R/go/go-values.yaml; REL=litellm-product-guarded-old; PROF=k8s/values-guarded-old.yaml; DEP=litellm-proxy-guarded-old; SRCDEP=$I/prod-deployment.yaml; MODE=disabled; SRCC="clone:litellm-clone-c-sched-suppressed-20261002" ;;
  *) echo "lane must be gray|prod|guarded-old" >&2; exit 64 ;;
esac
SNAP=$I/snap
W=$(mktemp -d /tmp/render-lane-$LANE.XXXX); chmod 700 $W
trap 'rm -rf $W' EXIT
DIGEST=$(kubectl -n litellm-product get deploy $DEP -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*@//')
cp $SRCDEP $W/deploy-src.yaml
# ingress-cidr / grace 与 up1064 冻结时一致；换集群网段时跟着 $D/gray 里的 prepare-values 命令改
COMMON=(--profile $PROF --repository 127.0.0.1:5000/litellm-carher --digest $DIGEST
  --config $SNAP/config/config.yaml --callbacks-dir $SNAP/callbacks --callbacks-volume callbacks
  --snapshot hooks=$SNAP/hooks --snapshot chatgpt-noauth=$SNAP/chatgpt-noauth --snapshot deepcopy-patch=$SNAP/deepcopy-patch
  --snapshot streaming-handler-patch=$SNAP/streaming-handler-patch
  --snapshot passthrough-streaming-handler-patch=$SNAP/passthrough-streaming-handler-patch
  --snapshot anthropic-logging-patch=$SNAP/anthropic-logging-patch
  --deployment $W/deploy-src.yaml --secret-metadata $R/go-inputs/secret-metadata.json
  --ingress-cidr 10.42.0.0/32 --ingress-cidr 10.42.0.1/32 --ingress-cidr 10.42.0.0/24 --ingress-cidr 10.42.1.0/24
  --ingress-cidr 10.42.2.0/24 --ingress-cidr 10.68.13.0/24 --ingress-cidr 10.42.3.0/24 --ingress-cidr 10.68.10.37/32 --termination-grace-seconds 600)
cd $SRC
python3 scripts/collect-scheduler-evidence.py --mode $MODE --source "$SRCC" --duplicate-scheduler-runs 0 --duplicate-background-jobs 0 \
  --unexpected-control-writes 0 --evidence-output $W/sched.json "${COMMON[@]}" >/dev/null 2>$W/err || { echo "RENDER FAIL (sched): $(tail -1 $W/err)"; exit 1; }
python3 scripts/prepare-values.py "${COMMON[@]}" --scheduler-evidence $W/sched.json --output $W/values.yaml >/dev/null 2>$W/err || { echo "RENDER FAIL (prepare-values): $(tail -1 $W/err)"; exit 1; }
REPL=$(python3 -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['replicas'])" $VAL)
python3 -c "import yaml,sys;p=sys.argv[1];v=yaml.safe_load(open(p));v['replicas']=int(sys.argv[2]);open(p,'w').write(yaml.safe_dump(v,sort_keys=False))" $W/values.yaml $REPL
helm template $REL $(ls $R/chart3/*.tgz | head -1) -n litellm-product --values $W/values.yaml > $W/target.yaml
python3 - $W/target.yaml $DEP <<'PY'
import json, subprocess, sys, yaml
tgt, dep = sys.argv[1:]
docs = [d for d in yaml.safe_load_all(open(tgt)) if d]
td = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == dep)
cms = {d["metadata"]["name"]: d.get("data", {}) for d in docs if d["kind"] == "ConfigMap"}
k = lambda *a: json.loads(subprocess.run(["kubectl", "-n", "litellm-product", "get", *a, "-o", "json"],
                                         capture_output=True, text=True, check=True).stdout)
ld = k("deploy", dep)


def view(d, cmget):
    s = d["spec"]["template"]["spec"]; c = s["containers"][0]
    v = {x["name"]: x.get("configMap", {}).get("name") for x in s["volumes"]}
    return {
        "image": c["image"].split("@")[-1],
        "envFrom": [x.get("secretRef", {}).get("name") for x in c.get("envFrom", [])],
        "env": sorted((e["name"], e.get("value", "")) for e in c.get("env", []) if "valueFrom" not in e),
        "mounts": sorted((m["mountPath"], m.get("subPath") or "") for m in c["volumeMounts"]),
        "callbacks": cmget(v["callbacks"]),
        "config": cmget(v["config"])["config.yaml"],
        "replicas": d["spec"]["replicas"],
    }


t = view(td, lambda n: cms[n])
l = view(ld, lambda n: k("cm", n)["data"])
bad = []
for key in ("image", "envFrom", "replicas"):
    if t[key] != l[key]:
        bad.append(f"{key}: target={t[key]} live={l[key]}")
te, le = dict(t["env"]), dict(l["env"])
d_env = {x: (te.get(x), le.get(x)) for x in set(te) | set(le) if te.get(x) != le.get(x)}
if d_env:
    bad.append(f"env differs (target, live): {d_env}")
tm, lm = set(t["mounts"]), set(l["mounts"])
known = {("/app/websearch_you_com.py", "websearch_you_com.py")}  # live 残留空目录（10-20 起），helm 不渲染它，无害
if (tm ^ lm) - known:
    bad.append(f"mounts target-only={sorted(tm - lm)} live-only={sorted((lm - tm) - known)}")
cf = [f for f in set(t["callbacks"]) | set(l["callbacks"]) if t["callbacks"].get(f) != l["callbacks"].get(f)]
if cf:
    bad.append(f"callback files differ: {sorted(cf)}")
if t["config"] != l["config"]:
    bad.append("config.yaml differs")
if bad:
    print(f"{dep}: DIFF: " + " | ".join(bad))
else:
    print(f"{dep}: RENDER == LIVE (image, envFrom={l['envFrom']}, {len(l['callbacks'])} callback files, "
          f"config.yaml, env, mounts, replicas={l['replicas']})")
sys.exit(1 if bad else 0)
PY
