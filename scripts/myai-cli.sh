#!/usr/bin/env bash
# myai（chat.auto-link.com.cn/myai/，ai_token_dashboard @ 243 K3s）只读运维 CLI 包装。
#
# 用法(本地 Mac,需能 ssh cltx@10.68.13.243):
#   scripts/myai-cli.sh install            # 首次:从仓内 tools/ 装 CLI(uv tool, editable)
#   scripts/myai-cli.sh status
#   scripts/myai-cli.sh diagnose sync-stuck
#
# 默认加 --json;要人读格式设 MYAI_HUMAN=1。
# 凭据:MYAI_SSH_PASSWORD 经 sshpass -e 走 env,不落盘不进 argv;不设就用 ssh key(BatchMode)。
# 足迹:全部只读(psql read-only 事务 + 上游 GET),不改 243 任何资源。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS="$REPO/tools/cli-anything-myai"

if [[ "${1:-}" == "install" ]]; then
  command -v uv >/dev/null || { echo "需要 uv:brew install uv" >&2; exit 2; }
  uv tool install --force --editable "$HARNESS"
  echo "installed: $(command -v cli-anything-myai)"
  exit 0
fi

CLI="$(command -v cli-anything-myai || true)"
if [[ -z "$CLI" ]]; then
  echo "cli-anything-myai 未安装,先跑: $0 install" >&2
  exit 2
fi

if [[ -n "${MYAI_HUMAN:-}" ]]; then
  exec "$CLI" "$@"
fi
exec "$CLI" --json "$@"
