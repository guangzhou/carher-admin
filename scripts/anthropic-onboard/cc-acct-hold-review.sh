#!/usr/bin/env bash
# cc-acct-hold-review.sh — 提交 Anthropic 账户 hold 申诉
#
# 用法:
#   ./scripts/anthropic-onboard/cc-acct-hold-review.sh acct-N
#
# 前提: .creds 含 email + mail_pw 字段（同 mailcom flow）

set -eo pipefail

ACCT="${1:?用法: $0 acct-N}"
if [[ ! "$ACCT" =~ ^acct-[0-9]+$ ]]; then
    echo "❌ ACCT 格式必须是 acct-N"; exit 1
fi
SSH_188="cltx@10.68.13.188"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/cc-acct-hold-review.py"
[[ -f "$PY_SCRIPT" ]] || { echo "❌ 找不到 $PY_SCRIPT"; exit 1; }

CREDS_REMOTE="/Data/anthropic-auth/$ACCT/.creds"
SS_DIR="/tmp/cc-review-$ACCT"
PY_REMOTE="/tmp/cc-acct-hold-review.py"
LOG="/tmp/cc-review-$ACCT.log"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  cc-acct-hold-review.sh  $ACCT"
echo "╚══════════════════════════════════════════════════════════════╝"

ssh "$SSH_188" "test -f $CREDS_REMOTE" || { echo "❌ $CREDS_REMOTE 不存在"; exit 1; }
scp -q "$PY_SCRIPT" "$SSH_188:$PY_REMOTE"

CC_EMAIL=$(ssh "$SSH_188" "grep ^email= $CREDS_REMOTE | cut -d= -f2-")
MAIL_PW=$(ssh "$SSH_188" "grep ^mail_pw= $CREDS_REMOTE | cut -d= -f2-")
[[ -n "$MAIL_PW" ]] || { echo "❌ .creds 缺 mail_pw="; exit 1; }

# 申诉理由 (日语 — 公司内网使用 + 多账号试用计划)
REASON="弊社の社内ネットワーク（イントラネット）での業務利用です。まず1アカウントを購入して動作検証を行っており、問題なければ追加で複数アカウントを購入する予定です。アカウント保留により業務テストが中断しておりますので、解除をお願いいたします。"

echo "==[1/2]== Docker patchright 跑申诉流程 (~3-5min)"
echo "  reason (ja, len=${#REASON}): ${REASON:0:60}..."

ssh "$SSH_188" "
    rm -rf $SS_DIR && mkdir -p $SS_DIR
    docker run --rm \
      -v $PY_REMOTE:/work/script.py:ro \
      -v $SS_DIR:/work/screenshots \
      -e CC_EMAIL='$CC_EMAIL' \
      -e MAIL_PW='$MAIL_PW' \
      -e REVIEW_REASON='$REASON' \
      -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 \
      mcr.microsoft.com/playwright/python:v1.59.0-noble \
      bash -c 'Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && \
               pip install patchright==1.59.1 -q --root-user-action=ignore 2>&1 | tail -1 && \
               python3 /work/script.py' 2>&1 | tee $LOG
"

echo ""
echo "==[2/2]== 截图列表"
ssh "$SSH_188" "ls -la $SS_DIR/"
echo ""
echo "看完整 log:    jms ssh JSZX-AI-03 'cat $LOG'"
echo "拉截图:        scp $SSH_188:$SS_DIR/*.png /tmp/"
