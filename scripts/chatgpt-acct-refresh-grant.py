#!/usr/bin/env python3
"""
chatgpt-acct-refresh-grant.py — 零-chromium 刷新 ChatGPT acct 凭据（refresh_token grant）。

用途：acct 报 401 / token_expired 时的**第一枪**。几十秒/号，成功就省掉整条 EIP
patchright OAuth 链（见 memory project_aliyun_acct_122_125_reauth_free_2026_09_08）。
HTTP 200 = 账号活着；4xx invalid_grant = refresh_token 已废，才需要重烧 OAuth。

必须在 **CF-clean 出口** 上跑（阿里云新加坡 EIP 节点，jms 资产 `dify` = 172.16.16.122
/ EIP 47.84.85.100）。普通 pod 走共享 NAT 47.84.112.136（生产 codex 出口），别污染。

用法：
    python3 chatgpt-acct-refresh-grant.py <auth.json> [<auth.json> ...]

每个文件原地 merge 更新（access_token/refresh_token/id_token/expires_at），
`account_id` 保留（refresh 响应不返回它）。**RT 会轮转，必须写回，否则等于烧掉凭据。**
原文件备份到 `<file>.bak-<epoch>`。

输出每行：`<file> <HTTP码> <email> plan=<..> until=<..> acct=<..> rt_rotated=<yes|no>`
"""
import json
import os
import sys
import time
import base64
import urllib.request
import urllib.error

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
AUTH_CLAIM = "https://api.openai.com/auth"


def jwt_claims(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def refresh(path):
    with open(path) as fh:
        cur = json.load(fh)
    rt = cur.get("refresh_token")
    if not rt:
        return f"{path} SKIP no refresh_token"

    body = json.dumps({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "scope": "openid profile email",
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "codex_cli_rs/0.30.0 (Linux; x86_64)"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, raw = r.getcode(), r.read().decode()
    except urllib.error.HTTPError as e:
        return f"{path} HTTP {e.code} {e.read().decode()[:200]}"
    except Exception as e:
        return f"{path} ERR {e!r}"

    new = json.loads(raw)
    merged = dict(cur)
    for k in ("access_token", "refresh_token", "id_token"):
        if new.get(k):
            merged[k] = new[k]
    if new.get("expires_in"):
        merged["expires_at"] = int(time.time()) + int(new["expires_in"])

    os.replace(path, f"{path}.bak-{int(time.time())}")
    with open(path, "w") as fh:
        json.dump(merged, fh, indent=2)

    c = jwt_claims(merged.get("id_token", ""))
    a = c.get(AUTH_CLAIM, {})
    rotated = "yes" if merged["refresh_token"] != rt else "no"
    return (f"{path} {code} {c.get('email')} plan={a.get('chatgpt_plan_type')} "
            f"until={a.get('chatgpt_subscription_active_until')} "
            f"acct={a.get('chatgpt_account_id')} rt_rotated={rotated}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        print(refresh(p), flush=True)
