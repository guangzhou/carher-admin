#!/usr/bin/env python3
"""Verify one or more acct-N tokens on 188 against /codex/usage; print error body on failure."""
import json, sys, base64, time, urllib.request, urllib.error
for n in sys.argv[1:]:
    p = f"/Data/chatgpt-auth/acct-{n}/auth.json"
    try:
        a = json.load(open(p)); tok = a["access_token"]; aid = a.get("account_id", "")
        raw = tok.split(".")[1]; raw += "=" * (-len(raw) % 4)
        exp = json.loads(base64.urlsafe_b64decode(raw)).get("exp", 0)
    except Exception as e:
        print(f"acct-{n}: load-err {e}"); continue
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/usage",
        headers={"Authorization": f"Bearer {tok}", "chatgpt-account-id": aid,
                 "Originator": "codex_cli_rs", "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)"})
    try:
        u = json.loads(urllib.request.urlopen(req, timeout=15).read())
        print(f"acct-{n}: OK plan={u.get('plan_type')} aid={aid[:12]} exp={time.strftime('%m-%d %H:%M', time.localtime(exp))}")
    except urllib.error.HTTPError as e:
        print(f"acct-{n}: HTTP{e.code} aid={aid[:12]} body={e.read().decode(errors='replace')[:160]}")
    except Exception as e:
        print(f"acct-{n}: ERR {e}")
