#!/usr/bin/env python3
"""开 enable_pre_call_checks 之前的影响面审计：
对每条 deployment 算出「闸门若打开，会用哪个 max_input_tokens」——
优先 model_info 显式值，否则 litellm 内置表按 litellm_params.model 查，查不到=无闸门。
"""
import litellm, collections, yaml
cfg = yaml.safe_load(open('/app/config.yaml'))
print('router_settings in config:', cfg.get('router_settings'))
rows = []
for d in cfg['model_list']:
    lp = d.get("litellm_params", {})
    mi = d.get("model_info", {}) or {}
    explicit = mi.get("max_input_tokens")
    inherited = None
    for cand in (lp.get("base_model"), lp.get("model"), d.get("model_name")):
        if not cand:
            continue
        try:
            inherited = litellm.get_model_info(cand).get("max_input_tokens")
            if inherited:
                src = cand
                break
        except Exception:
            continue
    else:
        src = None
    rows.append((d.get("model_name"), mi.get("id"), explicit, inherited, src))

agg = collections.defaultdict(list)
for name, _id, ex, inh, src in rows:
    eff = ex if ex else inh
    agg[(name, ex, inh, src)].append(_id)

print(f"total deployments={len(rows)}")
print(f"{'model_name':30s} {'explicit':>10s} {'inherited':>10s}  n  source-slug")
for (name, ex, inh, src), ids in sorted(agg.items(), key=lambda x: x[0][0]):
    print(f"{name:30s} {str(ex):>10s} {str(inh):>10s} {len(ids):>2d}  {src}")
no_gate = sum(1 for _, _, ex, inh, _ in rows if not ex and not inh)
print(f"\n开闸后仍无闸门(算不出窗口)的 deployment 数 = {no_gate}/{len(rows)}")
