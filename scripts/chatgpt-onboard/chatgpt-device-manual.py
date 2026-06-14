#!/usr/bin/env python3
"""
chatgpt-device-manual.py — Device-code flow with MANUAL human browser login.

Unlike chatgpt-litellm-oauth.py (which drives the browser via patchright and
hits /add-phone), this script does ZERO browser automation:

  1. POST /api/accounts/deviceauth/usercode   → user_code + device_auth_id
  2. PRINT the user_code + verification URL — the operator opens it in their
     OWN browser, logs in, completes phone binding / consent MANUALLY.
  3. Poll  /api/accounts/deviceauth/token     → authorization_code + code_verifier
  4. POST  /oauth/token                        → access_token / refresh_token
  5. Write auth.json.

CF passes plain urllib requests that carry `Originator: codex_cli_rs` for the
/api/accounts/deviceauth/* and /oauth/token endpoints (verified from 188, whose
IP geolocates to JP). No Cloudflare clearance browser needed.

ENV:
  AUTH_JSON_OUTPUT   where to write auth.json (default /work/out/auth.json)
  POLL_MINUTES       how long to wait for the human to finish (default 20)
"""
import os, sys, json, base64, time
import urllib.request, urllib.parse, urllib.error

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE = "https://auth.openai.com"
AUTH_OUT  = os.environ.get("AUTH_JSON_OUTPUT", "/work/out/auth.json")
POLL_MIN  = int(os.environ.get("POLL_MINUTES", "20"))

CODEX_HEADERS = {
    "Content-Type": "application/json",
    "Originator": "codex_cli_rs",
    "User-Agent": "codex_cli_rs/0.30.0 (Linux 5.15; x86_64) unknown",
}


def http_post(url, body, extra_headers=None, timeout=20):
    headers = dict(CODEX_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


# ── 1. Request user_code ────────────────────────────────────────────────────
print("[1] POST /api/accounts/deviceauth/usercode ...", flush=True)
status, body = http_post(f"{AUTH_BASE}/api/accounts/deviceauth/usercode",
                         {"client_id": CLIENT_ID})
print(f"  status={status} body={body[:300]}", flush=True)
if status != 200:
    sys.exit(f"❌ usercode failed: {body[:400]}")
d = json.loads(body)
DEVICE_AUTH_ID = d["device_auth_id"]
USER_CODE      = d["user_code"]
INTERVAL       = int(d.get("interval", "5"))
VERIFY_URI     = d.get("verification_uri") or f"{AUTH_BASE}/codex/device"
VERIFY_FULL    = d.get("verification_uri_complete") or ""

print("", flush=True)
print("  ╔════════════════════════════════════════════════════════════════╗", flush=True)
print("  ║  MANUAL DEVICE LOGIN — do this in YOUR browser                   ║", flush=True)
print("  ╚════════════════════════════════════════════════════════════════╝", flush=True)
print(f"  USER_CODE     : {USER_CODE}", flush=True)
print(f"  VERIFY URL    : {VERIFY_URI}", flush=True)
if VERIFY_FULL:
    print(f"  VERIFY (full) : {VERIFY_FULL}", flush=True)
print(f"  device_auth_id: {DEVICE_AUTH_ID[:40]}...", flush=True)
print(f"  → log in, enter the code, bind phone & authorize. Polling {POLL_MIN}min.", flush=True)
print("", flush=True)

# ── 2. Poll for authorization_code (human is logging in meanwhile) ───────────
print("[2] Polling /api/accounts/deviceauth/token ...", flush=True)
auth_code = None
code_verifier = None
deadline = time.time() + POLL_MIN * 60
attempt = 0
while time.time() < deadline:
    attempt += 1
    status, body = http_post(f"{AUTH_BASE}/api/accounts/deviceauth/token",
                             {"device_auth_id": DEVICE_AUTH_ID, "user_code": USER_CODE})
    if status == 200:
        dd = json.loads(body)
        if "authorization_code" in dd:
            auth_code = dd["authorization_code"]
            code_verifier = dd.get("code_verifier")
            print(f"  ✅ got authorization_code (attempt {attempt})", flush=True)
            break
    if attempt % 6 == 1:
        print(f"  attempt {attempt}: status={status} body={body[:90]}", flush=True)
    time.sleep(INTERVAL)

if not auth_code:
    sys.exit(f"❌ no authorization_code within {POLL_MIN}min (human didn't finish?)")

# ── 3. Exchange code → tokens ────────────────────────────────────────────────
print("[3] POST /oauth/token ...", flush=True)
form_body = "&".join([
    "grant_type=authorization_code",
    f"code={urllib.parse.quote(auth_code)}",
    f"redirect_uri={urllib.parse.quote(f'{AUTH_BASE}/deviceauth/callback')}",
    f"client_id={CLIENT_ID}",
    f"code_verifier={urllib.parse.quote(code_verifier or '')}",
])
status, body = http_post(f"{AUTH_BASE}/oauth/token", form_body,
                         extra_headers={"Content-Type": "application/x-www-form-urlencoded"})
print(f"  status={status} body={body[:200]}", flush=True)
if status != 200:
    sys.exit(f"❌ token exchange failed: {body[:500]}")

tok = json.loads(body)
access_token  = tok["access_token"]
refresh_token = tok.get("refresh_token", "")
id_token      = tok.get("id_token", access_token)

# ── 4. Decode JWT + write auth.json ──────────────────────────────────────────
try:
    parts = access_token.split(".")
    pl = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(pl))
    exp = claims.get("exp", int(time.time()) + 3600)
    auth_claims = claims.get("https://api.openai.com/auth", {})
    account_id = auth_claims.get("chatgpt_account_id", "")
    plan_type  = auth_claims.get("chatgpt_plan_type", "?")
except Exception as e:
    print(f"  JWT decode error: {e}", flush=True)
    exp, account_id, plan_type = int(time.time()) + 3600, "", "?"

out = {
    "access_token":  access_token,
    "refresh_token": refresh_token,
    "id_token":      id_token,
    "expires_at":    exp,
    "account_id":    account_id,
}
os.makedirs(os.path.dirname(AUTH_OUT) or ".", exist_ok=True)
with open(AUTH_OUT, "w") as f:
    json.dump(out, f, indent=2)

import datetime
print(f"\n✅ auth.json → {AUTH_OUT}", flush=True)
print(f"   account_id : {account_id}", flush=True)
print(f"   plan_type  : {plan_type}", flush=True)
print(f"   expires_at : {exp}  ({datetime.datetime.fromtimestamp(exp)})", flush=True)
print(f"   token_len  : {len(access_token)}", flush=True)
