#!/bin/bash
# 本目录 = 一次「版本升级灰度」的操作层（198，root 跑）。run 目录由 /root/l3/.gray-run-latest 指向（或 GRAY_RUN_POINTER）。
# 用法: clone-qualify.sh <template-run-dir> <tag>
#   例: clone-qualify.sh /root/l3/up1064-20261025T164519Z 20261101
# SOP §2「clone 先过」整条链，按顺序、逐个等完成、任何一步红就停：
#   ① 生产元数据 dump（只读，MVCC 快照，排除两张日志表数据）→ ② restore clone A → ③ 新镜像在 A 上 migrate
#   → ④ A post-migration dump → ⑤ restore B → ⑥ restore C → ⑦ new(A) / old(B) 两腿 → ⑧ concurrent-new + concurrent-old（C）同时跑
#   最后打印四腿 status（全 PASS 才算过）。新旧镜像 = $D/target.env 的 TARGET_DIGEST / PREV_DIGEST。
# 模板 = 上一轮 run 的 $TPL/clone/{1-dump,2-restore,3-migrate-a,4-postmig,5-version}.yaml（这几份是实跑通过的形状）。
#
# 10-26 v1.106.4 踩过、已焊死在这里的坑：
#   - 镜像字段写全引用，GRAY_IMAGE_DIGEST 只写裸 digest（runner 校验 ^sha256:[0-9a-f]{64}$，写全引用 ⇒ 「execution binding checksum is invalid」）
#   - ③ post-mig dump 与 ⑤⑥ restore 必须串行：同批 apply ⇒ B/C 读不到 litellm-migrated.dump.sha256 直接失败
#   - dump 的「非日志表 ≤1 GiB」闸门已过时（10-26 实测 1.1 GB，DailyTagSpend 381 MB）⇒ 用 DUMP_LIMIT_BYTES（默认 4 GiB）
#   - GRAY_RUNNER_SHA256 必须 = 集群里 runner CM 的 sha（带 BARRIER_ACK_KEYS 的那份），不一致直接停
#   - kubectl exec 一律 </dev/null（本脚本也可能被 ssh 'bash -s' 喂进来，见 memory kubectl_exec_i_eats_heredoc_stdin）
set -uo pipefail
TPL=${1:?模板 run 目录}; TAG=${2:?tag，如 20261101}
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; NS=litellm-clone
D=$(cat ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}); . $D/target.env; umask 077
: "${TARGET_DIGEST:?}" "${PREV_DIGEST:?target.env 缺 PREV_DIGEST}"
OUT=$D/clone; mkdir -p $OUT
RUNNER_CM=${RUNNER_CM:-litellm-gray-migration-runners-80c881172f57}
RUNNER_SHA=$(kubectl -n $NS get cm $RUNNER_CM -o jsonpath='{.data.compatibility-runner\.py}' | sha256sum | cut -d' ' -f1)
[ "$RUNNER_SHA" = "$(sha256sum < $SRC/scripts/compatibility-runner.py | cut -d' ' -f1)" ] || { echo "STOP: runner CM $RUNNER_CM ≠ 仓库 compatibility-runner.py"; exit 1; }
python3 - "$TPL/clone" "$OUT" "$TAG" "$TARGET_DIGEST" "$PREV_DIGEST" "$RUNNER_SHA" "$RUNNER_CM" "${DUMP_LIMIT_BYTES:-4294967296}" <<'PY'
import yaml,sys,re,os
src,out,tag,new,old,rsha,rcm,limit=sys.argv[1:]
REPO="127.0.0.1:5000/litellm-carher@"
for f in ("1-dump","2-restore","3-migrate-a","4-postmig","5-version"):
    docs=[d for d in yaml.safe_load_all(open(f"{src}/{f}.yaml")) if d]
    for d in docs:
        d["metadata"]["name"]=re.sub(r"-\d{8}$","",d["metadata"]["name"])+"-"+tag
        d["spec"].pop("suspend",None); d["spec"]["ttlSecondsAfterFinished"]=86400
        c=d["spec"]["template"]["spec"]["containers"][0]; n=d["metadata"]["name"]
        if "litellm-carher@" in c["image"]:
            is_old = "old" in n
            c["image"]=REPO+(old if is_old else new)
        for e in c.get("env",[]):
            if e["name"]=="GRAY_IMAGE_DIGEST": e["value"]=old if "old" in n else new
            if e["name"]=="GRAY_RUNNER_SHA256": e["value"]="sha256:"+rsha
            if e["name"]=="GRAY_RUN_ID": e["value"]="litellm-198-clone-"+tag
            if e["name"]=="GRAY_GENERATION": e["value"]="g"+tag
        if c.get("args"):
            c["args"]=[re.sub(r"/dump/prev-\d{8}b?","/dump/prev-"+tag,re.sub(r"-le \d+ \|\|","-le "+limit+" ||",a)) for a in c["args"]]
        for v in d["spec"]["template"]["spec"].get("volumes",[]):
            if v.get("configMap",{}).get("name","").startswith("litellm-gray-migration-runners"): v["configMap"]["name"]=rcm
    for d in docs:
        yaml.safe_dump(d,open(f"{out}/{d['metadata']['name']}.yaml","w"),sort_keys=False)
print("rendered", sorted(os.listdir(out)))
PY
run1(){ local j=$1-$TAG; kubectl -n $NS delete job $j --ignore-not-found --wait=true >/dev/null; kubectl apply -f $OUT/$j.yaml >/dev/null
  kubectl -n $NS wait --for=condition=complete --timeout=${2:-900s} job/$j >/dev/null 2>&1; local s=$?; echo "$j rc=$s"
  [ $s = 0 ] || { kubectl -n $NS logs job/$j --tail=15 2>&1 | grep -v -E 'prisma:warn|Please report'; exit 1; }; }
mig(){ kubectl -n $NS exec litellm-clone-$1-0 -- psql -U litellm -d litellm -At -c 'select count(*) from _prisma_migrations where finished_at is not null' </dev/null; }
run1 litellm-prod-metadata-dump
run1 litellm-clone-a-restore
echo "clone-a migrations before=$(mig a)"
run1 litellm-clone-schema-migration
echo "clone-a migrations after=$(mig a)"
run1 litellm-clone-a-post-migration-dump
run1 litellm-clone-b-restore
run1 litellm-clone-c-restore
run1 litellm-new-version-test
run1 litellm-old-version-test
for j in litellm-concurrent-new-test litellm-concurrent-old-test; do kubectl -n $NS delete job $j-$TAG --ignore-not-found --wait=true >/dev/null; kubectl apply -f $OUT/$j-$TAG.yaml >/dev/null; done
for j in litellm-concurrent-new-test litellm-concurrent-old-test; do kubectl -n $NS wait --for=condition=complete --timeout=900s job/$j-$TAG >/dev/null 2>&1; echo "$j-$TAG rc=$?"; done
for j in new-version old-version concurrent-new concurrent-old; do
  kubectl -n $NS logs job/litellm-$j-test-$TAG 2>/dev/null | grep '"binding"' | tail -1 | python3 -c "import json,sys;d=json.loads(sys.stdin.read() or '{}');print('$j',d.get('status'),d.get('mode'),d.get('binding',{}).get('image_digest','')[:19])"
done
