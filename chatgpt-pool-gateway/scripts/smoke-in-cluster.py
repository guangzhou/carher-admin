#!/usr/bin/env python3
"""Smoke T1/T2/T3/T4/T6/T7/T9/T-OPS — gateway in litellm-dev vs mock-chatgpt-upstream.

直接在 198 上跑 (不在容器里)。
依赖 kubectl + python3 (198 系统 python3.10 即可)。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.error

NS = "litellm-dev"
GW_POD_SELECTOR = "-l app=chatgpt-pool-gateway"
MOCK_BASE = "http://mock-chatgpt-upstream.litellm-dev.svc.cluster.local:4101"
GW_BASE = "http://chatgpt-pool-gateway.litellm-dev.svc.cluster.local:4000"
KEY = "sk-pool-internal-dev"

PASS = 0
FAIL = 0
RESULTS: list[tuple[str, bool, str]] = []


def log(m: str) -> None:
    print(f"[smoke {time.strftime('%H:%M:%S')}] {m}", flush=True)


def gw_pod() -> str:
    out = subprocess.check_output(
        ["kubectl", "-n", NS, "get", "pod", *GW_POD_SELECTOR.split(), "-o", "jsonpath={.items[0].metadata.name}"],
        text=True,
    )
    return out.strip()


def in_pod(script: str) -> str:
    """Run python script inside gateway pod, return stdout."""
    pod = gw_pod()
    res = subprocess.run(
        ["kubectl", "-n", NS, "exec", pod, "--", "python", "-c", script],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"in_pod failed: rc={res.returncode}\nstderr={res.stderr}\nstdout={res.stdout}")
    return res.stdout


def http(method: str, url: str, *, headers: dict | None = None, body: dict | None = None, timeout: int = 30) -> tuple[int, str, dict]:
    """Run HTTP call from inside gateway pod (in-cluster DNS)."""
    payload = json.dumps({
        "method": method, "url": url, "headers": headers or {}, "body": body, "timeout": timeout,
    })
    script = """
import json, sys, urllib.request, urllib.error
p = json.loads(sys.stdin.read())
data = json.dumps(p["body"]).encode() if p["body"] is not None else None
req = urllib.request.Request(p["url"], data=data, method=p["method"], headers=p["headers"])
try:
    r = urllib.request.urlopen(req, timeout=p["timeout"])
    body = r.read().decode("utf-8", "replace")
    print(json.dumps({"code": r.status, "body": body, "hdrs": dict(r.headers)}))
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", "replace")
    print(json.dumps({"code": e.code, "body": body, "hdrs": dict(e.headers)}))
except Exception as e:
    print(json.dumps({"code": 0, "body": f"EXC: {type(e).__name__}: {e}", "hdrs": {}}))
"""
    pod = gw_pod()
    res = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", pod, "--", "python", "-c", script],
        input=payload, capture_output=True, text=True, timeout=timeout + 15,
    )
    if res.returncode != 0:
        return 0, f"exec failed: {res.stderr}", {}
    out = json.loads(res.stdout)
    return out["code"], out["body"], out.get("hdrs", {})


def case(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        log(f"  ✅ {name}")
    else:
        FAIL += 1
        log(f"  ❌ {name}: {detail}")
    RESULTS.append((name, ok, detail))


# ---------- bootstrap ----------
log("bootstrap: 拉 mock accounts + 写 auth.json + 注册 gateway")
code, body, _ = http("GET", f"{MOCK_BASE}/_admin/accounts")
if code != 200:
    log(f"FATAL: mock /_admin/accounts {code} {body}"); sys.exit(2)
mock = json.loads(body)
mock_names = list(mock.keys())[:3]
log(f"  mock accts: {mock_names}")

# 把 auth.json 写到 gateway pod /data/auth/<name>/auth.json
pod = gw_pod()
for n in mock_names:
    auth = {
        "tokens": {
            "access_token": mock[n]["access_token"],
            "refresh_token": mock[n]["refresh_token"],
            "expires_at": time.time() + 3600,
            "last_refresh": time.time(),
        },
        "account_id": n,
    }
    script = f"""
