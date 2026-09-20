#!/usr/bin/env python3
"""Pod-side probe: clone carher-1's alias+allowlist shape into a throwaway key,
send REAL requests for claude-fable-5.1 / claude-opus-5, and judge the landing by
the x-litellm-model-id response header (the body's `model` field only echoes the
request name and cannot testify about landing).

Runs inside the litellm-proxy container. Deletes its own probe key in a finally
block so a failed request cannot leave a live key behind.
"""
import json, os, sys, urllib.request, urllib.error

BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
TARGET = "carher-1"
MODELS = ["claude-fable-5.1", "claude-opus-5"]


def call(path, body=None, method="GET", key=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data,
        headers={"Authorization": "Bearer " + (key or MK),
                 "Content-Type": "application/json"},
        method=method)
    try:
        x = urllib.request.urlopen(r, timeout=timeout)
        return x.status, dict(x.headers), x.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")


def find_target():
    for page in range(1, 60):
        st, _, raw = call(f"/key/list?page={page}&size=100&return_full_object=true")
        rows = (json.loads(raw).get("keys") or [])
        if not rows:
            return None
        for k in rows:
            if k.get("key_alias") == TARGET:
                return k
    return None


src = find_target()
if not src:
    sys.exit(f"FATAL: key_alias={TARGET} not found")

aliases = src.get("aliases") or {}
allow = src.get("models") or []
print(f"[shape] {TARGET}: {len(aliases)} aliases, {len(allow)} models")
for m in MODELS:
    print(f"[shape]   alias {m} -> {aliases.get(m)!r}")

st, _, raw = call("/key/generate", {
    "key_alias": "tmp-probe-carher1-9router",
    "aliases": aliases,
    "models": allow,
    "duration": "20m",
    "max_budget": 1.0,
}, method="POST")
if st != 200:
    sys.exit(f"FATAL: /key/generate {st}: {raw[:400]}")
probe = json.loads(raw)["key"]
print(f"[probe] created throwaway key ...{probe[-6:]}")

results = {}
try:
    for m in MODELS:
        st, hdr, raw = call("/v1/chat/completions", {
            "model": m,
            "messages": [{"role": "user",
                          "content": "Reply with exactly: PROBE-OK-9ROUTER"}],
            "max_tokens": 32,
        }, method="POST", key=probe, timeout=300)
        landing = hdr.get("x-litellm-model-id") or hdr.get("X-Litellm-Model-Id")
        txt = ""
        if st == 200:
            try:
                txt = json.loads(raw)["choices"][0]["message"]["content"]
            except Exception:
                txt = raw[:200]
        results[m] = {"status": st, "model_id": landing,
                      "text": (txt or raw)[:200].strip()}
        print(f"[req] {m}: HTTP {st}  x-litellm-model-id={landing}  "
              f"reply={results[m]['text']!r}")
finally:
    st, _, raw = call("/key/delete", {"keys": [probe]}, method="POST")
    print(f"[probe] delete throwaway key: HTTP {st} {raw[:120]}")

print("__RESULT__" + json.dumps(results))
