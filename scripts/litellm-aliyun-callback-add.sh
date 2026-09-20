#!/bin/bash
# litellm-aliyun-callback-add.sh — 阿里云 carher ns 外科式新增一个 litellm callback
#
# 用法(在 226 上跑;文件先 jms scp 上去):
#   bash litellm-aliyun-callback-add.sh /tmp/<name>.py <module>.<instance> [ENV=VAL ...]
#   例: bash litellm-aliyun-callback-add.sh /tmp/budget_notice.py budget_notice.budget_notice \
#         BUDGET_NOTICE_KEY_ALIASES=carher-t0-canary BUDGET_FRIENDLY_MOCK_DISABLED=1
#
# 三处变更全部外科式(2026-08-21 budget_notice 上线验证过):
#   1. CM litellm-callbacks: merge-patch 只加目标 key(不 rebuild,不碰其余 key —
#      旧法 create cm --dry-run|apply 全量重建曾吃掉别人补丁)
#   2. CM litellm-config: config.yaml callbacks 列表只加一行(diff 校验恰 1 行)
#   3. deploy litellm-proxy: strategic patch 加 volumeMount + env(单次 patch 单次滚动;
#      不加 volumeMount 直接 CrashLoop ImportError)
#
# 已知坑(都在本脚本兜住):
#   - `diff|grep -c` 在 set -o pipefail 下 diff rc=1 会杀脚本 → 包 (|| true)
#   - hostPort + maxSurge=0 滚动死锁:老 pod Terminating 卡住占 hostPort、新 pod
#     Pending(2026-08-03 首见,08-21 复现)。脚本只检测+打印处置,不自动删:
#     确认老 pod 0/1 且新 RS ≥1 Ready 后 force-delete 老 pod(见 litellm-hook-dev skill)
set -euo pipefail
NS=carher
SRC=${1:?usage: $0 <file.py> <module.instance> [ENV=VAL ...]}
INSTANCE=${2:?need <module.instance>}
shift 2
KEY=$(basename "$SRC")
test -s "$SRC"
echo "adding callback: key=$KEY instance=$INSTANCE envs=$*"
md5sum "$SRC"

echo "=== 1. CM litellm-callbacks merge-patch(单 key) ==="
python3 - "$SRC" "$KEY" <<'PY'
import json, sys
src, key = sys.argv[1], sys.argv[2]
open('/tmp/cb-add-patch.json','w').write(json.dumps({'data':{key: open(src).read()}}))
PY
kubectl -n $NS patch cm litellm-callbacks --type=merge --patch-file /tmp/cb-add-patch.json
kubectl -n $NS get cm litellm-callbacks -o go-template="{{index .data \"$KEY\"}}" | md5sum

echo "=== 2. CM litellm-config callbacks 列表 +1 行 ==="
kubectl -n $NS get cm litellm-config -o go-template='{{index .data "config.yaml"}}' > /tmp/cb-config-before.yaml
if grep -q "  - $INSTANCE\$" /tmp/cb-config-before.yaml; then
  echo "already in callbacks list, skip"
else
  python3 - "$INSTANCE" <<'PY'
import re, json, sys
inst = sys.argv[1]
text = open('/tmp/cb-config-before.yaml').read()
m = re.search(r'(  callbacks:\n(?:  - .+\n)+)', text)
assert m, 'callbacks block not found'
out = text.replace(m.group(1), m.group(1) + f'  - {inst}\n', 1)
open('/tmp/cb-config-after.yaml','w').write(out)
open('/tmp/cb-config-patch.json','w').write(json.dumps({'data':{'config.yaml': out}}))
PY
  D=$( (diff /tmp/cb-config-before.yaml /tmp/cb-config-after.yaml || true) | grep -c '^[<>]' )
  test "$D" = "1" || { echo "FATAL: config diff=$D lines, expected 1"; exit 1; }
  kubectl -n $NS patch cm litellm-config --type=merge --patch-file /tmp/cb-config-patch.json
fi

echo "=== 3. deploy patch: volumeMount + env ==="
ENVJSON=""
for kv in "$@"; do
  n=${kv%%=*}; v=${kv#*=}
  ENVJSON="$ENVJSON,{\"name\":\"$n\",\"value\":\"$v\"}"
done
ENVJSON=${ENVJSON#,}
PATCH="{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"litellm\",\"volumeMounts\":[{\"name\":\"callbacks\",\"mountPath\":\"/app/$KEY\",\"subPath\":\"$KEY\",\"readOnly\":true}]"
if [ -n "$ENVJSON" ]; then PATCH="$PATCH,\"env\":[$ENVJSON]"; fi
PATCH="$PATCH}]}}}}"
kubectl -n $NS patch deploy litellm-proxy --type=strategic -p "$PATCH"

echo "=== 4. rollout(检测 hostPort 死锁) ==="
if ! kubectl -n $NS rollout status deploy litellm-proxy --timeout=360s; then
  echo "---- rollout 超时,检查 hostPort 死锁形态 ----"
  kubectl -n $NS get pods -l app=litellm-proxy -o wide
  echo "若:老 pod 0/1 Terminating 卡住 + 新 pod Pending + 新 RS ≥1 Ready →"
  echo "  force-delete 老 pod 解锁(kubectl delete pod <old> --force --grace-period=0)"
  echo "  注:carher ns 有零中断 hook 拦截,需按提示加 override 注释,先人工核对三条件"
  exit 1
fi
kubectl -n $NS get pods -l app=litellm-proxy --no-headers
C=$(kubectl -n $NS get pods -l app=litellm-proxy --no-headers | grep -cE 'CrashLoop|Error' || true)
test "$C" = "0" || { echo "FATAL: CrashLoop —— 大概率 volumeMount 没生效/文件名不符"; exit 1; }
echo "=== DONE. 记得同步 repo k8s/litellm-proxy.yaml 四处(callbacks列表/CM data/volumeMount/env) ==="
rm -f /tmp/cb-add-patch.json /tmp/cb-config-*.yaml /tmp/cb-config-patch.json
