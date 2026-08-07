#!/usr/bin/env python3
"""给 litellm-config CM 里未报价的 entry 补 model_info 价格 (CM 侧惯例位置)。
结构化改 YAML, 不做文本替换; 改前后断言 entry 数与其它 entry 逐字节不变。"""
import json,subprocess,sys,yaml,copy
NS="litellm-product"; CM="litellm-config"
# 网宿车联天下1 渠道: 同渠道兄弟(kimi-k2.7-code / qwen3.7-plus / glm-5.2 共 6 条)统一 1.4e-06/4.4e-06
# 参考: qwen3.7-max 阿里 list $2.50/$7.50, OpenRouter $1.475/$4.425 -> 转售价落在 1.4/4.4 一档
TARGETS={"wangsu-cheliantianxia1-qwen3.7-max":(1.4e-06,4.4e-06)}
raw=subprocess.run(f"kubectl -n {NS} get cm {CM} -o json",shell=True,capture_output=True,text=True).stdout
data=json.loads(raw)["data"]
cfg=yaml.safe_load(data["config.yaml"]); before=copy.deepcopy(cfg)
ml=cfg["model_list"]; hit=0
for e in ml:
    p=TARGETS.get(e.get("model_name"))
    if not p: continue
    mi=e.setdefault("model_info",{})
    if mi.get("input_cost_per_token"): print(f"  SKIP {e['model_name']} 已有价"); continue
    mi["input_cost_per_token"],mi["output_cost_per_token"]=p; hit+=1
    print(f"  SET  {e['model_name']} -> {p}")
# 断言
assert len(ml)==len(before["model_list"])==74, f"entry 数变了: {len(ml)}"
for a,b in zip(before["model_list"],ml):
    if a.get("model_name") not in TARGETS:
        assert a==b, f"误伤了 {a.get('model_name')}"
assert set(cfg)==set(before), "顶层键变了"
print(f"  改动 {hit} 条, 其余 {len(ml)-hit} 条逐字段相同, 顶层键不变")
if "--apply" not in sys.argv:
    print("  DRY RUN"); sys.exit(0)
data["config.yaml"]=yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False,default_flow_style=False)
patch=json.dumps({"data":{"config.yaml":data["config.yaml"]}})
open("/tmp/cm_patch_hzl.json","w").write(patch)
r=subprocess.run(f"kubectl -n {NS} patch cm {CM} --type merge --patch-file /tmp/cm_patch_hzl.json",
                 shell=True,capture_output=True,text=True)
print(" ",r.stdout.strip() or r.stderr.strip())
