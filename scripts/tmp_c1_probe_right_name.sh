#!/usr/bin/env bash
# 纠正两条我说错的话:
#
# (1) "carher-1 的两个模型现在 402、线上是坏的" —— **错,是我探针打错了名字。**
#     SpendLogs 真实落点表说明 carher-1 用的是 model_group=cursor-fc-fable-5.1,
#     落到 openai/cu/claude-fable-5-1-medium(=9router 腿),10 行,last_seen 12:48:28。
#     而 claude-fable-5.1 那个组 last_seen 是 09-16 07:53,carher-1 **早就不打它了**。
#     所以那个组四条腿全是用完的 copilot2api、打了必 402 —— 是预期,不是故障。
#
# (2) "各有 2 行、一条 9router 一条 copilot、摘旧腿" —— 错得更远,
#     那 4 行全是 copilot,真要摘就摘空了。**不摘。**
#
# 结论: 09-17 那轮 repoint 是**换名字**(carher-1 改打 cursor-fc-*),
# 不是在原组里加腿。claude-fable-5.1 那个组是别人的历史资产,与本次无关,不许动。
#
# 本轮: 打**正确的名字**,确认线上到底好没好。这是判"要不要改 toolcall"的前提。
set -uo pipefail
NS=litellm-product
POD=$(sudo kubectl -n $NS get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')

echo "=== 1) 用正确名字直打(carher-1 真实在用的组) ==="
sudo kubectl -n $NS exec -i $POD -- python3 -c "
import os, httpx
MK=os.environ['LITELLM_MASTER_KEY']
for name in ['cursor-fc-fable-5.1','cursor-fc-opus-5','claude-grok-4.6']:
    for i in range(2):
        try:
            r=httpx.post('http://127.0.0.1:4000/v1/chat/completions',
              json={'model':name,'max_tokens':20,'messages':[{'role':'user','content':'hi'}]},
              headers={'Authorization':'Bearer '+MK}, timeout=180)
            d=r.json()
            if 'error' in d:
                print('  %-22s r%d HTTP %s | %s' % (name,i+1,r.status_code,
                      str(d['error'].get('message'))[:110]))
            else:
                c=d['choices'][0]
                print('  %-22s r%d HTTP %s | OK fr=%s | %s' % (name,i+1,r.status_code,
                      c.get('finish_reason'),
                      (c['message'].get('content') or '')[:40].replace(chr(10),' ')))
        except Exception as e:
            print('  %-22s r%d EXC %s %s' % (name,i+1,type(e).__name__,str(e)[:80]))
" < /dev/null 2>&1 | grep -v sitecustomize

echo
echo "=== 2) 带工具打一发(复现 carher-1 的真实形状: tool_choice=auto + 工具) ==="
sudo kubectl -n $NS exec -i $POD -- python3 -c "
import os, httpx, json
MK=os.environ['LITELLM_MASTER_KEY']
tools=[{'type':'function','function':{'name':'file_write','description':'Write a file',
        'parameters':{'type':'object','properties':{'path':{'type':'string'},
        'content':{'type':'string'}},'required':['path','content']}}},
       {'type':'function','function':{'name':'web_search','description':'Search the web',
        'parameters':{'type':'object','properties':{'query':{'type':'string'}},
        'required':['query']}}}]
for name in ['cursor-fc-fable-5.1']:
    try:
        r=httpx.post('http://127.0.0.1:4000/v1/chat/completions',
          json={'model':name,'max_tokens':300,'tools':tools,'tool_choice':'auto',
                'messages':[{'role':'user','content':'用 file_write 建一个 /tmp/t.md,内容随便写一句话'}]},
          headers={'Authorization':'Bearer '+MK}, timeout=240)
        d=r.json()
        if 'error' in d:
            print('  %s | HTTP %s | %s' % (name,r.status_code,str(d['error'])[:400]))
        else:
            c=d['choices'][0]
            print('  %s | HTTP %s | finish_reason=%s' % (name,r.status_code,c.get('finish_reason')))
            tc=c['message'].get('tool_calls')
            print('    tool_calls =', json.dumps(tc,ensure_ascii=False)[:400] if tc else 'None')
            print('    content    =', (c['message'].get('content') or '(空)')[:300])
    except Exception as e:
        print('  %s EXC %s %s' % (name,type(e).__name__,str(e)[:150]))
" < /dev/null 2>&1 | grep -v sitecustomize

echo
echo "=== 3) 12:06 那批失败的报错原文(unsupported IDE tool 的现场) ==="
sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
"select \"startTime\" || ' | ' || left(coalesce(metadata->>'error_information',
        metadata::text), 700)
 from \"LiteLLM_SpendLogs\"
 where \"api_key\"='a538f674b895678f417eea9e277f75a24bf94333f388b96b0546167a6d779bb7'
   and \"model_group\"='cursor-fc-fable-5.1' and \"model\"='cursor-fc-fable-5.1'
 order by \"startTime\" desc limit 3;" < /dev/null 2>&1 | grep -v '^\[sudo\]'
