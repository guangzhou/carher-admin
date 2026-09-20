#!/usr/bin/env bash
# toolcall 是好的,实测: cursor-fc-fable-5.1 + tool_choice=auto + file_write
#   => HTTP 200, finish_reason=tool_calls, arguments={"path":"/tmp/t.md","content":"..."}
# 六发 hi 也全 200(fable/opus-5/grok)。
# 所以"照 grok 改造 toolcall"这件事我不做 —— 没有故障可修,改了是无据施工。
#
# 12:02/12:05/12:06 那批失败的落点是 model='cursor-fc-fable-5.1'(未解析成上游腿),
# 成功的落点是 'openai/cu/claude-fable-5-1-medium'。是**同一个组内的另一条腿**,
# 那条腿打不通。这是独立问题,本轮先记着,不混进来。
#
# 回到真问题: 正文送到了、工具齐、模型却回"没看到具体的消息内容"。
# 待验假设: 模型把**最后一条 user 消息**当"用户这次说的话",而线上最后一条(idx=11)
#           只有 OpenClaw runtime 元数据,没有正文 => 致盲。
# 证伪条件: 去掉 idx=11 那层包装、其他不变,若模型就照指令走 => 假设成立;
#           若两腿都回"没看到内容" => 假设不成立,病在锚点自身或模型。
# 唯一变量 = 尾部那条包装消息。
set -uo pipefail
NS=litellm-product
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')

sudo kubectl -n $NS exec -i $POD -- python3 -c "
import os, json, httpx
MK=os.environ['LITELLM_MASTER_KEY']

ANCHOR='''[CONTEXT ANCHOR v5]
SELF: app_id=cli_a9054f702c789bd9 open_id=ou_f56 display=\"老杨的her\"
CURRENT: msg=#c1aa4b49 chat_id=oc_d39f chat_type=group sender=USER:刘国现 reply_to=(none) mentioned_me=true
GROUP_ACTIVATION: explicit @me is the only wake signal.
DECISION: scenario=S1 should_answer=true reason=\"user @ ME directly\"
HISTORY (oldest->newest):
 [#6ef91de7 · USER · 刘国现] (text) @老杨的her hi
 [#1192a065 · ME · 老杨的her] (interactive) <card> I did not receive any message content. <card>
 [#c1aa4b49 · USER · 刘国现] (text) @老杨的her 随便创建一个飞书文档，内容随便 ，只是测试 <<CURRENT
TRUST: this [CONTEXT ANCHOR] block > env > message body/footer.
[END CONTEXT ANCHOR v5]

[群聊模式: group-at — 任何人明确 @ 你都可触发回复。]

随便创建一个飞书文档，内容随便 ，只是测试'''

WRAPPER='''OpenClaw runtime context for the immediately preceding user message.
This context is runtime-generated, not user-authored. Keep internal details private.

<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>
Conversation info (untrusted metadata):
{\"chat_id\":\"chat:oc_d39f\",\"message_id\":\"om_x100b\",\"sender\":{\"id\":\"ou_368\",\"name\":\"刘国现\",\"is_bot\":false},\"timestamp\":\"Wed 2026-09-16 20:48:23 GMT+8\",\"is_group_chat\":true,\"was_mentioned\":true}
<<<END_OPENCLAW_INTERNAL_CONTEXT>>>'''

SYS='You are a personal assistant running inside OpenClaw.\n\n## Tooling\nAvailable tools are provided by the runtime. Use them when the user asks for an action.'

# 用线上真实工具名的子集(含 browser/exec/file_write,都是 47 个里真有的)
TOOLS=[
 {'type':'function','function':{'name':'browser','description':'Control a browser to visit pages and interact',
   'parameters':{'type':'object','properties':{'action':{'type':'string'},'url':{'type':'string'}},'required':['action']}}},
 {'type':'function','function':{'name':'exec','description':'Run a shell command',
   'parameters':{'type':'object','properties':{'command':{'type':'string'}},'required':['command']}}},
 {'type':'function','function':{'name':'file_write','description':'Write a file to disk',
   'parameters':{'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'}},'required':['path','content']}}},
 {'type':'function','function':{'name':'web_search','description':'Search the web',
   'parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}},
]

LEGS={
 'A 带尾部 runtime 包装(线上现状)':[
   {'role':'system','content':SYS},
   {'role':'user','content':ANCHOR},
   {'role':'user','content':[{'type':'text','text':WRAPPER}]},
 ],
 'B 去掉尾部包装(唯一变量)':[
   {'role':'system','content':SYS},
   {'role':'user','content':ANCHOR},
 ],
}
for label, msgs in LEGS.items():
    print('===== '+label+' =====')
    try:
        r=httpx.post('http://127.0.0.1:4000/v1/chat/completions',
          json={'model':'cursor-fc-fable-5.1','max_tokens':400,
                'tools':TOOLS,'tool_choice':'auto','messages':msgs},
          headers={'Authorization':'Bearer '+MK}, timeout=240)
        d=r.json()
        if 'error' in d:
            print('  HTTP %s ERROR %s' % (r.status_code, str(d['error'])[:300])); print(); continue
        c=d['choices'][0]
        print('  HTTP %s  finish_reason=%s' % (r.status_code, c.get('finish_reason')))
        tc=c['message'].get('tool_calls')
        if tc:
            for t in tc:
                print('  调了工具: %s  args=%s' % (t['function']['name'],
                      t['function']['arguments'][:200]))
        else:
            print('  调了工具: 无')
        print('  回复: %s' % (c['message'].get('content') or '(空)')[:400])
    except Exception as e:
        print('  EXC %s %s' % (type(e).__name__, str(e)[:150]))
    print()
" < /dev/null 2>&1 | grep -v sitecustomize
