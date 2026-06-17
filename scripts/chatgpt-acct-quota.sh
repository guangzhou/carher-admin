#!/usr/bin/env bash
# chatgpt-acct-quota.sh — 198 prod chatgpt-acct 池 5h/7d 配额完整列表
#
# 数据源：JSZX-AI-03:/home/cltx/.chatgpt-quota/state/state.json
# 默认输出完整列表；--summary 追加 grouped counts；--json 透传 state.json。
# 这是 ChatGPT 上游配额查看的唯一默认入口；不要在 skill/对话里临时拼
# jms/kubectl/python heredoc 重做表格，直接运行本脚本并原样输出。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JMS="$SCRIPT_DIR/jms"
VIEW="$SCRIPT_DIR/chatgpt_acct_quota_view.py"
[[ -x "$JMS" ]] || JMS="jms"

JSON=0
SUMMARY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --summary) SUMMARY=1; shift ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ $JSON -eq 1 ]]; then
  exec "$JMS" ssh JSZX-AI-03 "cat /home/cltx/.chatgpt-quota/state/state.json"
fi

if [[ $SUMMARY -eq 1 ]]; then
  "$JMS" ssh JSZX-AI-03 "python3 - --summary" < "$VIEW"
else
  "$JMS" ssh JSZX-AI-03 "python3 -" < "$VIEW"
fi
