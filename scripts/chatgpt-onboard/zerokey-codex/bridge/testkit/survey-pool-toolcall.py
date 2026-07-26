import json,urllib.request
from concurrent.futures import ThreadPoolExecutor
GUIDE="You are a coding agent with a real shell tool. You MUST call the tool to run commands."
TOOLS=[{"type":"function","name":"shell_enqueue_job","description":"Enqueue a shell command for the worker to run; its output is returned to you next turn.","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}]
def probe(p):
    b={"model":"gpt-5.6-sol","stream":False,
       "input":[{"role":"system","content":GUIDE},{"role":"user","content":"run: echo hi"}],
       "tools":TOOLS}
    r=urllib.request.Request("http://%s.litellm-product.svc.cluster.local:8200/v1/responses"%p,
      data=json.dumps(b).encode(),headers={"Authorization":"Bearer raw","Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(r,timeout=120) as resp:
            d=json.loads(resp.read().decode("utf-8","ignore"))
        for o in d.get("output") or []:
            if o.get("type") in ("function_call","custom_tool_call"): return (p,"CALL")
        return (p,"TEXT")
    except Exception as e:
        try:
            body=e.read().decode()
            if "no Codex tokens available" in body: return (p,"NOTOKEN")
            return (p,"ERR%s"%getattr(e,'code',''))
        except Exception: return (p,"ERR")
pods=["zero-%d"%i for i in [28,50,52,49]+list(range(81,122))+[127,128,129,130,131]]
res=[]
for i in range(0,len(pods),4):
    with ThreadPoolExecutor(max_workers=4) as ex: res+=list(ex.map(probe,pods[i:i+4]))
from collections import Counter
c=Counter(k for _,k in res)
print("TOTAL:",dict(c))
print("NOTOKEN pods:", ",".join(p for p,k in res if k=="NOTOKEN"))
print("GOOD pods   :", ",".join(p for p,k in res if k=="CALL"))
