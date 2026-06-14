#!/usr/bin/env python3
"""Active-test each litellm-chatgpt-N container on 188 by POSTing a tiny chat
completion to host port 4000+N. Ground-truth health (token actually usable),
unlike /codex/usage which can false-negative on un-refreshed tokens.
Prints: acct  email  RESULT
"""
import json, os, subprocess, urllib.request, urllib.error

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()

def email_of(acct):
    cf = f"/Data/chatgpt-auth/{acct}/.creds"
    if os.path.exists(cf):
        for ln in open(cf):
            if ln.startswith("email="):
                return ln.split("=", 1)[1].strip().strip("'\"")
    return "?"

# discover running litellm-chatgpt-N containers
names = sh("docker ps --format '{{.Names}}' | grep -E '^litellm-chatgpt-[0-9]+$' | sort -t- -k3 -n").splitlines()
print("%-9s %-34s %s" % ("acct", "email", "result"))
print("-" * 80)
for name in names:
    n = name.rsplit("-", 1)[-1]
    acct = f"acct-{n}"
    port = 4000 + int(n)
    em = email_of(acct)
    mk = sh(f"docker exec {name} printenv LITELLM_MASTER_KEY 2>/dev/null")
    if not mk:
        print("%-9s %-34s %s" % (acct, em, "NO-MASTER-KEY")); continue
    body = json.dumps({"model": "chatgpt-gpt-5.5",
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 5}).encode()
    req = urllib.request.Request(f"http://localhost:{port}/v1/chat/completions",
                                 data=body,
                                 headers={"Authorization": "Bearer " + mk,
                                          "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        json.loads(r.read())
        print("%-9s %-34s %s" % (acct, em, "✅ 200 OK"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        tag = "invalidated" if "invalidat" in raw else ("expired" if "expired" in raw else f"HTTP{e.code}")
        print("%-9s %-34s ❌ %s" % (acct, em, tag))
    except Exception as e:
        print("%-9s %-34s ❌ %s" % (acct, em, str(e)[:40]))
