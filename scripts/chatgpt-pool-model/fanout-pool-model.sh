#!/usr/bin/env bash
# fanout-pool-model.sh — 把一个 chatgpt-backend 模型全池铺开到 198 prod 的所有在跑 acct
#
# 前置（本脚本 *不* 做，见 README.md）：
#   1. CM chatgpt-pool-config 已含 `model_name: chatgpt-<SLUG> -> chatgpt/<SLUG>, mode: responses`
#   2. CM litellm-config router_settings.model_group_alias 已含 `<ALIAS> -> chatgpt-<SLUG>`
#      （198 prod 的 router_settings 在 CM，不在 DB；/config/update 写 DB 不生效）
#   3. quota-rebalance.py models_for() 已含该模型（否则 pause/resume 一轮后 entry 被摘）
#
# 本脚本做的事（对每个 replicas>=1 的 acct，*串行*）：
#   - rollout restart 该 acct deploy（读新 CM；acct deploy strategy=Recreate + 1GB/pod，
#     必须串行，并发会 2x 内存 OOM 节点）
#   - rollout status --timeout=$ROLLOUT_TIMEOUT，不 ready 就 skip register
#     （挡住给 auth 死号挂 entry → 否则 smoke 500/400，见 add-acct-198 Case D）
#   - /model/new 注册 router entry（id=chatgpt-acct-N-<SLUG>，api_base 每 acct，pool key）
#
# 在 198 kube host 上跑（jms ssh AIYJY-litellm）。传本脚本到 198 用 base64 走参数
#   （jms stdin 管道被隧道抖动会静默丢），且写 $HOME 不要写 /tmp（/tmp 常有 root 残留占位）。
#
# 用法：  SLUG=codex-auto-review ALIAS=codex-auto-review bash fanout-pool-model.sh
set -u

SLUG="${SLUG:-codex-auto-review}"                 # chatgpt 后端 slug（透传，别加后缀）
MODEL_NAME="chatgpt-${SLUG}"                       # acct pod / router 里的 model_name
NS="${NS:-litellm-product}"
EP="${EP:-https://cc.auto-link.com.cn/pro}"
MK="${MK:-sk-pro-litellm-ce077e2b0721bb419a633e4d}"        # prod master key
AK="${AK:-sk-chatgpt-198-d8a3f4e62b9c1057ef324918a7b6d3e0}" # acct pod pool key
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-150s}"

ACCTS=$(kubectl -n "$NS" get deploy -l pool=chatgpt-acct \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.replicas}{"\n"}{end}' \
  | awk '$2>=1{print $1}' | sed 's/chatgpt-acct-//' | sort -n)

total=$(echo "$ACCTS" | wc -w | tr -d ' ')
echo "=== START fanout model=$MODEL_NAME over $total running accts (ns=$NS) ==="
ok=0; regfail=0; rollfail=0; i=0
for N in $ACCTS; do
  i=$((i+1)); D=chatgpt-acct-$N
  echo "[$i/$total] $D: rollout restart"
  kubectl -n "$NS" rollout restart deploy/"$D" >/dev/null 2>&1
  if ! kubectl -n "$NS" rollout status deploy/"$D" --timeout="$ROLLOUT_TIMEOUT" >/dev/null 2>&1; then
    echo "  !! $D not ready in $ROLLOUT_TIMEOUT — skip register (auth 死号? 看 pod 日志 'refresh token failed 401')"
    rollfail=$((rollfail+1)); continue
  fi
  MID="chatgpt-acct-$N-${SLUG}"
  AB="http://$D.$NS.svc.cluster.local:4000"
  curl -s -o /dev/null -X POST "$EP/model/delete" -H "Authorization: Bearer $MK" \
    -H "Content-Type: application/json" -d "{\"id\":\"$MID\"}"
  code=$(curl -s -X POST "$EP/model/new" -H "Authorization: Bearer $MK" -H "Content-Type: application/json" \
    -d "{\"model_name\":\"$MODEL_NAME\",\"litellm_params\":{\"model\":\"openai/$MODEL_NAME\",\"api_base\":\"$AB\",\"api_key\":\"$AK\"},\"model_info\":{\"id\":\"$MID\",\"mode\":\"responses\"}}" \
    -w "%{http_code}" -o /dev/null)
  if [ "$code" = "200" ]; then ok=$((ok+1)); echo "  ok register $MID"; else regfail=$((regfail+1)); echo "  !! register $MID HTTP $code"; fi
done
echo "=== FANOUT DONE model=$MODEL_NAME: registered=$ok rollfail=$rollfail regfail=$regfail total=$total ==="
