"""真实考题：给带 bug 的仓库，让 zerokey 找+改+验证。考 探索+推理+改+验证 全链路。"""
import json,urllib.request,re,subprocess,os,shutil,sys
sys.path.insert(0,'/tmp')
from apply_patch import apply_patch as faithful_ap
KEY='sk-R0YepgJaLzqm7TbFyJhZGQ'; URL='https://cc.auto-link.com.cn/pro/v1/responses?model=zk-115-gpt-5.6-sol'
UA='codex-tui/0.146.1 (Mac OS 26.2.0; arm64) unknown'; SBX='/tmp/zk_harness/sbx'; SRC='/tmp/zk_harness/repo'
def seed():
    if os.path.exists(SBX): shutil.rmtree(SBX)
    shutil.copytree(SRC,SBX)
def run_cmd(c):
    p=subprocess.run(c,shell=True,cwd=SBX,capture_output=True,text=True,timeout=30); return (p.stdout+p.stderr)[:3000] or f'(exit {p.returncode})'
def ap(pl):
    for hdr in (r'\*\*\* Update File: (.+)',r'\*\*\* Add File: (.+)'):
        m=re.search(hdr,pl)
        if m:
            path=m.group(1).strip(); full=path if path.startswith(SBX) else os.path.join(SBX,path.lstrip('/'))
            if 'Update File' in pl and os.path.exists(full):
                # 极简 update：按 -/+ 行做整体替换（够测就行）
                old=[l[1:] for l in pl.split('\n') if l.startswith('-') and not l.startswith('---')]
                new=[l[1:] for l in pl.split('\n') if l.startswith('+') and not l.startswith('+++')]
                txt=open(full).read()
                if old: txt=txt.replace('\n'.join(old),'\n'.join(new))
                else: txt='\n'.join(new)
                open(full,'w').write(txt); return f'updated {path}'
            body='\n'.join(l[1:] for l in pl.split('\n') if l.startswith('+') and not l.startswith('+++'))
            os.makedirs(os.path.dirname(full),exist_ok=True); open(full,'w').write(body); return f'wrote {path}'
    return 'ap?'
def ex(js):
    out=[]
    for m in re.finditer(r'exec_command\(\{cmd:\s*("(?:[^"\\]|\\.)*")\}\)',js): out.append(run_cmd(json.loads(m.group(1))))
    for m in re.finditer(r'apply_patch\(("(?:[^"\\]|\\.)*")\)',js): out.append(faithful_ap(json.loads(m.group(1)),SBX))
    return '\n'.join(out) or '(none)'
ti=next((it for it in json.load(open('/tmp/agents_payload.json'))['input'] if it.get('type')=='additional_tools'),None)
def one():
    seed()
    task=(f'{SBX} 是个 python 项目，运行 python3 test_cart.py 会失败。'
          f'请找出 cart.py 里的 bug 并修好，直到 test_cart.py 全部通过。')
    inp=[ti,{'type':'message','role':'user','content':[{'type':'input_text','text':task}]}]
    read=grep=0
    for t in range(12):
        d=json.loads(urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({'model':'gpt-5.6-sol','instructions':'','stream':False,'input':inp}).encode(),headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json','User-Agent':UA}),timeout=180).read())
        tcs=[o for o in d.get('output',[]) if o.get('type')=='custom_tool_call']
        txt=''.join(c.get('text','') for o in d.get('output',[]) for c in (o.get('content') or []))
        if tcs:
            js=tcs[0].get('input','')
            if 'cat ' in js or 'BPI(read)' in js: read+=1
            if 'grep' in js or 'rg ' in js: grep+=1
            res=ex(js)
            inp.append({'type':'custom_tool_call','call_id':tcs[0].get('call_id','c'),'name':'exec','input':js})
            inp.append({'type':'custom_tool_call_output','call_id':tcs[0].get('call_id','c'),'output':[{'type':'input_text','text':res}]})
        else:
            break
        if t>=11: break
    ok=subprocess.run('python3 test_cart.py',shell=True,cwd=SBX,capture_output=True,text=True).returncode==0
    return t+1,ok
print("=== zerokey 修 bug，5 次 ===")
import sys
for i in range(6):
    tn,ok=one(); print(f"run{i+1}: {'修好✓' if ok else '没修好✗'}  轮次={tn}")
