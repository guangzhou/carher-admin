#!/usr/bin/env bash
# chatgpt-acct-quota-aliyun.sh — 阿里云 carher ns chatgpt-acct 池状态完整列表
#
# 数据源:
#   - kubectl -n carher get pod -l pool=chatgpt-acct       (pod readiness/restarts/age)
#   - kubectl -n carher exec <pod> -- cat /chatgpt-auth/auth.json  (email + expires_at)
#   - kubectl -n carher exec litellm-db-0 -- psql (LiteLLM_SpendLogs 5h/24h)
#
# 跟 198 版 chatgpt-acct-quota.sh 的区别:
#   - 没有 quota-rebalance state.json (5h%/7d%/tier/paused/restore 不可得)
#   - 阿里云 SG IP 直调 chatgpt.com /codex/usage 被 CF 403, 不做上游 usage 探针
#   - 阿里云只跑 gpt-5.5 一档 (无 5.4 / 5.3-codex 池)
#
# 默认输出完整表格; --summary 追加 ready 分组; --json 原样吐 JSON
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JMS="$SCRIPT_DIR/jms"
VIEW="$SCRIPT_DIR/chatgpt_acct_quota_aliyun_view.py"
[[ -x "$JMS" ]] || JMS="jms"

TUNNEL_LOCAL_PORT="${TUNNEL_LOCAL_PORT:-16443}"
TUNNEL_REMOTE_HOST="${TUNNEL_REMOTE_HOST:-172.16.1.163}"
TUNNEL_REMOTE_PORT="${TUNNEL_REMOTE_PORT:-6443}"
TUNNEL_HOP="${TUNNEL_HOP:-laoyang}"

ensure_tunnel() {
  if nc -z -G 2 127.0.0.1 "$TUNNEL_LOCAL_PORT" 2>/dev/null; then
    return 0
  fi
  echo "# aliyun k8s tunnel down; starting jms proxy $TUNNEL_HOP $TUNNEL_LOCAL_PORT $TUNNEL_REMOTE_HOST $TUNNEL_REMOTE_PORT" >&2
  nohup "$JMS" proxy "$TUNNEL_HOP" "$TUNNEL_LOCAL_PORT" "$TUNNEL_REMOTE_HOST" "$TUNNEL_REMOTE_PORT" \
    > /tmp/jms-proxy.log 2>&1 &
  for _ in $(seq 1 15); do
    sleep 1
    nc -z -G 2 127.0.0.1 "$TUNNEL_LOCAL_PORT" 2>/dev/null && return 0
  done
  echo "# tunnel still not reachable on 127.0.0.1:$TUNNEL_LOCAL_PORT after 15s; see /tmp/jms-proxy.log" >&2
  return 1
}

case "${1:-}" in
  -h|--help)
    sed -n '2,16p' "$0"
    exit 0
    ;;
esac

ensure_tunnel
exec python3 "$VIEW" "$@"
