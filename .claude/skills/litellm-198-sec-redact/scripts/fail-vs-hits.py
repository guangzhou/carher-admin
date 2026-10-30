#!/usr/bin/env python3
"""用法: fail-vs-hits.py <切换时刻 RFC3339 UTC>
切 replace 后：失败请求里有几条被 sec_redact 改过（按 litellm_call_id 对 pod 日志 req=）。只读。
判据：若补丁导致失败，失败请求应集中在「有命中」的请求里；没有交集 ⇒ 失败与补丁无关。"""
import re
import subprocess
import sys
E = {"KUBECONFIG": "/etc/rancher/k3s/k3s.yaml", "PATH": "/usr/local/bin:/usr/bin:/bin"}
S = sys.argv[1] if len(sys.argv) > 1 else "2026-10-29T06:39:23Z"  # 切换时刻（UTC）
# 插件日志的 req= 是 litellm_call_id；SpendLogs.request_id 常是上游回包 id（msg_…）⇒ 必须按 metadata 里的 call id 对
SQL = f"""select coalesce(metadata->>'litellm_call_id', request_id), split_part(coalesce(metadata->>'user_api_key_alias','-'),'-',1), status
 from "LiteLLM_SpendLogs" where "startTime" >= '{S}' and call_type not like '/%';"""
r = subprocess.run(["kubectl", "-n", "litellm-product", "exec", "-i", "litellm-db-0", "--", "psql", "-U", "litellm",
                    "-d", "litellm", "-At", "-F", "|"], input=SQL, capture_output=True, text=True, env=E)
rows = [l.split("|") for l in r.stdout.splitlines() if l.count("|") == 2]
hit = set()
for p in subprocess.run(["kubectl", "-n", "litellm-product", "get", "po", "-l", "app=litellm-proxy", "-o", "name"],
                        capture_output=True, text=True, env=E).stdout.split():
    for l in subprocess.run(["kubectl", "-n", "litellm-product", "logs", p, "-c", "litellm", f"--since-time={S}"],
                            capture_output=True, text=True, env=E).stdout.splitlines():
        m = re.search(r"sec_redact: hit.* req=(\S+)", l)
        if m:
            hit.add(m.group(1))
tot = len(rows)
fails = [x for x in rows if x[2] == "failure"]
hit_rows = [x for x in rows if x[0] in hit]
hit_fail = [x for x in hit_rows if x[2] == "failure"]
nohit_rows = [x for x in rows if x[0] not in hit]
nohit_fail = [x for x in nohit_rows if x[2] == "failure"]
pct = lambda a, b: round(100 * len(a) / max(len(b), 1), 2)
print(f"since {S}: requests={tot} failures={len(fails)} ({pct(fails, rows)}%)")
print(f"  with sec_redact hit: {len(hit_rows)} requests, {len(hit_fail)} failed ({pct(hit_fail, hit_rows)}%)")
print(f"  without hit        : {len(nohit_rows)} requests, {len(nohit_fail)} failed ({pct(nohit_fail, nohit_rows)}%)")
real = [x for x in rows if x[1] != "zz"]
realf = [x for x in real if x[2] == "failure"]
print(f"  excluding zz-* probe keys: {len(real)} requests, {len(realf)} failed ({pct(realf, real)}%)")
