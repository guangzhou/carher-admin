#!/usr/bin/env python3
"""codex-device-oauth-simple.py — Minimal Codex device OAuth flow.

Uses playwright (not patchright) to avoid chromium version mismatch.
Runs inside the playwright Docker image on 188.

Env vars:
  MAIL_USER, MAIL_LOGIN_PW_FILE, CHATGPT_PW_FILE, AUTH_JSON_OUTPUT, SCREENSHOT_DIR
"""

import os, re, sys, json, time
import urllib.request, urllib.parse

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
SCOPE = "openid profile email offline_access"

EMAIL = os.environ["MAIL_USER"]
MAIL_PW = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
CHATGPT_PW = open(os.environ["CHATGPT_PW_FILE"]).read().strip()
SS_DIR = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
AUTH_OUT = os.environ.get("AUTH_JSON_OUTPUT", "/work/auth.json")

os.makedirs(SS_DIR, exist_ok=True)

def ss(page, name):
    page.screenshot(path=f"{SS_DIR}/{name}.png", full_page=False)
    print(f"  shot: {name}")

def curl_post(url, data, headers=None):
    """Simple POST using urllib (no external deps needed for Phase 1/3)."""
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Originator", "codex_cli_rs")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ── Phase 1: Get device code (curl, no browser needed) ─────────────────
print("[Phase 1] Requesting device code...")
status, body = curl_post(
    "https://auth.openai.com/oauth/device/authorize",
    {"client_id": CLIENT_ID, "scope": SCOPE},
)
print(f"  status={status}")
if status != 200:
    # Try alternative endpoint
    status, body = curl_post(
        "https://auth.openai.com/api/accounts/deviceauth/usercode",
        {"client_id": CLIENT_ID, "scope": SCOPE},
    )
    print(f"  alt status={status}")

if status != 200:
    sys.exit(f"Phase 1 failed: {status} {body[:200]}")

d1 = json.loads(body)
device_code = d1.get("device_code") or d1.get("device_auth_id", "")
user_code = d1["user_code"]
verify_uri = d1.get("verification_uri") or d1.get("verification_uri_complete") or "https://auth.openai.com/codex/device"
interval = int(d1.get("interval", 5))
print(f"  user_code={user_code}  device_code={device_code[:30]}...")

# ── Phase 2: Browser authorization ────────────────────────────────────
print("[Phase 2] Browser login + device authorization...")

from playwright.sync_api import sync_playwright

