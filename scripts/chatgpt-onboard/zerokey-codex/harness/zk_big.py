"""大而可拆的任务：建 4 个独立模块+各自测试，全通过。看单 agent zerokey 扛不扛得住。
这是最该用子 agent 的任务形状（4 块独立工作）。若单 agent 也能 acct 级完成，agents 非差距。"""
import json,urllib.request,re,subprocess,os,shutil,sys
sys.path.insert(0,'/tmp'); from apply_patch import apply_patch as fap
KEY='sk-R0YepgJaLzqm7TbFyJhZGQ'; URL='https://cc.auto-link.com.cn/pro/v1/responses?model=zk-115-gpt-5.6-sol'
UA='codex-tui/0.146.1 (Mac OS 26.2.0; arm64) unknown'; SBX='/tmp/zk_harness/sbx'
def run_cmd(c):
    p=subprocess.run(c,shell=True,cwd=SBX,capture_output=True,text=True,timeout=40); return (p.stdout+p.stderr)[:3000] or f'(exit {p.returncode})'
def ex(js):
    out=[]
    for m in re.finditer(r'exec_command\(\{cmd:\s*("(?:[^"\\]|\\.)*")\}\)',js): out.append(run_cmd(json.loads(m.group(1))))
    for m in re.finditer(r'apply_patch\(("(?:[^"\\]|\\.)*")\)',js): out.append(fap(json.loads(m.group(1)),SBX))
    return '\n'.join(out) or '(none)'
ti=next((it for it in json.load(open('/tmp/agents_payload.json'))['input'] if it.get('type')=='additional_tools'),None)
TASK=(f'在 {SBX} 里建一个工具库，4 个独立模块，每个配 assert 测试：\n'
      f'1) strutil.py: slugify(s) 把 "Hello World" 变 "hello-world"\n'
      f'2) numutil.py: is_prime(n) 判素数\n'
      f'3) listutil.py: chunk(lst,n) 把列表按 n 分块\n'
      f'4) dictutil.py: deep_get(d,path) 按 "a.b.c" 取嵌套值\n'
      f'每个模块一个 test_*.py。最后运行所有测试，全部通过才算完成。')
def one():
    if os.path.exists(SBX): shutil.rmtree(SBX)
    os.makedirs(SBX)
    inp=[ti,{'type':'message','role':'user','content':[{'type':'input_text','text':TASK}]}]
    for t in range(30):
        d=json.loads(urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({'model':'gpt-5.6-sol','instructions':'','stream':False,'input':inp}).encode(),headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json','User-Agent':UA}),timeout=180).read())
        tcs=[o for o in d.get('output',[]) if o.get('type')=='custom_tool_call']
        txt=''.join(c.get('text','') for o in d.get('output',[]) for c in (o.get('content') or []))
        if tcs:
            js=tcs[0].get('input',''); res=ex(js)
            inp.append({'type':'custom_tool_call','call_id':tcs[0].get('call_id','c'),'name':'exec','input':js})
            inp.append({'type':'custom_tool_call_output','call_id':tcs[0].get('call_id','c'),'output':[{'type':'input_text','text':res}]})
        else: break
    # 验：4 模块 + 4 测试都在，且各自 import+基本调用不报错
    mods=['strutil','numutil','listutil','dictutil']
    have=all(os.path.exists(f'{SBX}/{m}.py') for m in mods)
    tests=sum(os.path.exists(f'{SBX}/test_{m}.py') for m in mods)
    # 跑所有测试
    r=subprocess.run('for f in test_*.py; do python3 "$f" || exit 1; done',shell=True,cwd=SBX,capture_output=True,text=True,timeout=40)
    return t+1,have,tests,r.returncode==0
print("=== 大任务(4模块+测试) 单 agent zerokey，4 次 ===")
for i in range(3):
    tn,have,tests,allpass=one()
    print(f"run{i+1}: 轮次={tn} 4模块全在={have} 测试文件={tests}/4 全测通过={allpass}")
