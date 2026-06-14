#!/usr/bin/env python3
"""
chatgpt-device-oauth.py — Device code flow via auth.openai.com browser session
1. Browser opens auth.openai.com (CF clearance)
2. JS fetch to /oauth/device/authorize → get device_code + user_code
3. Browser navigates to verification_uri (auth.openai.com/codex/device)
4. Fill user_code → log in (email + password + OTP from mail.com)
5. Poll token endpoint until success → write auth.json
"""

import os, re, sys, json, base64, time, subprocess
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

EMAIL      = os.environ["MAIL_USER"]
MAIL_PW    = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
CHATGPT_PW = open(os.environ["CHATGPT_PW_FILE"]).read().strip()
SS_DIR     = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
AUTH_OUT   = os.environ.get("AUTH_JSON_OUTPUT", "/work/auth.json")
CLIENT_ID  = "app_EMoamEEZ73f0CkXaXp7hrann"

os.makedirs(SS_DIR, exist_ok=True)

def ss(page, name):
    path = f"{SS_DIR}/{name}.png"
    page.screenshot(path=path, full_page=False)
    print(f"  shot: {path}")

def mailcom_login(ctx):
    p = ctx.new_page()
    Stealth().apply_stealth_sync(p)
    p.goto("https://www.mail.com/", wait_until="domcontentloaded")
    p.locator("a:has-text('Log in')").first.click()
    p.wait_for_timeout(1500)
    p.locator("input[placeholder='Email address']").first.fill(EMAIL)
    p.locator("input[placeholder='Password']").first.fill(MAIL_PW)
    btns = p.locator("button:has-text('Log in')")
    for i in range(btns.count()):
        box = btns.nth(i).bounding_box()
        if box and box["y"] > 50:
            btns.nth(i).click()
            break
    p.wait_for_timeout(5000)
    if "navigator" not in p.url:
        ss(p, "mailcom-fail")
        sys.exit(f"mail.com login failed url={p.url}")
    ss(p, "mailcom-inbox")
    return p

def get_otp(mail_page, since_ts, max_wait=90):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        for fr in mail_page.frames:
            if fr.name != "mail":
                continue
            try:
                text = fr.evaluate("() => document.body.innerText")
            except Exception:
                continue
            for ln in text.splitlines():
                if not re.search(r"openai|chatgpt|noreply", ln, re.I):
                    continue
                try:
                    fr.get_by_text(ln, exact=False).first.click()
                    time.sleep(2)
                except Exception:
                    continue
                all_text = ""
                for f2 in mail_page.frames:
                    try:
                        all_text += f2.evaluate("() => document.body.innerText") + "\n"
                    except Exception:
                        pass
                for m in re.finditer(r"\b(\d{6})\b", all_text):
                    ctx_s = max(0, m.start() - 100)
                    ctx = all_text[ctx_s: m.start() + 100]
                    if re.search(r"code|verify|openai|login", ctx, re.I):
                        return m.group(1), ctx.strip()
        print("  OTP not yet, retry in 5s...")
        time.sleep(5)
        for fr in mail_page.frames:
            if fr.name == "mail":
                try:
                    fr.evaluate("() => document.location.reload()")
                except Exception:
                    pass
        mail_page.wait_for_timeout(3000)
    return None, None

