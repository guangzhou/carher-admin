import json,urllib.request
BASE="http://10.68.13.198:30402"; MK="sk-pro-litellm-ce077e2b0721bb419a633e4d"
def api(m,p,d=None):
    body=json.dumps(d).encode() if d else None
    req=urllib.request.Request(f"{BASE}{p}",data=body,headers={"Authorization":f"Bearer {MK}","Content-Type":"application/json"},method=m)
    with urllib.request.urlopen(req,timeout=20) as r: return json.loads(r.read())
pods=[25,87,88,89,90,91,92,93,94,95,96,97,98,99]
for variant in ["terra","luna"]:
    ok=0
    for n in pods:
        entry={"model_name":f"zerokey-pool-gpt-5.6-{variant}",
          "litellm_params":{"model":f"openai/gpt-5.6-{variant}","api_base":f"http://zero-{n}.litellm-product.svc.cluster.local:8200/v1","api_key":"raw","rpm":30,"input_cost_per_token":5e-6,"output_cost_per_token":3e-5},
          "model_info":{"id":f"zk-{n}-gpt-5.6-{variant}","mode":"responses"}}
        try: api("POST","/pro/model/new",entry); ok+=1
        except Exception as e: print(f"zk-{n}-{variant} FAIL {str(e)[:60]}")
    print(f"gpt-5.6-{variant}: registered {ok}/{len(pods)}")
