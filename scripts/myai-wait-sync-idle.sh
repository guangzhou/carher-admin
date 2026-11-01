#!/usr/bin/env bash
# 等到 243 myai「可以安全换 pod」再返回（发布前用）。
#
# 危险窗口 = 同步末尾的发布阶段：它在长事务里合并整天事件，拿着锁；这时换 pod，新 pod 的
# apply_usage_schema 等锁、被 liveness 杀（10-29 chat 金丝雀重启 1 次）。
# 同步是首尾相接跑的（上一轮结束同一秒下一轮开始），永远等不到「完全空闲」，所以判据是：
#   只看最新一条同步：不在跑 ⇒ 安全；在跑但刚开始不到 SAFE_SECONDS（还在拉上游、没进发布）⇒ 安全。
# 旧的 'running' 行（被重启的 worker 遗留，永远不会结束）不看：只要它之后还有更新的一轮，就当孤儿跳过。
# 🔴 11-01：deploy 换掉 worker 留下 16088，旧判据被它卡到超时；新判据下孤儿行不再是「最新一条」。
#
# 用法: HOST243_PW=... scripts/myai-wait-sync-idle.sh [最长等待秒数，默认 2400]
#   SAFE_SECONDS 默认 480（10-29 实测一轮 24 分钟，发布在最后约 1 分钟）
set -euo pipefail
MAX="${1:-2400}"
SAFE="${SAFE_SECONDS:-480}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export MYAI_SSH_PASSWORD="${HOST243_PW:-${MYAI_SSH_PASSWORD:-}}"
[ -n "$MYAI_SSH_PASSWORD" ] || { echo "需要 HOST243_PW" >&2; exit 2; }
probe() {
  # 输出 "<status> <已运行秒数>"；JSON 按字段取，不靠表格空白匹配
  MYAI_HUMAN= "$HERE/myai-cli.sh" db sql \
    "SELECT CASE WHEN r.status='running' AND r.start_date=r.end_date AND r.end_date < (now() AT TIME ZONE 'Asia/Shanghai')::date THEN 'repair' ELSE r.status END AS status, extract(epoch from now()-r.started_at)::int AS age FROM usage_sync_runs r WHERE NOT (r.status='running' AND EXISTS (SELECT 1 FROM usage_sync_runs n WHERE n.started_at > r.started_at)) ORDER BY r.started_at DESC LIMIT 1" 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["status"], d[0]["age"]) if d else print("none 0")'
}
end=$(( $(date +%s) + MAX ))
while :; do
  if out=$(probe); then
    st=${out% *}; age=${out#* }
    if [ "$st" != running ] && [ "$st" != repair ]; then echo "safe: 最新一轮 ${st} $(date -u +%T)"; exit 0; fi
    # 收尾修复轮（单日、日期早于今天）：换 pod 会把它杀掉并白白耗掉一次 repairAttempts（上限 2），必须等它结束。
    if [ "$st" = repair ]; then echo "等待：收尾修复轮在跑（${age}s），不能打断 $(date -u +%T)" >&2
    elif [ "$age" -lt "$SAFE" ]; then echo "safe: 新一轮刚开始 ${age}s（<${SAFE}s，还没进发布） $(date -u +%T)"; exit 0; fi
  else
    echo "读不到同步状态，重试" >&2
  fi
  [ "$(date +%s)" -ge "$end" ] && { echo "等了 ${MAX}s 仍没等到安全窗口，放弃" >&2; exit 1; }
  sleep 15
done
