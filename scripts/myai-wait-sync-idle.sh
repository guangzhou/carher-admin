#!/usr/bin/env bash
# 等 243 myai 没有在跑的同步再返回（发布前用）。在跑的同步拿着长事务锁，这时换 pod 会让新 pod 的
# apply_usage_schema 等锁、被 liveness 杀掉（10-29 chat 金丝雀重启 1 次）。
# 用法: HOST243_PW=... scripts/myai-wait-sync-idle.sh [最长等待秒数，默认 1800]
set -euo pipefail
MAX="${1:-1800}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export MYAI_SSH_PASSWORD="${HOST243_PW:-${MYAI_SSH_PASSWORD:-}}"
[ -n "$MYAI_SSH_PASSWORD" ] || { echo "需要 HOST243_PW" >&2; exit 2; }
count() {
  # JSON 输出，按字段取数，不靠匹配表格空白（旧写法 grep '^ *1 *$' 对不上 CLI 的列对齐，等于没等）
  MYAI_HUMAN= "$HERE/myai-cli.sh" db sql \
    "SELECT count(*) AS n FROM usage_sync_runs WHERE status='running' AND started_at > now()-interval '40 minutes'" 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(int(d[0]["n"]))'
}
end=$(( $(date +%s) + MAX ))
while :; do
  n=$(count || echo -1)
  if [ "$n" = 0 ]; then echo "idle $(date -u +%T)"; exit 0; fi
  [ "$n" = -1 ] && echo "读不到同步状态，重试" >&2
  [ "$(date +%s)" -ge "$end" ] && { echo "等了 ${MAX}s 仍有同步在跑（n=${n}），放弃" >&2; exit 1; }
  sleep 20
done