import os, json
os.makedirs('/data/auth/{n}', exist_ok=True)
with open('/data/auth/{n}/auth.json', 'w') as f:
    json.dump({json.dumps(auth)}, f)
print('wrote', '/data/auth/{n}/auth.json')
"""
    print(in_pod(script).strip())
    # register
    code, body, _ = http(
        "POST", f"{GW_BASE}/admin/acct/add",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        body={"name": n, "auth_path": f"/data/auth/{n}/auth.json", "priority": 50},
    )
    log(f"  register {n}: {code} {body[:100]}")

# 触发 1 次 probe 让 picker 有数据
for n in mock_names:
    http("POST", f"{GW_BASE}/admin/acct/probe",
         headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
         body={"name": n})

time.sleep(2)
code, body, _ = http("GET", f"{GW_BASE}/admin/acct/list",
                     headers={"Authorization": f"Bearer {KEY}"})
log(f"acct/list ({code}): {body[:400]}")

# ---------- T9: bad bearer ----------
code, body, _ = http("POST", f"{GW_BASE}/v1/chat/completions",
                     headers={"Authorization": "Bearer WRONG", "Content-Type": "application/json"},
                     body={"model": "x", "messages": []})
case("T9 bad-bearer", code in (401, 403), f"code={code} body={body[:80]}")

# ---------- T1: 非流式 ----------
code, body, _ = http("POST", f"{GW_BASE}/v1/chat/completions",
                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                     body={"model": "chatgpt-pool", "messages": [{"role": "user", "content": "hi"}]})
ok = code == 200 and '"choices"' in body
case("T1 non-stream", ok, f"code={code} body={body[:200]}")

# ---------- T2: 流式 ----------
code, body, _ = http("POST", f"{GW_BASE}/v1/chat/completions",
                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                     body={"model": "chatgpt-pool", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
ok = code == 200 and "[DONE]" in body
case("T2 stream", ok, f"code={code} body-len={len(body)} tail={body[-200:]}")

# ---------- T3: affinity (5 turn same conv_id) ----------
conv = f"C-{uuid.uuid4().hex[:8]}"
hits_before, _, _ = http("GET", f"{GW_BASE}/metrics")
prev_hit = 0
for line in hits_before.splitlines() if isinstance(hits_before, str) else []:
    if line.startswith("gateway_affinity_total") and 'result="hit"' in line:
        try: prev_hit = float(line.split()[-1])
        except: pass

for i in range(5):
    http("POST", f"{GW_BASE}/v1/chat/completions",
         headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
         body={"model": "chatgpt-pool", "messages": [{"role": "user", "content": f"t{i}"}],
               "metadata": {"conversation_id": conv}})

_, metrics_after, _ = http("GET", f"{GW_BASE}/metrics")
cur_hit = 0
for line in metrics_after.splitlines():
    if line.startswith("gateway_affinity_total") and 'result="hit"' in line:
        try: cur_hit = float(line.split()[-1])
        except: pass
delta = cur_hit - prev_hit
case("T3 affinity", delta >= 4, f"hit delta={delta} (expect ≥4)")

# ---------- T4: compaction-drop ----------
body4 = {
    "model": "chatgpt-pool",
    "messages": [{"role": "user", "content": "hi"}],
    "input": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "x"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    ],
}
code, body, _ = http("POST", f"{GW_BASE}/v1/chat/completions",
                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                     body=body4)
ok = code == 200 and '"choices"' in body
case("T4 compaction-drop", ok, f"code={code} body={body[:200]}")

# 取 COMPACTION_DROPS metric 看是否+1
_, metrics, _ = http("GET", f"{GW_BASE}/metrics")
drops = 0
for line in metrics.splitlines():
    if line.startswith("gateway_compaction_drops_total ") or line.startswith("gateway_compaction_drops "):
        try: drops = float(line.split()[-1])
        except: pass
log(f"  metric gateway_compaction_drops_total = {drops}")

# ---------- T6: quota pause ----------
victim = mock_names[0]
http("POST", f"{MOCK_BASE}/_admin/quota",
     headers={"Content-Type": "application/json"},
     body={"name": victim, "primary_used": 100})
# 强制 probe
http("POST", f"{GW_BASE}/admin/acct/probe",
     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
     body={"name": victim})
time.sleep(1)
code, body, _ = http("GET", f"{GW_BASE}/admin/acct/list",
                     headers={"Authorization": f"Bearer {KEY}"})
state = None
try:
    accts = json.loads(body).get("accounts", [])
    state = next((a["state"] for a in accts if a["name"] == victim), None)
except Exception:
    pass
case("T6 quota pause", state in ("cooling", "offline"), f"state={state} body={body[:200]}")
# reset
http("POST", f"{MOCK_BASE}/_admin/reset", headers={"Content-Type": "application/json"}, body={})

# ---------- T7: fail-fast (mock /_admin/fault=500) ----------
# 不依赖 affinity 黏定 — 直接给所有 mock acct 都设 fault, 任何 pick 都撞 500
sticky_conv = f"T7-{uuid.uuid4().hex[:8]}"
# 设 fault on ALL mock accts
for n in mock_names:
    http("POST", f"{MOCK_BASE}/_admin/fault",
         headers={"Content-Type": "application/json"},
         body={"name": n, "fault": "500"})
_, m_before, _ = http("GET", f"{GW_BASE}/metrics")
before = 0
for line in m_before.splitlines():
    if line.startswith("gateway_first_byte_5xx_total ") or line.startswith("gateway_first_byte_5xx "):
        try: before = float(line.split()[-1])
        except: pass
# 发 8 次, 任何 acct 都会撞 fault
for _ in range(8):
    http("POST", f"{GW_BASE}/v1/chat/completions",
         headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
         body={"model": "chatgpt-pool", "messages": [{"role": "user", "content": "t7"}],
               "metadata": {"conversation_id": sticky_conv}})
_, m_after, _ = http("GET", f"{GW_BASE}/metrics")
after = 0
for line in m_after.splitlines():
    if line.startswith("gateway_first_byte_5xx_total ") or line.startswith("gateway_first_byte_5xx "):
        try: after = float(line.split()[-1])
        except: pass
case("T7 fail-fast", after > before, f"first_byte_5xx before={before} after={after}")
http("POST", f"{MOCK_BASE}/_admin/reset", headers={"Content-Type": "application/json"}, body={})

# ---------- T-OPS ----------
new_name = f"acct-ops-{int(time.time())}"
auth = {"tokens": {"access_token": "sk-ops", "refresh_token": "sk-ops-rt",
                   "expires_at": time.time() + 3600, "last_refresh": time.time()},
        "account_id": new_name}
in_pod(f"""
import os, json
os.makedirs('/data/auth/{new_name}', exist_ok=True)
with open('/data/auth/{new_name}/auth.json', 'w') as f:
    json.dump({json.dumps(auth)}, f)
""")
code, body, _ = http("POST", f"{GW_BASE}/admin/acct/add",
                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                     body={"name": new_name, "auth_path": f"/data/auth/{new_name}/auth.json", "priority": 99})
case("T-OPS 2-step add", code == 200 and '"ok":true' in body.replace(" ", ""), f"code={code} body={body[:200]}")

# ---------- summary ----------
print()
log(f"RESULT: PASS={PASS} FAIL={FAIL} TOTAL={PASS + FAIL}")
for n, ok, d in RESULTS:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}  {d if not ok else ''}")
sys.exit(0 if FAIL == 0 else 1)
