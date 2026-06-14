#!/usr/bin/env python3
"""Audit all /Data/chatgpt-auth/acct-*/auth.json on 188: email, token exp, live /codex/usage."""
import json, base64, glob, os, time, urllib.request, urllib.error

def email_of(acct):
    cf = f"/Data/chatgpt-auth/{acct}/.creds"
    if os.path.exists(cf):
        for ln in open(cf):
            if ln.startswith("email="):
                return ln.split("=", 1)[1].strip().strip("'\"")
    return "?"

def usage(tok, aid):
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/usage",
        headers={"Authorization": f"Bearer {tok}", "chatgpt-account-id": aid,
                 "Originator": "codex_cli_rs", "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)"})
    try:
        u = json.loads(urllib.request.urlopen(req, timeout=12).read())
        return "OK", u.get("plan_type", "?")
    except urllib.error.HTTPError as e:
        return f"HTTP{e.code}", ""
    except Exception as e:
        return f"ERR:{str(e)[:30]}", ""

def keynum(p):
    s = p.rstrip("/").split("-")[-1]
    return int(s) if s.isdigit() else 999

dirs = sorted(glob.glob("/Data/chatgpt-auth/acct-*/"), key=keynum)
hdr = "%-9s%-34s%-22s%-12s%s" % ("acct", "email", "token_exp", "usage", "plan")
print(hdr)
print("-" * 90)
now = time.time()
for d in dirs:
    acct = os.path.basename(d.rstrip("/"))
    aj = os.path.join(d, "auth.json")
    em = email_of(acct)
    if not os.path.exists(aj):
        print("%-9s%-34s%-22s%-12s" % (acct, em, "<no auth.json>", "-"))
        continue
    try:
        a = json.load(open(aj)); tok = a["access_token"]; aid = a.get("account_id", "")
        raw = tok.split(".")[1]; raw += "=" * (-len(raw) % 4)
        cl = json.loads(base64.urlsafe_b64decode(raw)); exp = cl.get("exp", 0)
        expd = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
        expmark = expd + (" EXP!" if exp < now else "")
    except Exception as e:
        print("%-9s%-34s%-22s%s" % (acct, em, "parse-err", str(e)[:20]))
        continue
    st, plan = usage(tok, aid)
    print("%-9s%-34s%-22s%-12s%s" % (acct, em, expmark, st, plan))
