#!/usr/bin/env python3
"""sec_redact 影子命中报告（prod + gray 两条车道，只读，198 上 sudo 跑）。

用法: shadow_report2.py <since RFC3339> [--json out.json]
输出（按 key 家族聚合，不打印 key 别名明细，不打印原文——日志里本来也只有 sha/掩码）：
  * 每条车道 POST 数 / 命中请求数
  * 方向×规则：命中次数（含 hit-summary 合并的计数）/ 命中请求数 / 涉及 key 数 / 涉及家族
  * 规则×家族：命中请求数
  * 每条规则的典型掩码片段（取出现最多的前 5 个，用于人工判误报）
  * 每请求命中数分布（判「整段历史重发」放大）
"""
import collections
import json
import re
import subprocess
import sys

ENV = {"KUBECONFIG": "/etc/rancher/k3s/k3s.yaml", "PATH": "/usr/local/bin:/usr/bin:/bin"}
LANES = {"prod": "litellm-proxy", "gray": "litellm-proxy-gray"}
since = sys.argv[1]
out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
KV = re.compile(r"(\w+)=('[^']*'|\"[^\"]*\"|\S+)")


def fam(alias):
    a = alias or "-"
    return a.split("-")[0] if "-" in a else a


per_lane, hits, summ = {}, [], []
for lane, app in LANES.items():
    pods = subprocess.run(["kubectl", "-n", "litellm-product", "get", "po", "-l", f"app={app}", "-o", "name"],
                          capture_output=True, text=True, env=ENV).stdout.split()
    posts = 0
    for p in pods:
        txt = subprocess.run(["kubectl", "-n", "litellm-product", "logs", p, "-c", "litellm", f"--since-time={since}"],
                             capture_output=True, text=True, env=ENV).stdout
        for l in txt.splitlines():
            if '"POST /' in l:
                posts += 1
            if "sec_redact: hit-summary" in l:
                d = dict(KV.findall(l.split("sec_redact: hit-summary", 1)[1]))
                d["lane"] = lane
                summ.append(d)
            elif "sec_redact: hit " in l:
                d = dict(KV.findall(l.split("sec_redact: hit", 1)[1]))
                d["lane"] = lane
                hits.append(d)
    per_lane[lane] = {"pods": len(pods), "posts": posts}

for lane in LANES:
    rq = {(h["lane"], h.get("req")) for h in hits if h["lane"] == lane}
    per_lane[lane]["requests_with_hit"] = len(rq)

cnt = collections.Counter()
reqs, keys, fams = (collections.defaultdict(set) for _ in range(3))
masks = collections.defaultdict(collections.Counter)
rule_fam = collections.defaultdict(lambda: collections.defaultdict(set))
per_req = collections.Counter()
for h in hits:
    k = (h.get("dir"), h.get("rule"))
    cnt[k] += 1
    reqs[k].add(h.get("req"))
    keys[k].add(h.get("key"))
    fams[k].add(fam(h.get("key")))
    masks[h.get("rule")][h.get("masked", "")] += 1
    rule_fam[h.get("rule")][fam(h.get("key"))].add(h.get("req"))
    per_req[h.get("req")] += 1
for s in summ:
    for part in s.get("by_rule", "").split(","):
        if ":" in part:
            r, n = part.rsplit(":", 1)
            cnt[(s.get("dir"), r)] += int(n)
            per_req[s.get("req")] += int(n)

print(f"since {since}")
for lane, v in per_lane.items():
    print(f"  {lane}: pods={v['pods']} POST={v['posts']} requests_with_hit={v['requests_with_hit']}")
print("\ndir       rule              hits   reqs  keys  families")
rows = []
for k, v in cnt.most_common():
    row = {"dir": k[0], "rule": k[1], "hits": v, "reqs": len(reqs[k]), "keys": len(keys[k]),
           "families": sorted(fams[k])}
    rows.append(row)
    print(f"{k[0] or '-':9} {k[1] or '-':16} {v:6} {row['reqs']:6} {row['keys']:5}  {','.join(row['families'])}")
print("\nrule × family (requests)")
for r, d in rule_fam.items():
    print(f"  {r}: " + ", ".join(f"{f}={len(s)}" for f, s in sorted(d.items(), key=lambda x: -len(x[1]))))
print("\ntop masked samples per rule")
for r, c in masks.items():
    print(f"  {r}: " + " | ".join(f"{m}×{n}" for m, n in c.most_common(5)))
if per_req:
    s = sorted(per_req.values())
    q = lambda f: s[min(len(s) - 1, int(f * len(s)))]
    print(f"\nhits per request n={len(s)} p50={q(.5)} p90={q(.9)} p99={q(.99)} max={s[-1]}")
if out_json:
    json.dump({"since": since, "lanes": per_lane, "rows": rows,
               "rule_family": {r: {f: len(s) for f, s in d.items()} for r, d in rule_fam.items()},
               "samples": {r: c.most_common(8) for r, c in masks.items()}}, open(out_json, "w"), ensure_ascii=False, indent=1)
