#!/usr/bin/env python3
"""灰度观察：槽位实例（aws-* + 旧 kiro-slots-* 隐藏别名）vs 老实例 最近 N 分钟成功率 / 空回复 / 耗时 / 兜底；
新实例每槽 429、被限、排队超时、余量。只读。
10-27 起组改名 aws-*，旧名 kiro-* 是隐藏别名，SpendLogs 的 model_group 记客户端点的名字 ⇒ 车道按 api_base 判，不按组名。
用法：python3 scripts/kiro-slots-watch.py [--minutes 30]"""
import argparse, json, subprocess, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "cli-anything-litellm"))
from cli_anything.litellm.utils import remote198

ap = argparse.ArgumentParser(); ap.add_argument("--minutes", type=int, default=30); a = ap.parse_args()
q = f'''select case when api_base like 'http://10.43.109.5:8990%' or api_base like '%//kiro-rs.kiro-rs%' then 'old' else 'slots' end lane,
  count(*) n, count(*) filter (where status<>'success') fail,
  count(*) filter (where status='success' and completion_tokens<=1) empty_ish,
  round(percentile_cont(0.5) within group (order by extract(epoch from "endTime"-"startTime"))::numeric,1) p50,
  round(percentile_cont(0.9) within group (order by extract(epoch from "endTime"-"startTime"))::numeric,1) p90,
  count(distinct metadata->>'user_api_key_alias') keys
from "LiteLLM_SpendLogs" where "startTime" > now() - interval '{a.minutes} minutes'
  and (api_base like '%kiro-rs%' or api_base like 'http://10.43.109.5:8990%' or api_base like 'http://10.43.252.245:8990%')
group by 1 order by 1'''
print(f"== LiteLLM 最近 {a.minutes} 分钟")
for r in remote198.psql(q, timeout_ms=120000):
    n = int(r["n"]); print(f"  {r['lane']:5} 请求 {n:>5}  失败 {r['fail']:>4} ({100*int(r['fail'])/max(n,1):.1f}%)  ≤1token {r['empty_ish']:>4}  p50 {r['p50']}s p90 {r['p90']}s  key {r['keys']}")
cmd = ("P=$(sudo -n kubectl -n kiro-rs get pod -l app=kiro-rs-slots -o name | head -1); "
       "K=$(sudo -n kubectl -n kiro-rs exec $P -- cat /app/config/config.json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)[\"adminApiKey\"])'); "
       "curl -s -H \"x-api-key: $K\" http://127.0.0.1:30891/api/admin/slots")
out = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "cltx@10.68.13.198", cmd], capture_output=True, text=True, timeout=60).stdout
d = json.loads(out)
print(f"== 新实例 模式={'槽位' if d['enabled'] else '未启用'} 冷备={d['spare']} 上限参数={d['limits']}")
for s in d["slots"]:
    st = s["stats"]
    print(f"  {s['name']:20} #{s['accountId']}  上限{s['limit']} 在途{s['inFlight']} 排队{s['queued']} 人{s['users']} | 发{st['sent']} 首发429 {st['first429']} 重试429 {st['retry429']} 用尽{st['exhausted']} 被限{st['restricted']} 排队超时{st['queueTimeouts']}")
