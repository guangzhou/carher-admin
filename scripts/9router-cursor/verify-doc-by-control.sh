#!/bin/sh
# 用**对照模型**把被测模型建的飞书文档读回来 —— 防止"自报自证"。
#
# 规则:被测腿说自己建成功了,不算过。必须由另一条已知健康的腿(默认
# carher-pro/chatgpt-gpt-5.5)独立读到同一篇文档的标题和正文,标题吻合才算过。
# 这是 memory feedback_control_group_must_not_come_from_suspect_metric 的直接落地:
# 对照组不能来自可疑的那把尺子。
#
# 用法(在 188 上执行):
#   ./verify-doc-by-control.sh https://xxx.feishu.cn/docx/AAAA
#   ./verify-doc-by-control.sh https://xxx.feishu.cn/docx/AAAA OPUS5-ACC-1789583461   # 顺带比标题
#   CONTROL=carher-pro/chatgpt-gpt-5.5 ./verify-doc-by-control.sh <url>

set -eu

URL="${1:?usage: $0 <feishu-docx-url> [expected-title]}"
WANT="${2:-}"
CONTROL="${CONTROL:-carher-pro/chatgpt-gpt-5.5}"
CONTAINER="${CONTAINER:-hermestest-14}"
TIMEOUT="${TIMEOUT:-600}"
TOKEN="${OPENCLAW_GATEWAY_TOKEN:-carher-container-token}"

Q="用 lark-doc 技能读取这个飞书文档 ${URL} 的标题和正文全文,原样输出。最后一行输出 TITLE=<标题>"
SK="agent:main:VERIFYDOC-$(date +%s)"

echo "CONTROL=$CONTROL"
echo "URL=$URL"
OUT="$(docker exec -e OPENCLAW_GATEWAY_TOKEN="$TOKEN" "$CONTAINER" \
        sh -c "openclaw agent --session-key '$SK' --model '$CONTROL' --timeout $TIMEOUT --message '$Q'" 2>&1 || true)"
echo "$OUT" | tail -30

echo
T="$(echo "$OUT" | grep -Eo 'TITLE=.*' | tail -1 || true)"
if [ -z "$T" ]; then
  echo "RESULT=CONTROL_CANNOT_READ — 对照腿读不到。**先确认对照腿自己是健康的**(拿一篇已知存在的老文档试),"
  echo "                            对照腿红了不能拿来判被测腿。"
  exit 1
fi
echo "GOT=$T"
if [ -n "$WANT" ]; then
  case "$T" in *"$WANT"*) echo "RESULT=VERIFIED (title matches)" ;;
                       *) echo "RESULT=TITLE_MISMATCH want=$WANT"; exit 1 ;; esac
else
  echo "RESULT=VERIFIED (control model read it back; 没给期望标题,人工核对上面这行)"
fi
