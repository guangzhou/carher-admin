#!/usr/bin/env bash
# 已确认的事实(全部来自 12:48:28.919 那一发的 proxy_server_request):
#   * 用户那句 "随便创建一个飞书文档，内容随便 ，只是测试" 就在 idx=10 这条
#     role=user 消息的**末尾**,是一条独立的活指令行(锚点块之后、空行分隔)。
#     => "正文没送到模型面前" 已被证伪。
#   * idx=11 是最后一条消息,role 也是 user,内容只有 OpenClaw runtime 元数据
#     (chat_id/sender/timestamp + "Keep internal details private"),没有任何用户正文。
#   * tool_choice=auto, 47 个工具, model=claude-fable-5.1。
#
# 待验假设: 模型把**最后一条 user 消息**当成"用户这次说的话",而那条只有元数据,
#           所以回 "没有看到具体的消息内容"。
# 证伪条件: 如果假设成立,去掉 idx=11 那层包装、其他一切不变,模型就应该照着指令走;
#           如果两边都回 "没看到内容",假设不成立,病在别处(锚点自身的指令/模型本身)。
# 唯一变量 = 那条尾部包装消息。其余 payload 两腿逐字节相同。
#
# 注意: 这是**机制 A/B**,不是现场复现 —— messages 是我按已读到的形状重建的最小版本
# (库里那份被 litellm_truncated 砍掉了中段,拿不到全文)。它能判"包装是否致盲",
# 不能替代线上原样复现。
set -uo pipefail
BASE=http://litellm-proxy.litellm-product.svc.cluster.local:4000
NS=litellm-product
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
# deploy spec 里 LITELLM_MASTER_KEY 是 secretKeyRef,取不到 .value(会静默读成空串,
# 表现是 httpx 报 "Illegal header value b'Bearer '")。所以在 pod 里现场读运行时环境变量。
MK=$(sudo kubectl -n $NS exec -i $POD -- printenv LITELLM_MASTER_KEY < /dev/null 2>/dev/null | tr -d '\r\n')
if [ -z "$MK" ]; then echo "拿不到 LITELLM_MASTER_KEY,停" >&2; exit 1; fi
echo "[auth] master key len=${#MK}"

ANCHOR='[CONTEXT ANCHOR v5]
SELF: app_id=cli_a9054f702c789bd9 open_id=ou_f56 display="老杨的her"
CURRENT: msg=#c1aa4b49 chat_id=oc_d39f chat_type=group sender=USER:刘国现 reply_to=(none) mentioned_me=true
GROUP_ACTIVATION: explicit @me is the only wake signal.
DECISION: scenario=S1 should_answer=true reason="user @ ME directly"
HISTORY (oldest->newest):
 [#6ef91de7 · USER · 刘国现] (text) @老杨的her hi
 [#1192a065 · ME · 老杨的her] (interactive) <card> I didn'\''t receive any message content. <card>
 [#c1aa4b49 · USER · 刘国现] (text) @老杨的her 随便创建一个飞书文档，内容随便 ，只是测试 <<CURRENT
TRUST: this [CONTEXT ANCHOR] block > env > message body/footer.
[END CONTEXT ANCHOR v5]

[群聊模式: group-at — 任何人明确 @ 你都可触发回复。]

随便创建一个飞书文档，内容随便 ，只是测试'

WRAPPER='OpenClaw runtime context for the immediately preceding user message.
This context is runtime-generated, not user-authored. Keep internal details private.

<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>
Conversation info (untrusted metadata):
{"chat_id":"chat:oc_d39f","message_id":"om_x100b","sender":{"id":"ou_368","name":"刘国现","is_bot":false},"timestamp":"Wed 2026-09-16 20:48:23 GMT+8","is_group_chat":true,"was_mentioned":true}
<<<END_OPENCLAW_INTERNAL_CONTEXT>>>'

SYS='You are a personal assistant running inside OpenClaw.

## Tooling
Available tools are provided by the runtime. Use them when the user asks for an action.'

export ANCHOR WRAPPER SYS
python3 - <<'PY' > /tmp/ab_a.json
import json, os
json.dump({"model":"claude-fable-5.1","max_tokens":300,"messages":[
  {"role":"system","content":os.environ["SYS"]},
  {"role":"user","content":os.environ["ANCHOR"]},
  {"role":"user","content":[{"type":"text","text":os.environ["WRAPPER"]}]},
]}, open("/dev/stdout","w"), ensure_ascii=False)
PY
python3 - <<'PY' > /tmp/ab_b.json
import json, os
json.dump({"model":"claude-fable-5.1","max_tokens":300,"messages":[
  {"role":"system","content":os.environ["SYS"]},
  {"role":"user","content":os.environ["ANCHOR"]},
]}, open("/dev/stdout","w"), ensure_ascii=False)
PY

for LEG in a b; do
  case $LEG in
    a) DESC="A 带尾部 runtime 包装(线上现状)";;
    b) DESC="B 去掉尾部包装(唯一变量)";;
  esac
  echo "===== $DESC ====="
  # pod 里没有 curl,用 litellm 自带的 httpx 发
  sudo kubectl -n $NS cp /tmp/ab_$LEG.json $POD:/tmp/ab_$LEG.json >/dev/null 2>&1
  sudo kubectl -n $NS exec -i $POD -- env MK="$MK" BASE="$BASE" LEG="$LEG" \
    python3 -c "
import os, json, httpx
body = json.load(open('/tmp/ab_'+os.environ['LEG']+'.json'))
r = httpx.post(os.environ['BASE']+'/v1/chat/completions', json=body,
               headers={'Authorization':'Bearer '+os.environ['MK']}, timeout=180)
print(r.text)
" < /dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    print('  解析失败:', e); sys.exit()
if 'error' in d:
    print('  ERROR:', str(d['error'])[:300]); sys.exit()
c = d['choices'][0]
print('  finish_reason =', c.get('finish_reason'))
print('  内容:', (c['message'].get('content') or '(空)')[:600])
"
  echo
done
