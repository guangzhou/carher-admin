#!/usr/bin/env python3
"""paused Deployment 攒了多少漂移：deployment.spec.template vs 活跃 RS 的 template 逐字段比对。
全等 ⇒ resume 不会放出任何东西，可安全 resume→restart→re-pause。
用法：改下面 NS / PREFIX 后在能 kubectl 的机器上跑。见 SKILL.md §5。"""
import subprocess, json, sys
NS="carher"
PREFIX="chatgpt-acct-"
TARGETS=("122","124","125")   # deployment 名的后缀部分
def kj(*a):
    r=subprocess.run(["kubectl","-n",NS,"get",*a,"-o","json"],capture_output=True,text=True)
    if r.returncode: print(r.stderr); sys.exit(1)
    return json.loads(r.stdout)
def walk(p,a,b,out):
    if type(a)!=type(b): out.append((p,a,b)); return
    if isinstance(a,dict):
        for k in sorted(set(a)|set(b)):
            if k in ("creationTimestamp",): continue
            if k not in a or k not in b: out.append((p+"/"+k, a.get(k,"<missing>"), b.get(k,"<missing>")))
            else: walk(p+"/"+k,a[k],b[k],out)
    elif isinstance(a,list):
        if len(a)!=len(b): out.append((p+"[len]",len(a),len(b))); return
        for i,(x,y) in enumerate(zip(a,b)): walk(f"{p}[{i}]",x,y,out)
    elif a!=b: out.append((p,a,b))
for A in TARGETS:
    d=kj("deploy",PREFIX+A)
    dt=d["spec"]["template"]
    rss=[r for r in kj("rs")["items"] if r["metadata"]["name"].startswith(PREFIX+A+"-")
         and (r["spec"].get("replicas") or 0)>0]
    print(f"=== {PREFIX}{A}  活跃RS={[r['metadata']['name'] for r in rss]}")
    for r in rss:
        rt=json.loads(json.dumps(r["spec"]["template"]))
        # RS 模板多一个 pod-template-hash 标签，属正常
        rt.get("metadata",{}).get("labels",{}).pop("pod-template-hash",None)
        diffs=[]; walk("",dt,rt,diffs)
        if not diffs: print("   ✅ 模板逐字段相等 —— 没有攒下任何漂移")
        for p,x,y in diffs: print(f"   ⚠ {p}\n      deploy={x!r}\n      rs    ={y!r}")
