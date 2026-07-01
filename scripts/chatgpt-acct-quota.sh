#!/usr/bin/env bash
# chatgpt-acct-quota.sh — 198 prod chatgpt-acct 池 5h/7d 配额完整列表
#
# 数据源：JSZX-AI-03:/home/cltx/.chatgpt-quota/state/state.json
# 默认输出完整列表；--summary 追加 grouped counts；--json 透传 state.json。
# 输出带 `=== BEGIN ... rows=N ===` / `=== END ... rows=N ===` frame
# 和 5h 流量 / 不健康汇总尾段，本身就是完整答复——直接原样贴回，不要追加 markdown。
# 副本落 /tmp/chatgpt-acct-quota-last.txt，供"贴失败"时 cat 回放。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JMS="$SCRIPT_DIR/jms"
VIEW="$SCRIPT_DIR/chatgpt_acct_quota_view.py"
[[ -x "$JMS" ]] || JMS="jms"

JSON=0
SUMMARY=0
QUIET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --summary) SUMMARY=1; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

OUT="/tmp/chatgpt-acct-quota-last.txt"

if [[ $JSON -eq 1 ]]; then
  "$JMS" ssh JSZX-AI-03 "cat /home/cltx/.chatgpt-quota/state/state.json" | tee "$OUT"
  exit ${PIPESTATUS[0]}
fi

ARGS=""
[[ $SUMMARY -eq 1 ]] && ARGS="--summary"

# 跑脚本 → tee 到 stdout + 副本文件
"$JMS" ssh JSZX-AI-03 "python3 - $ARGS" < "$VIEW" | tee "$OUT"
RC=${PIPESTATUS[0]}

if [[ $RC -ne 0 ]]; then
  echo "[chatgpt-acct-quota] ERROR rc=$RC (副本=$OUT)" >&2
  exit "$RC"
fi

# Footer 自检：BEGIN/END frame 必齐 + rows 必一致
BEGIN_ROWS=$(grep -E '^=== BEGIN .* rows=[0-9]+ ===' "$OUT" | head -1 | sed -E 's/.*rows=([0-9]+).*/\1/')
END_ROWS=$(grep -E '^=== END .* rows=[0-9]+ ===' "$OUT" | head -1 | sed -E 's/.*rows=([0-9]+).*/\1/')
DATA_ROWS=$(grep -cE '^acct-[0-9]+ ' "$OUT" || true)

if [[ -z "${BEGIN_ROWS:-}" || -z "${END_ROWS:-}" ]]; then
  echo "[chatgpt-acct-quota] WARN: BEGIN/END frame 缺失 — 输出可能被截断 (副本=$OUT)" >&2
  exit 3
fi
if [[ "$BEGIN_ROWS" != "$END_ROWS" || "$BEGIN_ROWS" != "$DATA_ROWS" ]]; then
  echo "[chatgpt-acct-quota] WARN: rows 不一致 begin=$BEGIN_ROWS end=$END_ROWS data=$DATA_ROWS (副本=$OUT)" >&2
  exit 4
fi

[[ $QUIET -eq 1 ]] || echo "[chatgpt-acct-quota] OK rows=$BEGIN_ROWS  副本=$OUT" >&2
