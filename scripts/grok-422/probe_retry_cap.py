#!/usr/bin/env python3
"""阳性对照：一条 x.ai 必回 422 的请求，经 litellm 打 sa-grok-4.7，**不关重试**（用线上重试策略）。
422 的触发物用 input 里一个未知 item type（agent_message 已被 callback 摊平，所以换一个 callback 不碰的类型）。
打 1 次，看 sub2api 那边这个 pod 收到几条 failover 链：改前 6，改后应为 2。"""
import json, os, time, urllib.request, urllib.error
B = "http://127.0.0.1:4000"; K = os.environ["LITELLM_MASTER_KEY"]
body = {"model": "sa-grok-4.7", "stream": False, "max_output_tokens": 16, "fallbacks": [],
        "input": [{"type": "probe_unknown_item_type", "content": [{"type": "input_text", "text": "x"}]},
                  {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Reply ok"}]}]}
t0 = time.time()
req = urllib.request.Request(B + "/v1/responses", data=json.dumps(body).encode(),
                             headers={"Authorization": "Bearer " + K, "Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=180) as r: print("status", r.status)
except urllib.error.HTTPError as e:
    print("status", e.code)
print(f"elapsed_s={time.time() - t0:.1f}")
