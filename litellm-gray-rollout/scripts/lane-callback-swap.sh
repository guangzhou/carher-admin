#!/usr/bin/env bash
# lane-callback-swap.sh — 在一条已冻结的 helm 车道上只换 callback 文件（198，在 198 上以 root 跑）。
#
# 用法:
#   lane-callback-swap.sh --lane prod|gray|guarded-old --run-dir <D> --files-dir <dir> FILE [FILE...]
#
#   <D>    本轮 run 目录（含 target.env、chart3/*.tgz、prod-target/ gray-inputs/ go-inputs/）
#   <dir>  放着要上线的那几个 callback 文件的目录（逐字节 = git 里那份）
#
# 为什么有这个脚本（2026-10-03，v1.103.2 收敛后一晚上换了 5 次 callback）
# ------------------------------------------------------------------------
# 车道是 helm 冻结出来的：callback 不是直接改 CM，而是「快照目录 → prepare-values 冻结 →
# helm template → 删除集门禁 → helm upgrade」。手搓这串命令踩过三个坑，脚本把它们钉死：
#   1. prepare-values 的 --deployment 必须是**快照键入时**那份 Deployment（卷名按它对齐）。
#      拿升级后的 live 去冻结会报 `mounted but has no --snapshot`，被 set -e 外层吞掉，
#      旧 values 原样沿用 ⇒ helm upgrade 是空操作，rollout 绿，什么都没换（rev10 实测）。
#      ⇒ 本脚本冻结后**断言 values 里该文件的 sha 已变成目标 sha**，否则拒绝 upgrade。
#   2. 门禁只许「callbacks CM 换代 + 且只有点名的这几个 key 变」——别的任何对象/键变了就停。
#   3. 验收看每个在服务 pod 里 /app/<file> 的 sha 和 ImportError，不看 rollout 状态。
#
# 车道角色（scheduler）不在这里改：gray / guarded-old 永远 disabled，prod 永远 primary，
# 由 prepare-values 按 profile 决定；本脚本只把 prepare-values 的报告打出来供核对。
#
# 不做的事：不碰 nginx、不碰路由标签、不改 replicas、不 apply。回滚 = 把备份的旧文件放回
# --files-dir 再跑一遍（或 helm rollback 到上一 revision，旧 CM 由 helm 管理）。
set -euo pipefail

LANE="" RUN_DIR="" FILES_DIR=""
FILES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lane) LANE="$2"; shift 2 ;;
    --run-dir) RUN_DIR="$2"; shift 2 ;;
    --files-dir) FILES_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) FILES+=("$1"); shift ;;
  esac
done
[[ -n "$LANE" && -d "$RUN_DIR" && -d "$FILES_DIR" && ${#FILES[@]} -gt 0 ]] || { sed -n '2,12p' "$0" >&2; exit 2; }

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
NS=litellm-product
# shellcheck disable=SC1091
. "$RUN_DIR/target.env"   # 提供 $SRC = 198 上本轮的 litellm-gray-rollout 源码树
TOOLS="$SRC"
umask 077

case "$LANE" in
  prod)        I=$RUN_DIR/prod-target; VAL=$I/prod-target-values.yaml; REL=litellm-product-proxy;       PROF=k8s/values-prod.yaml;        DEP=litellm-proxy;             DEPLOY_SRC=$I/prod-live-deployment.yaml; MODE=primary ;;
  gray)        I=$RUN_DIR/gray-inputs;  VAL=$RUN_DIR/gray/gray-values.yaml; REL=litellm-product-gray;  PROF=k8s/values-gray.yaml;        DEP=litellm-proxy-gray;        DEPLOY_SRC=$I/prod-deployment.yaml;     MODE=disabled ;;
  guarded-old) I=$RUN_DIR/go-inputs;    VAL=$RUN_DIR/go/go-values.yaml;     REL=litellm-product-guarded-old; PROF=k8s/values-guarded-old.yaml; DEP=litellm-proxy-guarded-old; DEPLOY_SRC=$I/prod-deployment.yaml; MODE=disabled ;;
  *) echo "unknown lane $LANE" >&2; exit 2 ;;
