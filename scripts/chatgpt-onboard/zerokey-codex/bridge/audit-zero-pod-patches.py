#!/usr/bin/env python3
"""Audit every zero-* deploy: does it install the latest patch, and does the file
in the running pod match the ConfigMap it mounts?

RUN THIS ON 198 (needs kubectl + sudo):
    scp audit-zero-pod-patches.py cltx@10.68.13.198:/tmp/ && ssh cltx@10.68.13.198 \
        'cd /tmp && python3 audit-zero-pod-patches.py'

Why it exists: on 2026-07-27, 21 pods were silently missing three `cp` lines in
their startup command, so they served the UNPATCHED responses.js and 503'd every
request carrying tools[]. A behavioural survey of pods 81-131 found and fixed
those, but MISSED zero-28/50/52/129 simply because they were outside the range
that was probed -- and zero-129 was in the bridge's active pool. Range-based
spot checks are not enough; enumerate every deploy and compare md5s.

Fix a flagged deploy with patch-zero-pod-startup.py <name> --apply.

Three independent checks, because any one alone can lie:
  1. STARTUP  -- does the container args copy each /patch file?
  2. MOUNTED  -- which CM does it mount, and what's the md5 of that CM's key?
  3. RUNNING  -- md5 of the file actually on disk in the live pod
A pod can have the cp line but mount a STALE CM (zero-87 mounts its own
zk-image-patch-stream87), so 1 and 2 must both be checked.
"""
import json, subprocess, hashlib, sys
from concurrent.futures import ThreadPoolExecutor

PW = "Hn8#mKLp3QxZ\n"
KEYS = ["responses.js", "web-tools.js", "raw.js", "zerokey-serve-codex.js",
        "images.js", "api.js"]

def kc(*a, timeout=120):
    return subprocess.run(["sudo", "kubectl", "-n", "litellm-product"] + list(a),
                          capture_output=True, text=True, input=PW, timeout=timeout)

def md5(s):
    return hashlib.md5(s.encode()).hexdigest()[:10] if s else "-"

# --- gather all CM contents once ---
cms = {}
r = kc("get", "cm", "-o", "json")
for it in json.loads(r.stdout)["items"]:
    n = it["metadata"]["name"]
    if "image-patch" in n:
        cms[n] = it.get("data") or {}

# --- gather deploys ---
r = kc("get", "deploy", "-o", "json")
deploys = {}
for it in json.loads(r.stdout)["items"]:
    n = it["metadata"]["name"]
    if not n.startswith("zero-"):
        continue
    sp = it["spec"]["template"]["spec"]
    c = sp["containers"][0]
    args = c.get("args") or []
    script = args[1] if (len(args) > 1 and args[0] == "-c") else (args[0] if args else "")
    cm = None
    for v in sp.get("volumes", []):
        if (v.get("configMap") or {}).get("name"):
            cm = v["configMap"]["name"]
    deploys[n] = {"script": script, "cm": cm}

# --- reference = the newest content across all patch CMs, per key ---
ref = {}
for k in KEYS:
    best = None
    for cm, d in cms.items():
        if k in d and (best is None or len(d[k]) > len(best)):
            best = d[k]
    ref[k] = best

def check(name):
    d = deploys[name]
    cm_data = cms.get(d["cm"], {})
    row = {"deploy": name, "cm": d["cm"], "missing_cp": [], "stale_cm": [], "run": {}}
    for k in KEYS:
        if ("cp /patch/%s" % k) not in d["script"]:
            row["missing_cp"].append(k)
        elif k in cm_data and ref.get(k) and cm_data[k] != ref[k]:
            row["stale_cm"].append(k)
    # running file md5 for responses.js
    r = kc("get", "pods", "-l", "app=%s" % name,
           "--field-selector=status.phase=Running",
           "-o", "jsonpath={.items[0].metadata.name}")
    pod = (r.stdout or "").strip()
    if pod:
        r2 = kc("exec", pod, "--", "sh", "-c",
                "md5sum /app/routes/responses.js /app/routes/web-tools.js 2>/dev/null")
        for ln in (r2.stdout or "").split("\n"):
            p = ln.split()
            if len(p) == 2:
                row["run"][p[1].split("/")[-1]] = p[0][:10]
    row["pod"] = pod
    return row

names = sorted(deploys, key=lambda x: int(x.split("-")[1]))
rows = []
for i in range(0, len(names), 6):
    with ThreadPoolExecutor(max_workers=6) as ex:
        rows += list(ex.map(check, names[i:i + 6]))

print("REFERENCE md5: " + " ".join("%s=%s" % (k, md5(ref[k])) for k in ["responses.js", "web-tools.js"]))
print()
bad = []
for r in rows:
    want = md5(ref["responses.js"])
    got = r["run"].get("responses.js", "-")
    ok = (not r["missing_cp"]) and (not r["stale_cm"]) and got == want
    if not ok:
        bad.append(r)
    print("  %-10s cm=%-26s resp_running=%s %s%s%s" % (
        r["deploy"], r["cm"] or "-", got,
        "OK" if ok else "**MISMATCH**",
        (" missing_cp=%s" % r["missing_cp"]) if r["missing_cp"] else "",
        (" stale_cm=%s" % r["stale_cm"]) if r["stale_cm"] else ""))
print()
print("TOTAL %d deploys, %d fully up-to-date, %d need attention" % (
    len(rows), len(rows) - len(bad), len(bad)))
if bad:
    print("NEEDS FIX: " + ",".join(b["deploy"] for b in bad))
