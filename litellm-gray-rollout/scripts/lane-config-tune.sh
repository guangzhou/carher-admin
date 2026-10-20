#!/usr/bin/env bash
# lane-config-tune.sh — 在一条已冻结的 helm 车道上改 config.yaml 的几个标量设置、和/或下线几个 callback（198，root 跑）。
#
# 用法:
#   lane-config-tune.sh --lane prod|gray|guarded-old --run-dir <D> [--dry-run] \
#       [--set general_settings.KEY=JSONVALUE ...] [--drop-callback MODULE ...]
#
#   --set   只许 general_settings / litellm_settings / router_settings 下的**顶层标量**（数字/布尔/字符串/null），
#           值按 JSON 解析（3000、true、"x"、null；null = 删掉这个键）
#   --drop-callback  MODULE = 文件名去掉 .py；从 litellm_settings.callbacks 去掉对应条目，并从快照删掉该文件
#   --dry-run        只做到冻结 + 门禁，打印 diff，不 helm upgrade、不改快照
#   --adopt-live     live 有带外改动（live CM ≠ helm manifest）且已手动回填进快照时用：门禁的 diff 基线改成 live 对象，
#                    并要求「快照（改动前）冻结出的 config/callbacks」== live 逐字节，否则停
#
# 是 lane-callback-swap.sh 的姊妹（同一条「快照 → prepare-values 冻结 → helm template → 门禁 → helm upgrade」链，
# 同一组坑）。区别只在门禁：
#   1. config.yaml 语义 diff 必须**恰好**等于 --set 声明的那几项 + callbacks 列表里去掉的那几条；别的任何字段变了就停。
#   2. callbacks CM 必须恰好少掉 --drop-callback 的那几个 key、其余 key 逐字节不变。
#   3. Deployment 只许 config/callbacks 卷名换代 + 被下线文件的 volumeMount 消失 + checksum 注解变；其他对象不许变。
#   4. 改前快照整份备份到 $I/tune-<ts>/snap-before/；回滚 = `--restore <tune-dir>`（把快照放回再走同一条链）
#      或 `helm rollback <rel> <上一 revision>`。
# 不做：不碰 nginx / 路由 / replicas / 镜像 / env，不 kubectl apply。
set -euo pipefail

LANE="" RUN_DIR="" DRY=0 RESTORE="" ADOPT_LIVE=0
SETS=() DROPS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --lane) LANE="$2"; shift 2 ;;
    --run-dir) RUN_DIR="$2"; shift 2 ;;
    --set) SETS+=("$2"); shift 2 ;;
    --drop-callback) DROPS+=("$2"); shift 2 ;;
    --restore) RESTORE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --adopt-live) ADOPT_LIVE=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[[ -n "$LANE" && -d "$RUN_DIR" ]] || { sed -n '2,12p' "$0" >&2; exit 2; }
[[ -n "$RESTORE" || ${#SETS[@]} -gt 0 || ${#DROPS[@]} -gt 0 ]] || { echo "nothing to do" >&2; exit 2; }

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
NS=litellm-product
# shellcheck disable=SC1091
. "$RUN_DIR/target.env"
TOOLS="$SRC"
umask 077

case "$LANE" in
  prod)        I=$RUN_DIR/prod-target; VAL=$I/prod-target-values.yaml; REL=litellm-product-proxy;       PROF=k8s/values-prod.yaml;        DEP=litellm-proxy;             DEPLOY_SRC=$I/prod-live-deployment.yaml; MODE=primary ;;
  gray)        I=$RUN_DIR/gray-inputs;  VAL=$RUN_DIR/gray/gray-values.yaml; REL=litellm-product-gray;  PROF=k8s/values-gray.yaml;        DEP=litellm-proxy-gray;        DEPLOY_SRC=$I/prod-deployment.yaml;     MODE=disabled ;;
  guarded-old) I=$RUN_DIR/go-inputs;    VAL=$RUN_DIR/go/go-values.yaml;     REL=litellm-product-guarded-old; PROF=k8s/values-guarded-old.yaml; DEP=litellm-proxy-guarded-old; DEPLOY_SRC=$I/prod-deployment.yaml; MODE=disabled ;;
  *) echo "unknown lane $LANE" >&2; exit 2 ;;
