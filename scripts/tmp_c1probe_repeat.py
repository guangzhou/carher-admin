#!/usr/bin/env python3
"""Repeat the carher-1-shaped probe through LiteLLM several times per model, so a
one-off 500 can be told apart from a persistent one. Judges landing by
x-litellm-model-id; on a non-200 also prints litellm's own error detail.
"""
import json, os, sys, urllib.request, urllib.error

BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
TARGET = "carher-1"
MODELS = ["claude-opus-5", "claude-fable-5.1"]
ROUNDS = 3


def call(path, body=None, method="GET", key=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
        headers={"Authorization": "Bearer " + (key or MK),
                 "Content-Type": "application/json"}, method=method)
    try:
        x = urllib.request.urlopen(r, timeout=timeout)
        return x.status, dict(x.headers), x.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")


src = None
for page in range(1, 60):
    st, _, raw = call(f"/key/list?page={page}&size=100&return_full_object=true")
    rows = json.loads(raw).get("keys") or []
    if not rows:
        break
    for k in rows:
        if k.get("key_alias") == TARGET:
            src = k
            break
    if src:
        break
if not src:
    sys.exit(f"FATAL: {TARGET} not found")

st, _, raw = call("/key/generate", {
    "key_alias": "tmp-probe-carher1-9router-rep",
    "aliases": src.get("aliases") or {},
    "models": src.get("models") or [],
    "duration": "20m", "max_budget": 1.0}, method="POST")
if st != 200:
    sys.exit(f"FATAL: /key/generate {st}: {raw[:300]}")
probe = json.loads(raw)["key"]

tally = {}
try:
    for m in MODELS:
        for i in range(1, ROUNDS + 1):
            st, hdr, raw = call("/v1/chat/completions", {
                "model": m,
                "messages": [{"role": "user", "content": "Reply with exactly: PROBE-OK-9ROUTER"}],
                "max_tokens": 32}, method="POST", key=probe, timeout=300)
            mid = hdr.get("x-litellm-model-id") or "-"
            if st == 200:
                try:
                    txt = json.loads(raw)["choices"][0]["message"]["content"]
                except Exception:
                    txt = raw[:120]
            else:
                txt = raw[:300]
            tally.setdefault(m, []).append(st)
            print(f"[{m} #{i}] HTTP {st}  model-id={mid}  {json.dumps(txt)[:220]}")
finally:
    call("/key/delete", {"keys": [probe]}, method="POST")

print("\n=== summary ===")
for m, sts in tally.items():
    ok = sum(1 for s in sts if s == 200)
    print(f"  {m}: {ok}/{len(sts)} green  statuses={sts}")
