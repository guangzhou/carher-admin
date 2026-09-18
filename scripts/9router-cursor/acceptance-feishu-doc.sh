#!/bin/sh
# 9router 反代腿「工具链真实验收」—— 让被测模型用本地 lark-doc skill 真建一份飞书文档。
#
# 为什么必须是这个而不是 curl:
#   小合成 curl 触发不到 field17 工具流,**永远假绿**。判工具链只认 `openclaw agent`
#   的真实回合(约 46 个工具声明 + 多轮 tool result 回灌)。见 memory
#   feedback_openclaw_agent_is_the_only_valid_toolcall_ruler。
#
# 为什么题面写得这么死:
#   反代腿一旦历史被丢,模型的典型逃逸姿势是**反问**("请提供标题/目录")。反问会被
#   误读成"模型不配合",实际是基础设施把任务弄丢了。所以题面必须封掉所有反问出口,
#   把"缺省值你自己定"明写进去,让唯一可能的失败是真失败。
#
# 用法(在 188 上执行;本机中国大陆出口打不通上游):
#   ./acceptance-feishu-doc.sh litellm/opus-5
#   ./acceptance-feishu-doc.sh litellm/opus-5 MYTAG-123
#   CONTAINER=hermestest-14 TIMEOUT=900 ./acceptance-feishu-doc.sh carher-pro/chatgpt-gpt-5.5
#
# 判据(三条都要,缺一不算过):
#   1) 本脚本最后一行 DOC_URL=...           <- 模型自报
#   2) verify-doc-by-control.sh 用**对照模型**把这篇读回来  <- 不许自报自证
#   3) trace-9router.sh 里有配套的 exec lark-cli docs +create,且 sessions_history=0
#
# ⚠️ 对照模型固定用一条已知健康的腿(carher-pro/chatgpt-gpt-5.5),它是阳性对照;
#    对照腿自己红了,先修对照腿,别改被测腿。

set -eu

M="${1:?usage: $0 <model> [tag]   e.g. $0 litellm/opus-5}"
N="${2:-ACC-$(date +%s)}"
CONTAINER="${CONTAINER:-hermestest-14}"
TIMEOUT="${TIMEOUT:-900}"
TOKEN="${OPENCLAW_GATEWAY_TOKEN:-carher-container-token}"   # openclaw 无 --token,只吃 env

Q="立刻执行以下任务,不要反问、不要确认、不要请求补充信息:调用 lark-doc 技能,在飞书里新建一份 docx 云文档。标题固定为:${N}。正文只写一行固定文字:这是 ${M} 通过 9router 反代创建的验收文档。所有参数都已给全,没有任何需要我补充的东西;缺省值你自己定。做完后在回复的最后一行只输出:DOC_URL=<新建文档的完整链接>。如果创建失败,最后一行输出:DOC_FAIL=<失败原因>。"

SK="agent:main:$(echo "$M" | tr '/.' '__')-$N"
echo "MODEL=$M"
echo "TAG=$N"
echo "SESSION=$SK"
echo "--- agent run (timeout ${TIMEOUT}s) ---"

OUT="$(docker exec -e OPENCLAW_GATEWAY_TOKEN="$TOKEN" "$CONTAINER" \
        sh -c "openclaw agent --session-key '$SK' --model '$M' --timeout $TIMEOUT --message '$Q'" 2>&1 || true)"
echo "$OUT" | tail -60

echo
URL="$(echo "$OUT" | grep -Eo 'DOC_URL=[^ ]+' | tail -1 || true)"
FAIL="$(echo "$OUT" | grep -Eo 'DOC_FAIL=.*' | tail -1 || true)"
if [ -n "$URL" ]; then
  echo "RESULT=SELF_REPORTED_OK  $URL"
  echo "NEXT=./verify-doc-by-control.sh '${URL#DOC_URL=}'   # 必须跑,自报不算过"
elif [ -n "$FAIL" ]; then
  echo "RESULT=FAIL  $FAIL"; exit 1
else
  # 最常见的这一类:模型在反问。那不是"模型不配合",先去查历史有没有被丢。
  case "$OUT" in
    *请提供*|*请告知*|*需要你*|*确认一下*)
      echo "RESULT=ASKED_BACK — 题面已封死反问出口却还在反问,**先跑 probe-fold.sh 查 f7/f8**,不要调提示词" ;;
    *) echo "RESULT=NO_MARKER — 既没 DOC_URL 也没 DOC_FAIL,看上面全文" ;;
  esac
  exit 1
fi