HEADLESS = os.environ.get("HEADLESS", "0") != "0"

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    page = ctx.new_page()

    # Navigate to verification page
    print(f"  Going to {verify_uri}...")
    page.goto(verify_uri, wait_until="domcontentloaded")
    time.sleep(3)
    ss(page, "01-verify-page")

    # Enter user code if input visible
    inputs = page.locator("input[type='text'], input[type='tel']")
    if inputs.count() > 0:
        print(f"  Entering user_code: {user_code}")
        inputs.first.fill(user_code)
        page.keyboard.press("Enter")
        time.sleep(3)
        ss(page, "02-code-entered")

    # Login: email
    email_input = page.locator("input[type='email'], input[name='email'], input[autocomplete='username']")
    if email_input.count() > 0:
        print(f"  Filling email: {EMAIL}")
        email_input.first.fill(EMAIL)
        submit = page.locator("button[type='submit'], button:has-text('Continue')")
        if submit.count() > 0:
            submit.first.click()
        else:
            page.keyboard.press("Enter")
        time.sleep(4)
        ss(page, "03-after-email")

    # Login: password
    pw_input = page.locator("input[type='password']")
    if pw_input.count() > 0:
        print(f"  Filling password...")
        pw_input.first.fill(CHATGPT_PW)
        submit = page.locator("button[type='submit'], button:has-text('Continue')")
        if submit.count() > 0:
            submit.first.click()
        else:
            page.keyboard.press("Enter")
        time.sleep(5)
        ss(page, "04-after-password")

    # OTP: check if needed
    page_text = page.content()
    need_otp = any(kw in page_text.lower() for kw in ["verification", "one-time", "check your email", "verify"])
    print(f"  Need OTP: {need_otp}")

    if need_otp:
        # Open mail.com to get OTP
        print("  Opening mail.com for OTP...")
        mail_page = ctx.new_page()
        mail_page.goto("https://www.mail.com/", wait_until="domcontentloaded")
        time.sleep(2)

        # Login to mail.com
        try:
            mail_page.locator("a:has-text('Log in')").first.click()
            time.sleep(1)
        except Exception:
            pass
        mail_page.locator("input[placeholder='Email address']").first.fill(EMAIL)
        mail_page.locator("input[placeholder='Password']").first.fill(MAIL_PW)
        login_btns = mail_page.locator("button:has-text('Log in')")
        for i in range(login_btns.count()):
            box = login_btns.nth(i).bounding_box()
            if box and box["y"] > 50:
                login_btns.nth(i).click()
                break
        time.sleep(5)
        ss(mail_page, "05-mail-inbox")

        # Wait for OTP email
        otp = None
        for attempt in range(12):
            time.sleep(5)
            for fr in mail_page.frames:
                try:
                    text = fr.evaluate("() => document.body.innerText")
                except Exception:
                    continue
                if not re.search(r"openai|chatgpt|noreply", text, re.I):
                    continue
                try:
                    fr.get_by_text(re.compile(r"openai|chatgpt", re.I)).first.click()
                    time.sleep(2)
                except Exception:
                    continue
                for f2 in mail_page.frames:
                    try:
                        body_text = f2.evaluate("() => document.body.innerText")
                    except Exception:
                        continue
                    for m in re.finditer(r"\b(\d{6})\b", body_text):
                        ctx_str = body_text[max(0, m.start()-100):m.start()+100]
                        if re.search(r"code|verify|openai|login", ctx_str, re.I):
                            otp = m.group(1)
                            break
                    if otp:
                        break
                if otp:
                    break
            if otp:
                break
            print(f"  Waiting for OTP... attempt {attempt+1}/12")
            # Refresh inbox
            for fr in mail_page.frames:
                if fr.name == "mail":
                    try:
                        fr.evaluate("() => document.location.reload()")
                    except Exception:
                        pass
            mail_page.wait_for_timeout(2000)

        mail_page.close()

        if not otp:
            ss(page, "06-no-otp")
            sys.exit("Failed to get OTP from mail.com")

        print(f"  Got OTP: {otp}")
        # Enter OTP on the auth page
        otp_input = page.locator("input[type='text'], input[type='tel'], input[autocomplete='one-time-code']")
        if otp_input.count() > 0:
            otp_input.first.fill(otp)
            page.keyboard.press("Enter")
            time.sleep(5)
            ss(page, "07-after-otp")

    # Check if we hit a consent/authorize page
    time.sleep(3)
    ss(page, "08-final")
    page_text = page.content()
    if "authorize" in page_text.lower() or "allow" in page_text.lower():
        allow_btn = page.locator("button:has-text('Allow'), button:has-text('Authorize'), button:has-text('Continue')")
        if allow_btn.count() > 0:
            allow_btn.first.click()
            time.sleep(3)
            ss(page, "09-authorized")

    print("  Phase 2 complete (browser)")
    browser.close()

# ── Phase 3: Poll for token ──────────────────────────────────────────
print("[Phase 3] Polling for token...")
token_data = None
for i in range(24):
    time.sleep(interval)
    status, body = curl_post(
        "https://auth.openai.com/oauth/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": CLIENT_ID,
        },
    )
    if status == 200:
        token_data = json.loads(body)
        print(f"  Got token! keys={list(token_data.keys())}")
        break
    resp = json.loads(body) if body.startswith("{") else {}
    err = resp.get("error", "")
    if err == "authorization_pending":
        print(f"  Pending... ({i+1}/24)")
        continue
    elif err == "slow_down":
        interval = min(interval + 2, 30)
        continue
    else:
        sys.exit(f"Phase 3 failed: {status} {body[:200]}")

if not token_data:
    sys.exit("Phase 3 timed out (120s)")

# Write auth.json
import base64
parts = token_data["access_token"].split(".")
payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
token_data["account_id"] = payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
token_data["expires_at"] = payload.get("exp", 0)

with open(AUTH_OUT, "w") as f:
    json.dump(token_data, f, indent=2)
print(f"\n✅ Written to {AUTH_OUT}")
print(f"   client_id={payload.get('client_id')}")
print(f"   expires_at={token_data['expires_at']}")
