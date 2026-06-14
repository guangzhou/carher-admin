#!/usr/bin/env python3
"""
device-code-manual.py — Generate a ChatGPT Codex device code for MANUAL binding.

Pure-curl (no browser): the codex_cli_rs Originator header passes Cloudflare, so we
drive the device-auth API directly. A human enters the printed user_code at
auth.openai.com/codex/device (handling login + phone verification themselves); this
script polls until they finish, exchanges the code, and writes auth.json.

ENV:
  AUTH_JSON_OUTPUT   output path for auth.json (default /work/out/auth.json)
  POLL_MINUTES       how long to keep polling for completion (default 18)
"""
import os, re, sys, json, base64, time
import urllib.request, urllib.parse, urllib.error

AUTH_OUT     = os.environ.get("AUTH_JSON_OUTPUT", "/work/out/auth.json")
POLL_MINUTES = float(os.environ.get("POLL_MINUTES", "18"))
CLIENT_ID    = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE    = "https://auth.openai.com"
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

# ── 1. get user_code ──────────────────────────────────────────────────────
print("[1] request user_code via /api/accounts/deviceauth/usercode...", flush=True)
status, body = http_post(f"{AUTH_BASE}/api/accounts/deviceauth/usercode", {"client_id": CLIENT_ID})
print(f"  status={status} body={body[:200]}", flush=True)
if status != 200:
    sys.exit(f"❌ usercode failed: {body[:300]}")
dd = json.loads(body)
DEVICE_AUTH_ID = dd["device_auth_id"]
USER_CODE = dd["user_code"]
INTERVAL = int(dd.get("interval", "5"))
print("=" * 60, flush=True)
print(f"USER_CODE={USER_CODE}", flush=True)
print(f"VERIFY_URI={AUTH_BASE}/codex/device", flush=True)
print("=" * 60, flush=True)

# ── 2. poll for authorization_code (human binds in browser meanwhile) ──────
print(f"[2] polling for up to {POLL_MINUTES} min — enter the code at {AUTH_BASE}/codex/device ...", flush=True)
deadline = time.time() + POLL_MINUTES * 60
auth_code = None
code_verifier = None
n = 0
while time.time() < deadline:
    n += 1
    status, body = http_post(f"{AUTH_BASE}/api/accounts/deviceauth/token",
                             {"device_auth_id": DEVICE_AUTH_ID, "user_code": USER_CODE})
    if status == 200:
        d = json.loads(body)
        if "authorization_code" in d:
            auth_code = d["authorization_code"]
            code_verifier = d.get("code_verifier")
            print("  ✅ got authorization_code", flush=True)
            break
    if n % 6 == 0:
        print(f"  [{int(deadline-time.time())}s left] status={status} {body[:80]}", flush=True)
    time.sleep(INTERVAL)

if not auth_code:
    sys.exit("❌ timed out waiting for manual bind (device code may have expired)")

# ── 3. exchange code → tokens ──────────────────────────────────────────────
print("[3] exchange auth code → tokens at /oauth/token...", flush=True)
form = "&".join([
    "grant_type=authorization_code",
    f"code={urllib.parse.quote(auth_code)}",
    f"redirect_uri={urllib.parse.quote(f'{AUTH_BASE}/deviceauth/callback')}",
    f"client_id={CLIENT_ID}",
    f"code_verifier={urllib.parse.quote(code_verifier or '')}",
])
status = None
body = ""
for attempt in range(1, 6):
    status, body = http_post(f"{AUTH_BASE}/oauth/token", form,
                             extra_headers={"Content-Type": "application/x-www-form-urlencoded"})
    print(f"  attempt={attempt} status={status} body={body[:200]}", flush=True)
    if status == 200:
        break
    if status in (429, 500, 502, 503, 504) and attempt < 5:
        time.sleep(2 * attempt)
        continue
    break
if status != 200:
    sys.exit(f"❌ token exchange failed: {body[:400]}")
tok = json.loads(body)
at = tok["access_token"]; rt = tok.get("refresh_token", ""); it = tok.get("id_token", at)
try:
    p = at.split(".")[1]; p += "=" * (-len(p) % 4)
    claims = json.loads(base64.urlsafe_b64decode(p))
    exp = claims.get("exp", int(time.time()) + 3600)
    oai = claims.get("https://api.openai.com/auth", {})
    account_id = oai.get("chatgpt_account_id", "")
    plan = oai.get("chatgpt_plan_type", "?")
except Exception as e:
    print(f"  jwt decode err: {e}", flush=True)
    exp, account_id, plan = int(time.time()) + 3600, "", "?"

os.makedirs(os.path.dirname(AUTH_OUT), exist_ok=True)
json.dump({"access_token": at, "refresh_token": rt, "id_token": it,
           "expires_at": exp, "account_id": account_id}, open(AUTH_OUT, "w"), indent=2)
import datetime
print(f"\n✅ auth.json → {AUTH_OUT}  account_id={account_id} plan={plan} "
      f"expires_at={exp} ({datetime.datetime.fromtimestamp(exp)})", flush=True)
