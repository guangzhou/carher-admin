"""复现用户 /init 场景：让 zerokey 在一个已有仓库里创建 AGENTS.md。
判据：重复宣告"已完成"的次数（用户现场是 4~5 次）+ 有没有把 BPI 内部标记讲给用户。
关键：apply_patch 必须像真 Codex 一样返回 {}（不是友好字符串）—— 这正是之前漏测的。"""
import json,urllib.request,re,subprocess,os,shutil
KEY='sk-R0YepgJaLzqm7TbFyJhZGQ'; URL='https://cc.auto-link.com.cn/pro/v1/responses?model=zk-115-gpt-5.6-sol'
UA='codex-tui/0.146.1 (Mac OS 26.2.0; arm64) unknown'; SBX='/tmp/zk_harness/initrepo'
ti=next((it for it in json.load(open('/tmp/agents_payload.json'))['input'] if it.get('type')=='additional_tools'),None)
def seed():
    if os.path.exists(SBX): shutil.rmtree(SBX)
    os.makedirs(SBX+'/src')
    open(SBX+'/README.md','w').write('# demo project\npython utility lib\n')
    open(SBX+'/src/main.py','w').write('def main():\n    pass\n')
    open(SBX+'/pyproject.toml','w').write('[project]\nname="demo"\n')
def run_cmd(c):
    p=subprocess.run(c,shell=True,cwd=SBX,capture_output=True,text=True,timeout=30)
    # 忠实模拟 Codex exec_command：返回 JSON 含 output/exit_code
    return json.dumps({"exit_code":p.returncode,"output":(p.stdout+p.stderr)[:2000]},ensure_ascii=False)
def faithful_apply_patch(pl):
    """真 Codex：成功返回 {}（context.rs:275）。之前 harness 返回友好字符串=不忠实。"""
    for hdr in (r'\*\*\* Add File: (.+)',r'\*\*\* Update File: (.+)'):
        m=re.search(hdr,pl)
        if m:
            path=m.group(1).strip(); full=path if path.startswith(SBX) else os.path.join(SBX,path.lstrip('/'))
            body='\n'.join(l[1:] for l in pl.split('\n') if l.startswith('+') and not l.startswith('+++'))
            os.makedirs(os.path.dirname(full) or '.',exist_ok=True); open(full,'w').write(body)
            return '{}'   # ← 忠实：空对象
    return '{}'
def ex(js):
    out=[]
    for m in re.finditer(r'text\("BPI\(([a-z_]+)\):"\)',js): pass
    # 按顺序还原 text() 调用：BPI 标记 + 工具返回
    for m in re.finditer(r'text\("(BPI\([a-z_]+\):)"\);|exec_command\(\{cmd:\s*("(?:[^"\\]|\\.)*")\}\)|apply_patch\(("(?:[^"\\]|\\.)*")\)',js):
        if m.group(1): out.append(m.group(1))
        elif m.group(2): out.append(run_cmd(json.loads(m.group(2))))
        elif m.group(3): out.append(faithful_apply_patch(json.loads(m.group(3))))
    return '\n'.join(out) or '(none)'
def one():
    seed()
    task=f'为 {SBX} 这个仓库创建 AGENTS.md（贡献者指南，含项目结构/构建测试命令/代码规范/提交规范，200-400词）。如已存在则不要覆盖。'
    inp=[ti,{'type':'message','role':'user','content':[{'type':'input_text','text':task}]}]
    done_claims=0; leak=False; turns=0
    for t in range(10):
        turns+=1
        d=json.loads(urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({'model':'gpt-5.6-sol','instructions':'','stream':False,'input':inp}).encode(),headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json','User-Agent':UA}),timeout=180).read())
        tcs=[o for o in d.get('output',[]) if o.get('type')=='custom_tool_call']
        txt=''.join(c.get('text','') for o in d.get('output',[]) for c in (o.get('content') or []))
        if 'BPI(' in txt: leak=True
        if tcs:
            js=tcs[0].get('input',''); res=ex(js)
            inp.append({'type':'custom_tool_call','call_id':tcs[0].get('call_id','c'),'name':'exec','input':js})
            inp.append({'type':'custom_tool_call_output','call_id':tcs[0].get('call_id','c'),'output':[{'type':'input_text','text':res}]})
        else:
            if re.search(r'已完成|已创建|完成：', txt): done_claims+=1
            break
    exists=os.path.exists(SBX+'/AGENTS.md')
    size=os.path.getsize(SBX+'/AGENTS.md') if exists else 0
    return turns,done_claims,leak,exists,size
print("=== /init 场景（忠实 apply_patch 返回 {}）5 次 ===")
for i in range(5):
    tn,dc,lk,ex_,sz=one()
    print(f"run{i+1}: 轮次={tn} 宣告完成={dc}次 BPI标记泄漏={'是' if lk else '否'} AGENTS.md={'有' if ex_ else '无'}({sz}B)")
