import json,urllib.request
BASE="http://10.68.13.198:30402"; MK="sk-pro-litellm-ce077e2b0721bb419a633e4d"
def api(method,path,data=None):
    body=json.dumps(data).encode() if data else None
    req=urllib.request.Request(f"{BASE}{path}",data=body,headers={"Authorization":f"Bearer {MK}","Content-Type":"application/json"},method=method)
    with urllib.request.urlopen(req,timeout=20) as r: return json.loads(r.read())
pods=[25,87,88,89,90,91,92,93,94,95,96,97,98,99]
ok=0
for n in pods:
    entry={
      "model_name":"zerokey-pool-gpt-5.6-sol",
      "litellm_params":{"model":"openai/gpt-5.6-sol","api_base":f"http://zero-{n}.litellm-product.svc.cluster.local:8200/v1","api_key":"raw","rpm":30,"input_cost_per_token":5e-6,"output_cost_per_token":3e-5},
      "model_info":{"id":f"zk-{n}-gpt-5.6-sol","mode":"responses"}
    }
    try:
        api("POST","/pro/model/new",entry); ok+=1; print(f"zk-{n}-gpt-5.6-sol registered")
    except Exception as e:
        print(f"zk-{n} FAIL {str(e)[:80]}")
print("registered",ok,"/",len(pods))
