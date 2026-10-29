#!/usr/bin/env python3
"""budget_notice ② 提醒的「客户端真实线型」探针：/v1/messages 流式 + /v1/responses 流式各真打一发。

用法（198 上 root 跑，需要 kubectl）：
    sudo python3 wire-probe.py                                  # 默认 prod：svc litellm-proxy
    sudo SVC=litellm-proxy-gray-nodeport python3 wire-probe.py  # gray 零流量车道

为什么冒烟之外还要它（2026-10-20）：`litellm-198-budget-notice-smoke.sh` 只走
/v1/chat/completions，13/13 全绿时 /v1/messages 上的提醒**一个字都没注入** ——
claude-opus-4.8 的桥接流不发 content_block_stop，注入状态机等不到注入点。
只有按 Claude Code（messages）和 Cursor/Codex（responses）的真实线型打才看得见。

做什么：每种线型临时建一把 cursor-wireprobe-* key（max_budget $5、spend 置 $4.2 = 84%
⇒ 必跨 80% 档），打一发流式，断言流里有「额度提醒」+ 额度表 + 提示语，且流正常收尾
（messages 有 message_stop；responses 的 response.completed 全文里也带提醒）。
每把 key 用完即删。花费：两次极短请求（≈$0.001）。退出码 0 = 两种都 PASS。
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "litellm-product"
SVC = os.environ.get("SVC", "litellm-proxy")
PREMIUM_MODEL = "claude-opus-4.8"        # messages 线型用（桥接流，曾不发 content_block_stop）
BILLABLE_MODEL = "wangsu7-gpt-5.6-sol"   # responses 线型用


def kubectl(*args):
    return subprocess.run(["kubectl", "-n", NS, *args],
                          capture_output=True, text=True).stdout.strip()


BASE = "http://%s:4000" % kubectl("get", "svc", SVC, "-o", "jsonpath={.spec.clusterIP}")
MK = base64.b64decode(kubectl("get", "secret", "litellm-secrets-r2", "-o",
                              "jsonpath={.data.LITELLM_MASTER_KEY}")).decode()


def call(path, payload, key, extra=None):
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=180)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def probe(wire):
    ts = str(int(time.time()))
    st, b = call("/key/generate", {"key_alias": f"cursor-wireprobe-{wire}-{ts}",
                                   "max_budget": 5.0, "budget_duration": "1d",
                                   "models": [PREMIUM_MODEL, BILLABLE_MODEL]}, MK)
    key = json.loads(b)["key"]
    try:
        call("/key/update", {"key": key, "spend": 4.2}, MK)
        time.sleep(3)
        if wire == "messages":
            st, b = call("/v1/messages", {"model": PREMIUM_MODEL, "max_tokens": 64, "stream": True,
                                          "messages": [{"role": "user", "content": "只回一个字:好"}]},
                         key, {"anthropic-version": "2023-06-01"})
            text = "".join(json.loads(ln[5:]).get("delta", {}).get("text", "")
                           for ln in b.splitlines()
                           if ln.startswith("data:") and '"text_delta"' in ln)
            closed = "message_stop" in b
        else:
            st, b = call("/v1/responses", {"model": BILLABLE_MODEL, "input": "只回一个字:好",
                                           "stream": True}, key)
            evs = [json.loads(ln[5:]) for ln in b.splitlines()
                   if ln.startswith("data:") and ln[5:].strip().startswith("{")]
            text = "".join(e.get("delta", "") for e in evs
                           if e.get("type") == "response.output_text.delta")
            done = [e for e in evs if e.get("type") == "response.completed"]
            full = "".join(c.get("text", "")
                           for it in (done[0]["response"].get("output", []) if done else [])
                           for c in (it.get("content") or []) if c.get("type") == "output_text")
            closed = bool(done) and "额度提醒" in full   # done 事件里的全文也要同步补上
        ok = (st == 200 and "额度提醒" in text and "| ⚠️ 今日总额度" in text
              and "查余额或者cye" in text and closed)
        print(("PASS " if ok else "FAIL ") + wire, st, "len=%d" % len(text), "closed=%s" % closed)
        print(text[-700:] if ok else b[-1500:])
        return ok
    finally:
        call("/key/delete", {"keys": [key]}, MK)


if __name__ == "__main__":
    print(f"target svc={SVC} base={BASE}")
    results = [probe("messages"), probe("responses")]
    sys.exit(0 if all(results) else 1)