esac
SNAP=$I/snap
[[ -d "$SNAP/callbacks" && -f "$VAL" && -f "$DEPLOY_SRC" ]] || { echo "lane inputs missing under $I" >&2; exit 2; }
CHART=$(ls "$RUN_DIR"/chart3/*.tgz | head -1)
DIGEST=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.template.spec.containers[0].image}'); DIGEST=${DIGEST#*@}
TS=$(date -u +%Y%m%dT%H%M%SZ); WORK=$I/swap-$TS; mkdir -p "$WORK"

# 4. live 必须仍是 helm 渲染出来的样子。2026-10-10 起 litellm-198-lane-callback-add.py 会在 helm
#    之外直接 patch Deployment（加新 callback）；那之后再从陈旧快照 helm upgrade，会把带外加的
#    callback 和手工改过的 CM 一起静默回退，且下面的门禁比的是 helm manifest ↔ target，看不出来。
#    ⇒ 动快照之前先比 live Deployment 与 helm manifest 引用的 ConfigMap 名，不一致就停。
helm get manifest $REL -n $NS > "$WORK/helm-before.yaml"
#    只比**真挂载**的卷（2026-10-20）：live 上「只声明未挂载」的旧卷（gray 1 个、prod 4 个
#    litellm-stable-*，历次升级残留）不影响运行，helm upgrade 把它们去掉也不会回退任何东西
#    （version-upgrade SKILL §5：mounted=False 的旧卷可以删）。忽略的逐条打印，不静默。
cm_refs_live=$(kubectl -n $NS get deploy $DEP -o json | python3 -c "
import sys,json
s=json.load(sys.stdin)['spec']['template']['spec']
mounted={m['name'] for c in s.get('containers',[])+s.get('initContainers',[]) for m in c.get('volumeMounts',[])}
for v in s['volumes']:
    if 'configMap' in v and v['name'] not in mounted:
        print('  ignore unmounted live volume', v['name'], '->', v['configMap']['name'], file=sys.stderr)
print(' '.join(sorted(v['name']+'='+v['configMap']['name'] for v in s['volumes'] if 'configMap' in v and v['name'] in mounted)))")
cm_refs_helm=$(python3 - "$WORK/helm-before.yaml" "$DEP" <<'PY'
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == sys.argv[2]:
        s = d["spec"]["template"]["spec"]
        print(" ".join(sorted(v["name"] + "=" + v["configMap"]["name"] for v in s["volumes"] if "configMap" in v)))
PY
)
[[ "$cm_refs_live" == "$cm_refs_helm" ]] || {
  echo "STOP: live $DEP 引用的 ConfigMap 与 helm release 不一致（有带外改动，如 litellm-198-lane-callback-add.py）。" >&2
  echo "  live: $cm_refs_live" >&2; echo "  helm: $cm_refs_helm" >&2
  echo "  先把 live 的 callbacks/config 回填进 $SNAP 与 $DEPLOY_SRC 再跑，否则 helm upgrade 会静默回退。" >&2
  exit 1; }

# 5. 已知坏版本指纹（与 scripts/litellm-198-callbacks-drift.py 的 KNOWN_BAD 同一份清单），动快照之前就拒。
#    2026-10-25：spendlog 清洗旧版（_LITERAL_HIGH_LONE_RE）⇒ SpendLogs 丢行，git 87515f0 修复；
#    旧版在历代快照 / .work 拷贝里到处都有，拿错一份发上去 = bug 回来且所有门禁全绿。
for f in "${FILES[@]}"; do
  case "$f" in
    null_byte_sanitize.py|sitecustomize.py)
      if grep -q '_LITERAL_HIGH_LONE_RE' "$FILES_DIR/$f" 2>/dev/null; then
        echo "STOP: $FILES_DIR/$f 是 spendlog 清洗旧版（87515f0 之前，会让 SpendLogs 丢行）" >&2; exit 1
      fi ;;
  esac
done

declare -A WANT
for f in "${FILES[@]}"; do
  [[ -f "$FILES_DIR/$f" ]] || { echo "missing $FILES_DIR/$f" >&2; exit 2; }
  [[ -f "$SNAP/callbacks/$f" ]] || { echo "$f is not an installed callback on $LANE (use litellm-callback-install.py for new ones)" >&2; exit 2; }
  cp -p "$SNAP/callbacks/$f" "$WORK/$f.before"
  cp "$FILES_DIR/$f" "$SNAP/callbacks/$f"
  WANT[$f]=$(sha256sum < "$SNAP/callbacks/$f" | cut -c1-12)
done
echo "backups: $WORK/*.before"

cp -p "$VAL" "$WORK/values.before.yaml"
COMMON=(--profile "$PROF" --repository 127.0.0.1:5000/litellm-carher --digest "$DIGEST"
  --config "$SNAP/config/config.yaml" --callbacks-dir "$SNAP/callbacks" --callbacks-volume callbacks
  --snapshot hooks="$SNAP/hooks" --snapshot chatgpt-noauth="$SNAP/chatgpt-noauth" --snapshot deepcopy-patch="$SNAP/deepcopy-patch"
  --snapshot streaming-handler-patch="$SNAP/streaming-handler-patch"
  --snapshot passthrough-streaming-handler-patch="$SNAP/passthrough-streaming-handler-patch"
  --snapshot anthropic-logging-patch="$SNAP/anthropic-logging-patch"
  --deployment "$DEPLOY_SRC" --secret-metadata "$RUN_DIR/go-inputs/secret-metadata.json"
  --ingress-cidr 10.42.0.0/32 --ingress-cidr 10.42.0.1/32 --ingress-cidr 10.42.0.0/24 --ingress-cidr 10.42.1.0/24
  --ingress-cidr 10.42.2.0/24 --ingress-cidr 10.68.13.0/24 --ingress-cidr 10.42.3.0/24 --ingress-cidr 10.68.10.37/32 --termination-grace-seconds 600)
REPLICAS=$(python3 -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['replicas'])" "$VAL")
EV=$WORK/scheduler-evidence.json
SOURCE="clone:litellm-clone-c-sched-suppressed-20261002"; [[ $MODE == primary ]] && SOURCE="direct:litellm-198-prod-reset-budget-owner"
( cd "$TOOLS" && python3 scripts/collect-scheduler-evidence.py --mode $MODE --source "$SOURCE" \
    --duplicate-scheduler-runs 0 --duplicate-background-jobs 0 --unexpected-control-writes 0 --evidence-output "$EV" "${COMMON[@]}" >/dev/null )
( cd "$TOOLS" && python3 scripts/prepare-values.py "${COMMON[@]}" --scheduler-evidence "$EV" --output "$WORK/values.yaml" ) | tail -1 | cut -c1-200
python3 - "$WORK/values.yaml" "$REPLICAS" "$MODE" "${FILES[@]}" <<'PY'
import yaml, sys, hashlib
p, rep, mode, *files = sys.argv[1:]
v = yaml.safe_load(open(p)); v["replicas"] = int(rep)
assert v["schedulerSafety"]["mode"] == mode, ("scheduler role drift", v["schedulerSafety"]["mode"])
for f in files:
    print(f"  frozen {f} = {hashlib.sha256(v['callbacks']['data'][f].encode()).hexdigest()[:12]}")
open(p, "w").write(yaml.safe_dump(v, sort_keys=False))
PY
for f in "${FILES[@]}"; do
  got=$(python3 -c "import yaml,sys,hashlib;v=yaml.safe_load(open(sys.argv[1]));print(hashlib.sha256(v['callbacks']['data'][sys.argv[2]].encode()).hexdigest()[:12])" "$WORK/values.yaml" "$f")
  [[ "$got" == "${WANT[$f]}" ]] || { echo "STOP: frozen $f=$got != wanted ${WANT[$f]} (freeze silently kept the old file)" >&2; exit 1; }
done

helm get manifest $REL -n $NS > "$WORK/live.yaml"
helm template $REL "$CHART" -n $NS --values "$WORK/values.yaml" > "$WORK/target.yaml"
python3 - "$WORK/live.yaml" "$WORK/target.yaml" "$WORK/approval.json" "$DEP" "${FILES[@]}" <<'PY'
import yaml, sys, json
live, tgt, appr, dep, *files = sys.argv[1:]
a = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(open(live)) if d}
b = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(open(tgt)) if d}
gone, added = sorted(set(a) - set(b)), sorted(set(b) - set(a))
assert len(gone) == 1 and len(added) == 1 and "callbacks" in gone[0][1] and "callbacks" in added[0][1], (gone, added)
o, n = a[gone[0]]["data"], b[added[0]]["data"]
changed = sorted(k for k in set(o) | set(n) if o.get(k) != n.get(k))
assert changed == sorted(files), ("unexpected callback keys changed", changed)
for k in set(a) & set(b):
    if k[0] == "Deployment":
        x, y = dict(a[k]["spec"]), dict(b[k]["spec"])
        for s in (x, y):
            s["template"] = dict(s["template"]); s["template"]["metadata"] = dict(s["template"]["metadata"])
            s["template"]["metadata"].pop("annotations", None)
            s["template"]["spec"] = {kk: vv for kk, vv in s["template"]["spec"].items() if kk != "volumes"}
        assert x == y, "Deployment spec changed beyond callbacks volume"
    elif k[0] != "ConfigMap":
        assert a[k] == b[k], ("non-CM object changed", k)
print("  gate: only callbacks CM rotated; keys changed:", changed)
json.dump({"deletions": [{"object": f"/{gone[0][0]}/litellm-product/{gone[0][1]}", "consumer": dep,
           "disposition": "callbacks CM 换代：只换 " + ", ".join(files), "approver": "lane-callback-swap"}]},
          open(appr, "w"), ensure_ascii=False)
PY
( cd "$TOOLS" && python3 scripts/check-release-deletion-set.py --live-manifest "$WORK/live.yaml" --target-manifest "$WORK/target.yaml" \
    --release $REL --namespace $NS --run-id lane-callback-swap --generation "$TS" \
    --approval "$WORK/approval.json" --output "$WORK/deletion-set.json" >/dev/null 2>&1 ) || true
python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print('  deletion-set',d['status'],d.get('errors'));sys.exit(0 if d['status']=='PASS' else 1)" "$WORK/deletion-set.json"

cp "$WORK/values.yaml" "$VAL"
helm upgrade $REL "$CHART" -n $NS --values "$VAL" --reset-values 2>&1 | grep -E 'REVISION|STATUS'
kubectl -n $NS rollout status deploy/$DEP --timeout=1500s | tail -1
want_pods=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.replicas}')
for _ in $(seq 1 90); do
  [[ $(kubectl -n $NS get po -l app=$DEP --no-headers | wc -l) == "$want_pods" ]] && break; sleep 10
done
bad=0
for p in $(kubectl -n $NS get po -l app=$DEP --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}'); do
  err=$(kubectl -n $NS logs "$p" -c litellm 2>/dev/null | grep -cE 'ImportError|ModuleNotFoundError|SyntaxError' || true)
  line="  $p import_err=$err"
  for f in "${FILES[@]}"; do
    s=$(kubectl -n $NS exec "$p" -c litellm -- sha256sum /app/$f | cut -c1-12)
    line+=" $f=$([[ $s == "${WANT[$f]}" ]] && echo ok || { bad=1; echo "BAD($s)"; })"
  done
  [[ $err == 0 ]] || bad=1
  echo "$line"
done
[[ $bad == 0 ]] && echo "VERIFIED $LANE: ${FILES[*]}" || { echo "VERIFY FAILED on $LANE" >&2; exit 1; }
