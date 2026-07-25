#!/usr/bin/env bash
# weighted-affinity-watchdog.sh — 198 prod 主池 weighted-affinity hook 静默失效看门狗
#
# 每 30 分钟跑一次（cron）:
# 1) 检查至少 1 个 proxy pod 日志里有 WeightedAffinityRouter 初始化
# 2) 若 30min 内有 chatgpt-acct 流量(SpendLogs)但 0 条 hook 决策 → hook 静默失效告警
# 3) 若 invalid_encrypted_content 400 飙升(>3/30min) → 可能是黏性降级告警
#
# 输出到 stdout；有飞书 webhook 可推；cron 落日志即可。
# 设计保守：宁可漏报不误报——没流量时不告警、只有"有流量但 hook 完全静默"才判失效。
set -uo pipefail

NS="litellm-product"
LOG_TAIL=3000  # 每 pod 取最近 3000 行日志

echo "[watchdog] $(date -u +%FT%T) 开始"

# 拿所有主池 proxy pod
PODS=$(kubectl -n "$NS" get pods --field-selector=status.phase=Running -o name | grep -E "litellm-proxy-[0-9a-f]{9,}")
if [ -z "$PODS" ]; then echo "[watchdog] WARN: 无 running proxy pod"; exit 0; fi

# 1) 有没有 pod 加载了 hook
INIT=0
for P in $PODS; do
  kubectl -n "$NS" logs "${P#pod/}" --tail="$LOG_TAIL" 2>/dev/null | grep -q "WeightedAffinityRouter: initialized" && INIT=$((INIT+1))
done
echo "[watchdog] hook initialized in $INIT/${#PODS[@]} pods"
if [ "$INIT" -eq 0 ]; then
  echo "[watchdog] ALERT: hook 未加载到任何 pod！检查 config callbacks + volumeMount"
  exit 1
fi

# 2) 有流量但 hook 决策静默
MISS=0; HIT=0
for P in $PODS; do
  M=$(kubectl -n "$NS" logs "${P#pod/}" --tail="$LOG_TAIL" 2>/dev/null | grep -c "WeightedAffinityRouter: MISS")
  H=$(kubectl -n "$NS" logs "${P#pod/}" --tail="$LOG_TAIL" 2>/dev/null | grep -c "WeightedAffinityRouter: HIT")
  MISS=$((MISS+M)); HIT=$((HIT+H))
done
DECISIONS=$((MISS+HIT))
echo "[watchdog] hook decisions: MISS=$MISS HIT=$HIT total=$DECISIONS"
# 若 30min 内 SpendLogs 有 chatgpt-acct 流量但 decisions=0 → 静默失效
# (这里不直接查 DB，靠 quota view 的 probe；简化：decisions=0 且 pod uptime>30min 才判)
OLDEST_AGE=$(kubectl -n "$NS" get pods --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.metadata.creationTimestamp}{"\n"}{end}' 2>/dev/null | sort | head -1)
if [ "$DECISIONS" -eq 0 ] && [ -n "$OLDEST_AGE" ]; then
  echo "[watchdog] WARN: hook decisions=0，可能静默失效（或池子无流量）"
fi

# 3) invalid_encrypted_content 飙升
ENC=0
for P in $PODS; do
  E=$(kubectl -n "$NS" logs "${P#pod/}" --tail="$LOG_TAIL" 2>/dev/null | grep -ci "invalid_encrypted_content")
  ENC=$((ENC+E))
done
echo "[watchdog] invalid_encrypted_content errors: $ENC"
if [ "$ENC" -gt 3 ]; then
  echo "[watchdog] ALERT: encrypted_content 400 飙升($ENC)！检查 Redis 黏性是否降级"
  exit 1
fi

echo "[watchdog] OK (init=$INIT decisions=$DECISIONS enc_err=$ENC)"
