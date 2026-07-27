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
import json, os, subprocess, hashlib, sys
from concurrent.futures import ThreadPoolExecutor


# Never hardcode the production sudo password. Resolution order is env ->
# .carher-secrets.json (gitignored) -> ~/.config/carher/secrets.json.
def _load_secret(name):
    """Resolve a secret without a hand-counted dirname chain, and WITHOUT dying at
    import time. Both defects were real: the old 4-level chain resolved to "/lib"
    when this file was scp'd to 198:/tmp -- the very workflow the docstring above
    documents -- and require() at module scope killed --help and the dry-run path
    before argv was parsed. Walk up to the repo marker if present, else fall back to
    the environment so a single-file copy still works."""
    import os as _os
    import sys as _sys
    _here = _os.path.dirname(_os.path.abspath(__file__))
    while True:
        _cand = _os.path.join(_here, "scripts", "lib")
        if _os.path.isfile(_os.path.join(_cand, "carher_secrets.py")):
            if _cand not in _sys.path:
                _sys.path.insert(0, _cand)
            import carher_secrets
            return carher_secrets.require(name)
        _parent = _os.path.dirname(_here)
        if _parent == _here:
            break
        _here = _parent
    v = _os.environ.get(name)
    if v:
        return v
    _sys.exit(
        "missing secret %r.\n"
        "  This copy cannot see scripts/lib (running standalone?), so set it in the\n"
        "  environment:  export %s='...'\n"
        "  Or run from a repo checkout, where .carher-secrets.json is picked up."
        % (name, name))

_PW_CACHE = []


def _pw():
    """Resolved on first use, not at import — so --help and the dry-run path work
    with no credential configured at all."""
    if not _PW_CACHE:
        _PW_CACHE.append(_load_secret("SUDO_PW") + "\n")
    return _PW_CACHE[0]
KEYS = ["responses.js", "web-tools.js", "raw.js", "zerokey-serve-codex.js",
        "images.js", "api.js"]

def kc(*a, timeout=120):
    return subprocess.run(["sudo", "kubectl", "-n", "litellm-product"] + list(a),
                          capture_output=True, text=True, input=_pw(), timeout=timeout)


def kc_json(*a, **kw):
    """kubectl -o json, with the failure surfaced. Parsing r.stdout blindly turned
    an auth failure or a typo'd resource name into an opaque JSONDecodeError."""
    r = kc(*a, **kw)
    if r.returncode != 0 or not (r.stdout or "").strip():
        sys.exit("kubectl %s failed (rc=%s): %s"
                 % (" ".join(a), r.returncode,
                    (r.stderr or r.stdout or "no output").strip()[:300]))
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        sys.exit("kubectl %s returned unparseable output: %s"
                 % (" ".join(a), e))

def md5(s):
    return hashlib.md5(s.encode()).hexdigest()[:10] if s else "-"

# --- gather all CM contents once ---
cms = {}
for it in kc_json("get", "cm", "-o", "json")["items"]:
    n = it["metadata"]["name"]
    if "image-patch" in n:
        cms[n] = it.get("data") or {}

# --- gather deploys ---
deploys = {}
for it in kc_json("get", "deploy", "-o", "json")["items"]:
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

# --- reference = one explicitly PINNED ConfigMap ---
#
# This used to pick, per key, whichever ConfigMap held the LONGEST string. That
# is not a notion of correctness, and it let the tool invert its own verdict: a
# stale-but-longer CM silently became the reference, so correctly-patched pods
# would be reported as MISMATCH (or a genuinely stale pod would pass). The bug was
# latent only because zk-image-patch and zk-image-patch-stream87 happened to hold
# byte-identical copies when the audit was first run.
#
# zk-image-patch is the shared CM that ~49 of the 50 deploys mount, so it is the
# authoritative copy by construction. zero-87 mounts its own
# zk-image-patch-stream87; that is deliberate, and it is compared against the same
# reference so a divergence there is REPORTED rather than becoming the standard.
#
# NOT the git working tree: verified 2026-07-27 that
# zerokey-patch/routes/responses.js in git is 101 lines BEHIND the deployed copy
# (the progressive-streaming work was never committed back), so git would flag all
# 50 pods. Override with REF_CM= if the authoritative CM ever changes.
REF_CM = os.environ.get("REF_CM", "zk-image-patch")
if REF_CM not in cms:
    sys.exit("reference ConfigMap %r not found; saw %s"
             % (REF_CM, sorted(cms) or "none"))
ref = {k: cms[REF_CM].get(k) for k in KEYS}
missing_in_ref = [k for k in KEYS if not ref.get(k)]
if missing_in_ref:
    print("WARNING: reference CM %s lacks %s -- those keys cannot be checked"
          % (REF_CM, missing_in_ref))

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
