"""最小委派机制头对头：主 agent 用 ⟦agent¦task=X⟧ 把独立子任务派给专注子循环。
子循环 = 全新 zerokey 会话 + 单任务（zerokey 单任务已 15/15）+ 自己的工具循环，
干完把摘要交回主 agent。量：委派 vs 单agent 在大任务上的 轮次/一致性。"""
import json,urllib.request,re,subprocess,os,shutil,sys
sys.path.insert(0,'/tmp'); from apply_patch import apply_patch as fap
KEY='sk-R0YepgJaLzqm7TbFyJhZGQ'; URL='https://cc.auto-link.com.cn/pro/v1/responses?model=zk-115-gpt-5.6-sol'
UA='codex-tui/0.146.1 (Mac OS 26.2.0; arm64) unknown'; SBX='/tmp/zk_harness/sbx'
ti=next((it for it in json.load(open('/tmp/agents_payload.json'))['input'] if it.get('type')=='additional_tools'),None)
def run_cmd(c):
    p=subprocess.run(c,shell=True,cwd=SBX,capture_output=True,text=True,timeout=40); return (p.stdout+p.stderr)[:2500] or f'(exit {p.returncode})'
def ex(js):
    out=[]
    for m in re.finditer(r'exec_command\(\{cmd:\s*("(?:[^"\\]|\\.)*")\}\)',js): out.append(run_cmd(json.loads(m.group(1))))
    for m in re.finditer(r'apply_patch\(("(?:[^"\\]|\\.)*")\)',js): out.append(fap(json.loads(m.group(1)),SBX))
    return '\n'.join(out) or '(none)'
def post(inp):
    return json.loads(urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({'model':'gpt-5.6-sol','instructions':'','stream':False,'input':inp}).encode(),headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json','User-Agent':UA}),timeout=180).read())
def subloop(task,maxturns=8):
    """子 agent：全新会话跑单任务，返回(轮次, 最后文本摘要)"""
    inp=[ti,{'type':'message','role':'user','content':[{'type':'input_text','text':task}]}]
    for t in range(maxturns):
        d=post(inp); tcs=[o for o in d.get('output',[]) if o.get('type')=='custom_tool_call']
        txt=''.join(c.get('text','') for o in d.get('output',[]) for c in (o.get('content') or []))
        if tcs:
            js=tcs[0].get('input',''); res=ex(js)
            inp.append({'type':'custom_tool_call','call_id':tcs[0].get('call_id','c'),'name':'exec','input':js})
            inp.append({'type':'custom_tool_call_output','call_id':tcs[0].get('call_id','c'),'output':[{'type':'input_text','text':res}]})
        else: return t+1,txt[:100]
    return maxturns,'(hit sub cap)'
def main_with_delegation():
    if os.path.exists(SBX): shutil.rmtree(SBX)
    os.makedirs(SBX)
    guide=('你可以把**独立**子任务委派给专注的子 agent：写 ⟦agent¦task=一句话完整描述（含绝对路径）⟧，'
           '每个子 agent 会独立完成并把结果交回给你。适合多个互不依赖的模块。委派后你会收到每个的完成摘要，'
           '再统一验证。')
    TASK=(f'{guide}\n\n在 {SBX} 里建工具库，4 个**独立**模块各配 assert 测试：\n'
          f'1) strutil.py: slugify(s) "Hello World"->"hello-world"\n'
          f'2) numutil.py: is_prime(n)\n3) listutil.py: chunk(lst,n)\n4) dictutil.py: deep_get(d,path) 按"a.b.c"取值\n'
          f'每个配 test_*.py。全部测试通过才完成。')
    inp=[ti,{'type':'message','role':'user','content':[{'type':'input_text','text':TASK}]}]
    main_turns=0; sub_turns=0; delegated=0
    for t in range(16):
        main_turns+=1; d=post(inp)
        tcs=[o for o in d.get('output',[]) if o.get('type')=='custom_tool_call']
        txt=''.join(c.get('text','') for o in d.get('output',[]) for c in (o.get('content') or []))
        # 抓委派块 ⟦agent¦task=...⟧
        agents=re.findall(r'⟦agent[¦|]task=([^⟧]+)⟧', txt) + re.findall(r'⟦agent[¦|]task=([^⟧]+)⟧', tcs[0].get('input','') if tcs else '')
        if agents:
            results=[]
            for a in agents:
                delegated+=1; st,summary=subloop(a.strip())
                sub_turns+=st; results.append(f'[子agent完成] {a.strip()[:40]} -> {summary}')
            inp.append({'type':'message','role':'assistant','content':[{'type':'input_text','text':'(已委派 '+str(len(agents))+' 个子agent)'}]})
            inp.append({'type':'message','role':'user','content':[{'type':'input_text','text':'[上一步执行结果]\n'+'\n'.join(results)+'\n（子agent已各自完成，请验证全部测试通过后给最终答复。）'}]})
            continue
        if tcs:
            js=tcs[0].get('input',''); res=ex(js)
            inp.append({'type':'custom_tool_call','call_id':tcs[0].get('call_id','c'),'name':'exec','input':js})
            inp.append({'type':'custom_tool_call_output','call_id':tcs[0].get('call_id','c'),'output':[{'type':'input_text','text':res}]})
        else: break
    r=subprocess.run('for f in test_*.py; do python3 "$f" || exit 1; done',shell=True,cwd=SBX,capture_output=True,text=True,timeout=40)
    mods=all(os.path.exists(f'{SBX}/{m}.py') for m in ['strutil','numutil','listutil','dictutil'])
    return main_turns,sub_turns,delegated,mods,r.returncode==0
print("=== 带委派机制，大任务 3 次 ===")
for i in range(3):
    mt,st,dg,mods,ok=main_with_delegation()
    print(f"run{i+1}: 主轮次={mt} 子轮次合计={st} 委派数={dg} 4模块全在={mods} 全测通过={ok}")