esac
SNAP=$I/snap
[[ -d "$SNAP/callbacks" && -f "$SNAP/config/config.yaml" && -f "$VAL" && -f "$DEPLOY_SRC" ]] || { echo "lane inputs missing under $I" >&2; exit 2; }
CHART=$(ls "$RUN_DIR"/chart3/*.tgz | head -1)
DIGEST=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.template.spec.containers[0].image}'); DIGEST=${DIGEST#*@}
TS=$(date -u +%Y%m%dT%H%M%SZ); WORK=$I/tune-$TS; mkdir -p "$WORK"

# 0. live 必须仍是 helm 渲染出来的样子（同 lane-callback-swap 第 4 步：带外改动会被静默回退）
helm get manifest $REL -n $NS > "$WORK/helm-before.yaml"
cm_refs_live=$(kubectl -n $NS get deploy $DEP -o json | python3 -c "
import sys,json
s=json.load(sys.stdin)['spec']['template']['spec']
mounted={m['name'] for c in s.get('containers',[])+s.get('initContainers',[]) for m in c.get('volumeMounts',[])}
print(' '.join(sorted(v['name']+'='+v['configMap']['name'] for v in s['volumes'] if 'configMap' in v and v['name'] in mounted)))")
cm_refs_helm=$(python3 - "$WORK/helm-before.yaml" "$DEP" <<'PY'
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == sys.argv[2]:
        s = d["spec"]["template"]["spec"]
        print(" ".join(sorted(v["name"] + "=" + v["configMap"]["name"] for v in s["volumes"] if "configMap" in v)))
PY
)
if [[ "$cm_refs_live" != "$cm_refs_helm" ]]; then
  if [[ $ADOPT_LIVE == 1 ]]; then
    # 带外改动（如 litellm-198-lane-callback-add.py）已经手动回填进快照：门禁改成比「快照未改动时冻结出的 CM」与 live CM 逐字节相同，
    # 相同才继续；之后的 diff 基线也换成 live（而不是 helm manifest），这样带外的那几项不会被当成「未声明的改动」，也不会被冲掉。
    echo "adopt-live: live 与 helm 不一致，改为核对快照 == live（带外改动已回填）"
  else
    echo "STOP: live $DEP 与 helm release 引用的 ConfigMap 不一致（有带外改动）" >&2; echo "  live: $cm_refs_live" >&2; echo "  helm: $cm_refs_helm" >&2
    echo "  先把 live 的 callbacks/config/env 回填进 ${SNAP} 与 ${DEPLOY_SRC}，再加 --adopt-live 重跑" >&2; exit 1
  fi
fi

# 1. 备份快照 + values，然后改快照（或从备份恢复）
cp -a "$SNAP" "$WORK/snap-before"
cp -p "$VAL" "$WORK/values.before.yaml"
cp -p "$DEPLOY_SRC" "$WORK/deploy-src.before.yaml"
echo "backup: $WORK/snap-before  $WORK/values.before.yaml"
if [[ -n "$RESTORE" ]]; then
  [[ -d "$RESTORE/snap-before" ]] || { echo "no snap-before in $RESTORE" >&2; exit 2; }
  rm -rf "$SNAP/config" "$SNAP/callbacks"
  cp -a "$RESTORE/snap-before/config" "$RESTORE/snap-before/callbacks" "$SNAP/"
  [[ -f "$RESTORE/deploy-src.before.yaml" ]] && cp -p "$RESTORE/deploy-src.before.yaml" "$DEPLOY_SRC"
  [[ -f "$RESTORE/plan.json" ]] && cp "$RESTORE/plan.json" "$WORK/restore-plan.json"
  echo "restored config + callbacks (+ deployment source) from $RESTORE"
else
  python3 - "$SNAP/config/config.yaml" "$SNAP/callbacks" "$WORK/plan.json" "${#SETS[@]}" "${SETS[@]}" "${DROPS[@]}" <<'PY'
import json, sys, os, re
cfg_path, cb_dir, plan_path, nsets, *rest = sys.argv[1:]
nsets = int(nsets); sets, drops = rest[:nsets], rest[nsets:]
lines = open(cfg_path, encoding="utf-8").read().split("\n")
SECTIONS = ("general_settings", "litellm_settings", "router_settings")
def section_span(name):
    start = next((i for i, l in enumerate(lines) if l == f"{name}:"), None)
    if start is None: sys.exit(f"STOP: section {name} not found as a top-level line")
    end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith(" ")), len(lines))
    return start, end
plan = {"set": {}, "drop": []}
for s in sets:
    m = re.fullmatch(r"([a-z_]+)\.([A-Za-z0-9_]+)=(.*)", s)
    if not m or m.group(1) not in SECTIONS: sys.exit(f"STOP: bad --set {s!r}")
    sec, key, raw = m.groups()
    val = json.loads(raw)
    if isinstance(val, (dict, list)): sys.exit(f"STOP: --set only takes scalars: {s}")
    st, en = section_span(sec)
    idx = [i for i in range(st + 1, en) if re.match(rf"  {re.escape(key)}:(\s|$)", lines[i])]
    if len(idx) > 1: sys.exit(f"STOP: {sec}.{key} appears {len(idx)} times")
    if idx and not re.match(rf"  {re.escape(key)}:\s*\S", lines[idx[0]]):
        sys.exit(f"STOP: {sec}.{key} is not a one-line scalar in config.yaml")
    if val is None:
        if idx: del lines[idx[0]]
    else:
        new = f"  {key}: {json.dumps(val)}"
        if idx: lines[idx[0]] = new
        else: lines.insert(st + 1, new)
    plan["set"][f"{sec}.{key}"] = val
for mod in drops:
    if not re.fullmatch(r"[a-z0-9_]+", mod): sys.exit(f"STOP: bad module {mod}")
    st, en = section_span("litellm_settings")
    hit = [i for i in range(st + 1, en) if re.fullmatch(rf"  - {re.escape(mod)}\.[A-Za-z0-9_]+", lines[i])]
    if len(hit) != 1: sys.exit(f"STOP: callbacks entry for {mod} found {len(hit)} times")
    del lines[hit[0]]
    f = os.path.join(cb_dir, mod + ".py")
    if not os.path.isfile(f): sys.exit(f"STOP: {f} missing in snapshot")
    os.remove(f)
    plan["drop"].append(mod)
open(cfg_path, "w", encoding="utf-8").write("\n".join(lines))
json.dump(plan, open(plan_path, "w"))
print("plan:", json.dumps(plan))
PY
fi

# 2. 冻结
COMMON=(--profile "$PROF" --repository 127.0.0.1:5000/litellm-carher --digest "$DIGEST"
  --config "$SNAP/config/config.yaml" --callbacks-dir "$SNAP/callbacks" --callbacks-volume callbacks
  --snapshot hooks="$SNAP/hooks" --snapshot chatgpt-noauth="$SNAP/chatgpt-noauth" --snapshot deepcopy-patch="$SNAP/deepcopy-patch"
  --snapshot streaming-handler-patch="$SNAP/streaming-handler-patch"
  --snapshot passthrough-streaming-handler-patch="$SNAP/passthrough-streaming-handler-patch"
  --snapshot anthropic-logging-patch="$SNAP/anthropic-logging-patch"
  --deployment "$WORK/deploy-src.yaml" --secret-metadata "$RUN_DIR/go-inputs/secret-metadata.json"
  --ingress-cidr 10.42.0.0/32 --ingress-cidr 10.42.0.1/32 --ingress-cidr 10.42.0.0/24 --ingress-cidr 10.42.1.0/24
  --ingress-cidr 10.42.2.0/24 --ingress-cidr 10.68.13.0/24 --ingress-cidr 10.42.3.0/24 --ingress-cidr 10.68.10.37/32 --termination-grace-seconds 600)
# 下线的 callback 在 --deployment 源里还挂着 ⇒ 冻结前从源 Deployment 去掉这些 mount（只去这几条，其余不动）
python3 - "$DEPLOY_SRC" "$WORK/deploy-src.yaml" "$SNAP/callbacks" <<'PY'
import sys, yaml, os
src, out, cbdir = sys.argv[1:]
d = yaml.safe_load(open(src))
have = set(os.listdir(cbdir))
removed = []
for c in d["spec"]["template"]["spec"]["containers"]:
    keep = []
    for m in c.get("volumeMounts", []):
        if m["name"] == "callbacks" and m.get("subPath") and m["subPath"] not in have:
            removed.append(m["subPath"]); continue
        keep.append(m)
    c["volumeMounts"] = keep
yaml.safe_dump(d, open(out, "w"), sort_keys=False)
print("  deployment source: dropped mounts", removed)
PY
REPLICAS=$(python3 -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['replicas'])" "$VAL")
EV=$WORK/scheduler-evidence.json
SOURCE="clone:litellm-clone-c-sched-suppressed-20261002"; [[ $MODE == primary ]] && SOURCE="direct:litellm-198-prod-reset-budget-owner"
( cd "$TOOLS" && python3 scripts/collect-scheduler-evidence.py --mode $MODE --source "$SOURCE" \
    --duplicate-scheduler-runs 0 --duplicate-background-jobs 0 --unexpected-control-writes 0 --evidence-output "$EV" "${COMMON[@]}" >/dev/null )
( cd "$TOOLS" && python3 scripts/prepare-values.py "${COMMON[@]}" --scheduler-evidence "$EV" --output "$WORK/values.yaml" ) | tail -1 | cut -c1-200
python3 - "$WORK/values.yaml" "$REPLICAS" "$MODE" <<'PY'
import yaml, sys
p, rep, mode = sys.argv[1:]
v = yaml.safe_load(open(p)); v["replicas"] = int(rep)
assert v["schedulerSafety"]["mode"] == mode, ("scheduler role drift", v["schedulerSafety"]["mode"])
open(p, "w").write(yaml.safe_dump(v, sort_keys=False))
PY

# 3. 门禁：只许声明过的变化
helm get manifest $REL -n $NS > "$WORK/live.yaml"
helm template $REL "$CHART" -n $NS --values "$WORK/values.yaml" > "$WORK/target.yaml"
if [[ $ADOPT_LIVE == 1 ]]; then
  # 基线换成 live：把 helm manifest 里本 Deployment 和它引用的 config/callbacks CM 替换成集群里的真实对象（只保留 chart 管的字段）
  python3 - "$WORK/live.yaml" "$DEP" <<'PY'
import json, subprocess, sys, yaml
path, dep = sys.argv[1:]
k = lambda *a: json.loads(subprocess.run(["kubectl", "-n", "litellm-product", *a, "-o", "json"], capture_output=True, text=True, check=True).stdout)
docs = [d for d in yaml.safe_load_all(open(path)) if d]
live_dep = k("get", "deploy", dep)
vol = {v["name"]: v["configMap"]["name"] for v in live_dep["spec"]["template"]["spec"]["volumes"] if "configMap" in v}
out = []
for d in docs:
    if d["kind"] == "Deployment" and d["metadata"]["name"] == dep:
        # 只把带外改动会碰的三处换成 live；live 是 apiserver 规整过的形状（补了 defaultMode、value:null…），
        # 归一化成 chart 的写法再比，否则全是假差异
        lc = live_dep["spec"]["template"]["spec"]["containers"][0]
        cb_keys = set(k("get", "cm", vol["callbacks"])["data"])
        # live 上有「subPath 指向 CM 里不存在的 key」的残留挂载（10-20 实测 gray/prod 都有 websearch_you_com.py，
        # 回滚 you_com 时 CM 去了 key、mount 没去 ⇒ pod 里是个空目录）。helm 渲染时不会再带它，属于无害清理，逐条打印
        dangling = [m for m in lc["volumeMounts"] if m["name"] == "callbacks" and m.get("subPath") not in cb_keys]
        for m in dangling:
            print("  adopt-live: ignore dangling live mount (subPath not in callbacks CM):", m["mountPath"])
        d["spec"]["template"]["spec"]["volumes"] = live_dep["spec"]["template"]["spec"]["volumes"]
        d["spec"]["template"]["spec"]["containers"][0]["env"] = lc.get("env", [])
        d["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] = [m for m in lc["volumeMounts"] if m not in dangling]
    if d["kind"] == "ConfigMap" and ("-config-" in d["metadata"]["name"] or "-callbacks-" in d["metadata"]["name"]):
        role = "config" if "-config-" in d["metadata"]["name"] else "callbacks"
        cm = k("get", "cm", vol[role])
        d = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": cm["metadata"]["name"], "namespace": "litellm-product",
             "labels": d["metadata"].get("labels", {})}, "data": cm["data"]}
    out.append(d)
yaml.safe_dump_all(out, open(path, "w"), sort_keys=False)
print("  adopt-live: baseline = live", vol["config"], vol["callbacks"])
PY
fi
python3 - "$WORK/live.yaml" "$WORK/target.yaml" "$WORK/approval.json" "$DEP" "$WORK/plan.json" "$WORK/restore-plan.json" <<'PY'
import yaml, sys, json, os
live, tgt, appr, dep, plan_path, restore_plan_path = sys.argv[1:]
plan = json.load(open(plan_path)) if os.path.exists(plan_path) else None          # 正向：逐项核声明
rplan = json.load(open(restore_plan_path)) if os.path.exists(restore_plan_path) else None  # 回滚：允许恢复被下线的 mount
toggled = set((plan or {}).get("drop", [])) | set((rplan or {}).get("drop", []))
a = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(open(live)) if d}
b = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(open(tgt)) if d}
gone, added = sorted(set(a) - set(b)), sorted(set(b) - set(a))
assert all(k == "ConfigMap" for k, _ in gone + added), ("non-CM object added/removed", gone, added)
def pick(xs, word): return [x for x in xs if f"-{word}-" in x[1]]
for word in ("config", "callbacks"):
    assert len(pick(gone, word)) == len(pick(added, word)) <= 1, (word, gone, added)
assert len(gone) == len(added) == len(pick(gone, "config")) + len(pick(gone, "callbacks")) >= 1, ("expected only config/callbacks CM rotation", gone, added)
report = {}
cfg_old = pick(gone, "config"); cfg_new = pick(added, "config")
if cfg_old:
    o = yaml.safe_load(a[cfg_old[0]]["data"]["config.yaml"]); n = yaml.safe_load(b[cfg_new[0]]["data"]["config.yaml"])
    diffs = []
    for top in sorted(set(o) | set(n)):
        if top not in ("general_settings", "litellm_settings", "router_settings"):
            assert o.get(top) == n.get(top), ("config top-level changed", top)
            continue
        os_, ns_ = o.get(top) or {}, n.get(top) or {}
        for k in sorted(set(os_) | set(ns_)):
            if os_.get(k) != ns_.get(k):
                diffs.append((f"{top}.{k}", os_.get(k), ns_.get(k)))
    report["config"] = [(k, (str(x)[:80] if not isinstance(x, list) else f"list[{len(x)}]"), (str(y)[:80] if not isinstance(y, list) else f"list[{len(y)}]")) for k, x, y in diffs]
    if plan is not None:
        want = {k: v for k, v in plan["set"].items()}
        got = {k: y for k, x, y in diffs if k != "litellm_settings.callbacks"}
        assert set(got) == set(want), ("config keys changed != declared", sorted(got), sorted(want))
        for k, v in want.items(): assert got[k] == v, (k, got[k], v)
        cbo = (o.get("litellm_settings") or {}).get("callbacks") or []; cbn = (n.get("litellm_settings") or {}).get("callbacks") or []
        removed = [x for x in cbo if x not in cbn]
        assert [x for x in cbn if x not in cbo] == [], "callbacks added?"
        assert sorted(r.split(".")[0] for r in removed) == sorted(plan["drop"]), ("callbacks removed != declared", removed)
        assert [x for x in cbo if x in cbn] == cbn, "callbacks order changed"
cb_old = pick(gone, "callbacks"); cb_new = pick(added, "callbacks")
if cb_old:
    o, n = a[cb_old[0]]["data"], b[cb_new[0]]["data"]
    removed = sorted(set(o) - set(n)); new = sorted(set(n) - set(o))
    changed = sorted(k for k in set(o) & set(n) if o[k] != n[k])
    report["callbacks"] = {"removed": removed, "added": new, "changed": changed}
    if plan is not None:
        assert not new and not changed, ("callback content changed", new, changed)
        assert removed == sorted(m + ".py" for m in plan["drop"]), ("callback keys removed != declared", removed)
elif plan is not None:
    assert not plan["drop"], "drop declared but callbacks CM did not rotate"
for k in set(a) & set(b):
    if k[0] == "Deployment":
        x, y = json.loads(json.dumps(a[k]["spec"])), json.loads(json.dumps(b[k]["spec"]))
        for s in (x, y):
            ann = s["template"]["metadata"].get("annotations") or {}
            # 只有 checksum 注解允许变；其余注解必须一致
            s["template"]["metadata"]["annotations"] = {kk: vv for kk, vv in ann.items() if not kk.endswith("-checksum")}
            s["template"]["spec"].pop("volumes", None)
            for c in s["template"]["spec"]["containers"]:
                c["volumeMounts"] = sorted(
                    ({kk: vv for kk, vv in m.items()} for m in c.get("volumeMounts", [])
                     if not (m["name"] == "callbacks" and m.get("subPath", "")[:-3] in toggled)),
                    key=lambda m: (m["name"], m["mountPath"]))
                # env：按名字比（顺序不算），apiserver 把 value:"" 存成缺省 ⇒ 归一成 ""
                c["env"] = sorted(({**e, "value": e.get("value") or ""} if "valueFrom" not in e else e for e in c.get("env", [])),
                                  key=lambda e: e["name"])
        assert x == y, "Deployment spec changed beyond config/callbacks volumes and dropped mounts"
    elif k[0] != "ConfigMap":
        assert a[k] == b[k], ("non-CM object changed", k)
print("  gate PASS:", json.dumps(report, ensure_ascii=False))
json.dump({"deletions": [{"object": f"/ConfigMap/litellm-product/{g[1]}", "consumer": dep,
           "disposition": "config/callbacks CM 换代（lane-config-tune）", "approver": "lane-config-tune"} for g in gone]},
          open(appr, "w"), ensure_ascii=False)
PY
( cd "$TOOLS" && python3 scripts/check-release-deletion-set.py --live-manifest "$WORK/live.yaml" --target-manifest "$WORK/target.yaml" \
    --release $REL --namespace $NS --run-id lane-config-tune --generation "$TS" \
    --approval "$WORK/approval.json" --output "$WORK/deletion-set.json" >/dev/null 2>&1 ) || true
python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print('  deletion-set',d['status'],d.get('errors'));sys.exit(0 if d['status']=='PASS' else 1)" "$WORK/deletion-set.json"

if [[ $DRY == 1 ]]; then
  rm -rf "$SNAP/config" "$SNAP/callbacks"; cp -a "$WORK/snap-before/config" "$WORK/snap-before/callbacks" "$SNAP/"
  cp -p "$WORK/deploy-src.before.yaml" "$DEPLOY_SRC"
  echo "DRY-RUN: gates passed, snapshot restored, nothing deployed ($WORK)"; exit 0
fi

# 4. 上线 + 逐 pod 验收。--deployment 源同步成去掉了下线 mount 的那份（否则下次 lane-callback-swap 冻结会因
#    「挂载了快照里没有的文件」失败）；原件留在 $WORK/deploy-src.before.yaml 供 --restore 用
cp "$WORK/deploy-src.yaml" "$DEPLOY_SRC"
cp "$WORK/values.yaml" "$VAL"
helm upgrade $REL "$CHART" -n $NS --values "$VAL" --reset-values 2>&1 | grep -E 'REVISION|STATUS'
kubectl -n $NS rollout status deploy/$DEP --timeout=1500s | tail -1
want_pods=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.replicas}')
for _ in $(seq 1 90); do
  [[ $(kubectl -n $NS get po -l app=$DEP --no-headers | wc -l) == "$want_pods" ]] && break; sleep 10
done
CFG_SHA=$(python3 -c "import yaml,sys,hashlib;v=yaml.safe_load(open(sys.argv[1]));print(hashlib.sha256(v['config']['data']['config.yaml'].encode()).hexdigest()[:12])" "$VAL")
bad=0
for p in $(kubectl -n $NS get po -l app=$DEP --field-selector=status.phase=Running -o json | python3 -c "
import json,sys;print(' '.join(i['metadata']['name'] for i in json.load(sys.stdin)['items'] if not i['metadata'].get('deletionTimestamp')))"); do
  err=$(kubectl -n $NS logs "$p" -c litellm 2>/dev/null | grep -cE 'ImportError|ModuleNotFoundError|SyntaxError' || true)
  s=$(kubectl -n $NS exec "$p" -c litellm -- sha256sum /app/config.yaml | cut -c1-12)
  line="  $p import_err=$err config=$([[ $s == "$CFG_SHA" ]] && echo ok || { bad=1; echo "BAD($s)"; })"
  for m in "${DROPS[@]}"; do
    # 容器里 /app/.venv/bin/test 是个 python 入口，会盖掉 coreutils test ⇒ 必须走 sh -c 用 shell 内建
    st=$(kubectl -n $NS exec "$p" -c litellm -- sh -c "[ -e /app/$m.py ] && echo present || echo gone" 2>/dev/null)
    case "$st" in gone) line+=" $m=gone" ;; present) line+=" $m=STILL_MOUNTED"; bad=1 ;; *) line+=" $m=UNKNOWN($st)"; bad=1 ;; esac
  done
  [[ $err == 0 ]] || bad=1
  echo "$line"
done
[[ $bad == 0 ]] && echo "VERIFIED $LANE tune $TS (rollback: --restore $WORK)" || { echo "VERIFY FAILED on $LANE (rollback: --restore $WORK)" >&2; exit 1; }
