#!/usr/bin/env bash
# 「OWUI 所有用户只能用 X(,Y...)」一条命令。默认 dry-run,--apply 才写。
#
#   P198=... owui-set-allowed-models.sh deepseek-v4.1-flash            # 只看计划
#   P198=... owui-set-allowed-models.sh deepseek-v4.1-flash --apply    # 真改
#
# 顺序(每一步不过就停,不进下一步):
#   0. 前置: 每个名字在 LiteLLM 每个副本都注册了且真发 200   ⇐ 名字在 LiteLLM 里不存在就先去建组
#   1. 备份  反代 CM + 两个 Deployment + webui.db 整库(sqlite backup API)
#   2. 反代  CM ALLOWED_MODELS=<名单> → rollout restart(2 副本滚动,零中断)
#   3. OWUI  admin API: 关 direct connections / 默认模型 / 停用指向旧模型的 workspace 模型
#            (不重启 OWUI;但 env ENABLE_DIRECT_CONNECTIONS 仍会是旧值,下次重启时 config 表优先)
#   4. 验收  owui-model-gate-audit.sh 三层
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/_198.sh"

MODELS=""; APPLY=0
for a in "$@"; do
  case $a in
    --apply) APPLY=1 ;;
    -*) sed -n '2,14p' "$0"; exit 2 ;;
    *) MODELS=$a ;;
  esac
done
[ -n "$MODELS" ] || { sed -n '2,14p' "$0"; exit 2; }
JSON_MODELS=$(python3 -c 'import json,sys; print(json.dumps([i.strip() for i in sys.argv[1].split(",") if i.strip()]))' "$MODELS")

echo "== 0. LiteLLM 逐副本前置检查"
IPS=$(r198 "kubectl get pod -n litellm-product -l app=litellm-proxy -o jsonpath='{range .items[*]}{.status.podIP} {end}'")
OUT=$(pypod open-webui key-swap-proxy "$HERE/pod_litellm_precheck.py" \
      "$(python3 -c 'import json,sys; print(json.dumps({"models":json.loads(sys.argv[1]),"ips":sys.argv[2].split()}))' "$JSON_MODELS" "$IPS")")
echo "$OUT"
echo "$OUT" | grep -q '^PRECHECK PASS' || { echo "前置不过,停。名字不存在就先建组(skill owui-key-swap-proxy §6.0)"; exit 3; }

CUR=$(r198 "kubectl get cm key-swap-proxy-config -n open-webui -o jsonpath='{.data.ALLOWED_MODELS}'")
echo "== 反代当前 ALLOWED_MODELS=${CUR:-<未设,走代码 default>}  →  $MODELS"
echo "== OWUI 侧计划"
pypod open-webui open-webui "$HERE/pod_owui_apply.py" "{\"allowed\":$JSON_MODELS,\"apply\":false}"

if [ $APPLY -ne 1 ]; then echo "(dry-run; 加 --apply 执行)"; exit 0; fi

TS=$(date +%Y%m%dT%H%M%S)
BK=/Data/owui-ops/backup-$TS-allowed-models
echo "== 1. 备份 → 198:$BK"
r198 "set -e; mkdir -p $BK
kubectl get cm key-swap-proxy-config -n open-webui -o yaml > $BK/key-swap-proxy-config.yaml
kubectl get deploy key-swap-proxy -n open-webui -o yaml > $BK/key-swap-proxy-deploy.yaml
kubectl get deploy open-webui -n open-webui -o yaml > $BK/open-webui-deploy.yaml
kubectl exec -n open-webui deploy/open-webui -- python3 -c \"import sqlite3; s=sqlite3.connect('/app/backend/data/webui.db'); d=sqlite3.connect('/app/backend/data/webui.db.bak-$TS'); s.backup(d); d.close(); print('webui.db -> webui.db.bak-$TS', sqlite3.connect('/app/backend/data/webui.db.bak-$TS').execute('select count(*) from user').fetchone())\"
ls -la $BK"

if [ "$CUR" != "$MODELS" ]; then
  echo "== 2. 反代白名单"
  PATCH=$(python3 -c 'import json,sys; print(json.dumps({"data":{"ALLOWED_MODELS":sys.argv[1]}}))' "$MODELS")
  r198 "set -e
kubectl patch configmap key-swap-proxy-config -n open-webui --type=merge -p '$PATCH'
kubectl rollout restart deployment/key-swap-proxy -n open-webui
kubectl rollout status deployment/key-swap-proxy -n open-webui --timeout=180s"
else
  echo "== 2. 反代白名单已是目标值,跳过"
fi

echo "== 3. OWUI 侧"
pypod open-webui open-webui "$HERE/pod_owui_apply.py" "{\"allowed\":$JSON_MODELS,\"apply\":true}"

echo "== 4. 验收"
"$HERE/owui-model-gate-audit.sh"
echo "回滚: CM 用 $BK/key-swap-proxy-config.yaml 里的 ALLOWED_MODELS 再 patch;库用 PVC 上 webui.db.bak-$TS"
