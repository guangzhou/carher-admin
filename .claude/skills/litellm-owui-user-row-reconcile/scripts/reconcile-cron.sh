#!/usr/bin/env bash
# 每 10 分钟对一次账：飞书那边新建的号，补进 LiteLLM 的人行。
# 稳态下（没有孤儿 key）只跑一条 psql 查询就退出，不碰任何东西、不重启任何东西。
# 只在真补了人的那一轮才会滚动重启 key-swap-proxy 清 600 秒拒绝缓存。
set -uo pipefail

DIR=/Data/litellm-user-backfill
LOG=/var/log/litellm-user-reconcile.log
export PATH=/usr/local/bin:/usr/bin:/bin
export RUN_ID="$(date +%Y%m%dT%H%M%S)"

ts() { date '+%F %T%z'; }

# 日志自己收口，别让它长成 198 上的下一个盘满事故。
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 20971520 ]; then
  tail -c 5242880 "$LOG" > "$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
fi

{
  echo "=== $(ts) run_id=$RUN_ID ==="
  timeout 900 python3 "$DIR/backfill.py" --apply
  rc=$?
  echo "--- $(ts) exit=$rc ---"
  # exit=2 是脚本自己抛的 Fail（飞书查不通、阳性对照坏了、md5 不一致等）。
  # 这类情况它在写库之前就停了，不会留半个事务 —— 事务本身是全或全无的。
  if [ "$rc" -ne 0 ]; then
    echo "!! 这轮没干净收尾，rc=$rc —— 下一轮会重试（所有 insert 都是 on conflict do nothing，重复跑安全）"
  fi
} >> "$LOG" 2>&1