HEADLESS = os.environ.get("HEADLESS", "1") != "0"

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
    )
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-US",
    )
    page = ctx.new_page()
    Stealth().apply_stealth_sync(page)

    # ── 1. Open auth.openai.com to get CF clearance ──────────────────────────
    print("[1] Opening auth.openai.com for CF clearance...")
    page.goto("https://auth.openai.com/log-in", wait_until="domcontentloaded")
    time.sleep(5)
    ss(page, "01-auth-openai-cf")
    print(f"  url={page.url}  title={page.title()[:40]}")

    # ── 2. Fetch device_code via JS in browser context ────────────────────────
    print("[2] Fetching device code via browser JS...")
    device_resp = page.evaluate(f"""async () => {{
        const r = await fetch("https://auth.openai.com/oauth/device/authorize", {{
            method: "POST",
            headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
            body: "client_id={CLIENT_ID}&scope=openid+profile+email+offline_access"
        }});
        const text = await r.text();
        return {{status: r.status, body: text}};
    }}""")
    print(f"  status={device_resp['status']}  body_preview={device_resp['body'][:200]}")

    if device_resp["status"] != 200:
        ss(page, "02-device-error")
        sys.exit(f"Device authorize failed: {device_resp}")

    device_data = json.loads(device_resp["body"])
    device_code = device_data["device_code"]
    user_code   = device_data["user_code"]
    verify_uri  = device_data.get("verification_uri", "https://auth.openai.com/codex/device")
    interval    = device_data.get("interval", 5)
    print(f"  user_code={user_code}  verify_uri={verify_uri}")
    ss(page, "02-device-ok")

    # ── 3. Navigate to device verification page ───────────────────────────────
    print(f"[3] Navigating to {verify_uri}...")
    page.goto(verify_uri, wait_until="domcontentloaded")
    time.sleep(3)
    ss(page, "03-verify-page")
    print(f"  url={page.url}  title={page.title()[:40]}")

    # Fill user_code
    code_input = page.locator("input").first
    if code_input.count() > 0:
        code_input.fill(user_code)
        page.keyboard.press("Enter")
        time.sleep(3)
    ss(page, "03b-code-filled")

    # ── 4. Complete login: email ──────────────────────────────────────────────
    print("[4] Filling email...")
    page.wait_for_selector("input[type='email'], input[autocomplete='username'], input[name='email']", timeout=15000)
    page.locator("input[type='email'], input[autocomplete='username'], input[name='email']").first.fill(EMAIL)
    cont = page.locator("button:has-text('Continue'), button[type='submit']")
    if cont.count() > 0:
        cont.first.click()
    else:
        page.keyboard.press("Enter")
    time.sleep(4)
    ss(page, "04-after-email")

    # ── 5. Fill password ──────────────────────────────────────────────────────
    print("[5] Filling password...")
    page.wait_for_selector("input[type='password']", timeout=15000)
    page.locator("input[type='password']").first.fill(CHATGPT_PW)
    cont2 = page.locator("button:has-text('Continue'), button[type='submit']")
    if cont2.count() > 0:
        cont2.first.click()
    else:
        page.keyboard.press("Enter")
    time.sleep(5)
    ss(page, "05-after-password")

    # ── 6. OTP if needed ─────────────────────────────────────────────────────
    body = page.content()
    need_otp = any(kw in body for kw in ["verification", "one-time", "OTP", "check your", "verify"])
    print(f"[6] Need OTP: {need_otp}")

    if need_otp:
        since_ts = int(time.time()) - 120
        print("[6a] Logging into mail.com...")
        mail_page = mailcom_login(ctx)
        otp, ctx_snip = get_otp(mail_page, since_ts)
        mail_page.close()
        if not otp:
            ss(page, "06-otp-timeout")
            sys.exit("❌ No OTP within 90s")
        print(f"  OTP: {otp}  ctx={ctx_snip[:60]}")
        otp_inp = page.locator("input[autocomplete='one-time-code'], input[type='text'], input[name='code']")
        otp_inp.first.fill(otp)
        page.locator("button:has-text('Continue'), button[type='submit']").first.click()
        time.sleep(4)
        ss(page, "06-after-otp")

    # ── 7. Wait for device auth completion ────────────────────────────────────
    print("[7] Waiting for device authorization completion...")
    deadline = time.time() + 90
    while time.time() < deadline:
        body_lower = page.content().lower()
        if any(t in body_lower for t in ("may now return", "device authorized", "you can close",
                                          "you've signed in", "successful", "codex", "all done")):
            print("  ✅ Device auth completed on browser side!")
            break
        ss_url = page.url
        print(f"  [{int(deadline-time.time())}s left] url={ss_url[:60]}")
        time.sleep(5)
    ss(page, "07-completion")

    # ── 8. Poll for token ─────────────────────────────────────────────────────
    print("[8] Polling for token...")
    token_data = None
    for attempt in range(30):
        result = page.evaluate(f"""async () => {{
            const r = await fetch("https://auth.openai.com/oauth/token", {{
                method: "POST",
                headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
                body: "grant_type=urn:ietf:params:oauth:grant-type:device_code&device_code={device_code}&client_id={CLIENT_ID}"
            }});
            const text = await r.text();
            return {{status: r.status, body: text}};
        }}""")
        resp_body = json.loads(result["body"])
        print(f"  attempt {attempt+1}: status={result['status']} keys={list(resp_body.keys())}")
        if "access_token" in resp_body:
            token_data = resp_body
            print("  ✅ Got token!")
            break
        if resp_body.get("error") == "authorization_pending":
            time.sleep(interval + 1)
        elif resp_body.get("error") == "slow_down":
            time.sleep(interval * 2)
        else:
            print(f"  Token error: {resp_body}")
            break

    if not token_data:
        sys.exit("❌ Failed to get token")

    # ── 9. Parse and write auth.json ──────────────────────────────────────────
    access_token   = token_data["access_token"]
    refresh_token  = token_data.get("refresh_token", "")
    id_token       = token_data.get("id_token", access_token)

    try:
        payload_raw = access_token.split(".")[1]
        payload_raw += "=" * (-len(payload_raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_raw))
        exp        = payload.get("exp", int(time.time()) + 3600)
        oai_auth   = payload.get("https://api.openai.com/auth", {})
        account_id = oai_auth.get("chatgpt_account_id", "")
        plan_type  = oai_auth.get("chatgpt_plan_type", "?")
    except Exception as e:
        print(f"  JWT decode error: {e}")
        exp, account_id, plan_type = int(time.time()) + 3600, "", "?"

    auth_json = {
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "id_token":      id_token,
        "expires_at":    exp,
        "account_id":    account_id,
    }
    with open(AUTH_OUT, "w") as f:
        json.dump(auth_json, f, indent=2)

    import datetime
    print(f"\n✅ auth.json → {AUTH_OUT}")
    print(f"   account_id : {account_id}")
    print(f"   plan_type  : {plan_type}")
    print(f"   expires_at : {exp}  ({datetime.datetime.fromtimestamp(exp)})")
    print(f"   token_len  : {len(access_token)}")

    browser.close()
