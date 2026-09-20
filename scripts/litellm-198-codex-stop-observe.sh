#!/usr/bin/env bash
# 198 prod litellm-proxy: codex goal-模式停机分支长期观察(T3 长稳)。
#
# 每个观察窗内统计:
#   - codex 停机分支真实触发数(family/over-budget CODEX 429)+ 去重 alias
#   - budget_notice 异常(pre_call error / traceback)——有则 ALERT
#   - pod 存活数(deployment 主 pod,排除 canary)
#
# 用法: ./scripts/litellm-198-codex-stop-observe.sh [WINDOW]
#   WINDOW  kubectl logs --since 值,默认 4h30m(略大于 4h cron 间隔,防漏窗)
#
# 本地跑,SSH 进 198(cltx@10.68.13.198),pod 操作需 sudo kubectl。
set -uo pipefail

WINDOW="${1:-4h30m}"
HOST="${LITELLM_198_HOST:-cltx@10.68.13.198}"

ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$HOST" "WINDOW='$WINDOW' bash -s" <<'REMOTE' 2>&1 | grep -v -E '^\[sudo\]|sitecustomize'
set -uo pipefail
NS=litellm-product
PODS=$(sudo kubectl -n "$NS" get pods -o name 2>/dev/null \
  | grep -E 'litellm-proxy-' | grep -v canary | cut -d/ -f2)
[ -z "$PODS" ] && { echo "ALERT: no litellm-proxy pods found"; exit 2; }

RUNNING=$(sudo kubectl -n "$NS" get pods 2>/dev/null \
  | grep -E 'litellm-proxy-' | grep -v canary | grep -c Running)
NPODS=$(echo "$PODS" | wc -l | tr -d ' ')

TMP=$(mktemp)
for P in $PODS; do
  sudo kubectl -n "$NS" logs "$P" --since="$WINDOW" 2>/dev/null >> "$TMP"
done

FIRE=$(grep -cE 'budget_notice:.*CODEX 429' "$TMP")
FAM=$(grep -cE 'budget_notice: family block CODEX 429' "$TMP")
OVER=$(grep -cE 'budget_notice: over-budget CODEX 429' "$TMP")
ALIASES=$(grep -oE 'budget_notice:.*CODEX 429 alias=[^ ]+' "$TMP" \
  | sed -E 's/.*alias=([^ ]+).*/\1/' | sort -u | paste -sd, -)
# 异常:budget_notice 自身 pre_call error,或紧跟 budget_notice 的 traceback
ERRS=$(grep -cE 'budget_notice: pre_call error|budget_notice:.*(Traceback|error [^r])' "$TMP")
ERRSAMPLE=$(grep -E 'budget_notice: pre_call error' "$TMP" | tail -3)

echo "=== codex-stop observe (window=$WINDOW) ==="
echo "pods: $RUNNING/$NPODS Running"
echo "codex-stop firings: $FIRE (family=$FAM over-budget=$OVER)"
echo "distinct aliases: ${ALIASES:-none}"
echo "budget_notice errors: $ERRS"
[ -n "$ERRSAMPLE" ] && { echo "-- error samples --"; echo "$ERRSAMPLE"; }

VERDICT=OK
[ "$RUNNING" != "$NPODS" ] && VERDICT=ALERT
[ "$ERRS" -gt 0 ] && VERDICT=ALERT
echo "VERDICT: $VERDICT"
rm -f "$TMP"
REMOTE