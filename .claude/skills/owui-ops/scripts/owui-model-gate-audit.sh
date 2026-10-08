#!/usr/bin/env bash
# 验收「OWUI 用户只能用某几个模型」—— 只读,不改任何东西。
#
#   P198=... owui-model-gate-audit.sh [--blocked a,b,c]
#
# 白名单直接读反代 CM 的 ALLOWED_MODELS(不信手填),然后三层取证:
#   L1 OWUI /api/models(真实普通用户 ×3 + admin)+ 白名单模型真发一条
#   L2 反代 /v1/models + 非白名单模型发送必须 403
#   L3 direct connections 关了 + 没有指向非白名单的 active workspace 模型
# 最后一行 RESULT PASS / FAIL xN。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/_198.sh"

BLOCKED="sa-grok-4.6,gpt-5.6-sol,claude-opus-5"
while [ $# -gt 0 ]; do
  case $1 in
    --blocked) BLOCKED=$2; shift 2 ;;
    *) sed -n '2,11p' "$0"; exit 2 ;;
  esac
done

ALLOWED=$(r198 "kubectl get cm key-swap-proxy-config -n open-webui -o jsonpath='{.data.ALLOWED_MODELS}'")
[ -n "$ALLOWED" ] || { echo "读不到 ALLOWED_MODELS(CM 没设 ⇒ 反代走代码 default)"; exit 3; }
echo "ALLOWED_MODELS=$ALLOWED"

ARGS=$(python3 -c 'import json,sys; s=lambda x:[i.strip() for i in x.split(",") if i.strip()]; print(json.dumps({"allowed":s(sys.argv[1]),"blocked":s(sys.argv[2])}))' "$ALLOWED" "$BLOCKED")
pypod open-webui open-webui "$HERE/pod_gate_audit.py" "$ARGS"
