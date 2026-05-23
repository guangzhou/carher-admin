#!/usr/bin/env python3
"""
chatgpt-litellm-oauth.py — Full re-OAuth for a ChatGPT Pro account on 188

KEY DISCOVERIES (don't re-debug these):
  1. CF on auth.openai.com blocks browser SPA POSTs to /api/accounts/authorize/continue
     when traffic comes from playwright bundled chromium (TLS/HTTP2 fingerprint).
     → SOLVED: use `patchright` (playwright fork w/ TLS stealth) + image v1.59.0-noble
        (which ships chromium-1217 matching patchright's expectations).
  2. CF passes `Originator: codex_cli_rs` requests for /api/accounts/deviceauth/*
     and /oauth/token, so curl can drive Phase 1 + Phase 3 from 188 directly.
  3. Page renders need Xvfb (headed) — headless triggers CF Turnstile.
  4. From 188 (公司内网, NOT a cloud DC IP), auth.openai.com geolocates to JP
     and serves the normal login flow.

FLOW:
  Phase 1 (curl): POST /api/accounts/deviceauth/usercode → user_code
  Phase 2 (browser via patchright):
    - GET /codex/device → redirects to /log-in (Welcome back)
    - type email (delay=80) → Continue → /log-in/password
    - type password (delay=80) → Continue → /email-verification
    - if OTP needed: open mail.com in new page (字段A), wait for new ChatGPT
      email (top-row timestamp must change from baseline), grab 6-digit code
    - back on auth.openai.com: type OTP → Continue → wait URL leaves /email-verification
    - back to /codex/device → fill user_code → Authorize
  Phase 3 (curl):
    - POST /api/accounts/deviceauth/token (poll until 200) → authorization_code + code_verifier
    - POST /oauth/token (grant_type=authorization_code) → access_token / refresh_token / id_token
  Phase 4: decode JWT → write auth.json

ENV:
  MAIL_USER             EmilyOconnorgvg@mail.com
  MAIL_LOGIN_PW_FILE    /run/mail_pw.txt    (字段A, webmail password)
  CHATGPT_PW_FILE       /run/chatgpt_pw.txt (字段B, ChatGPT login password)
  AUTH_JSON_OUTPUT      /work/out/auth-acct-N.json
  SCREENSHOT_DIR        /work/screenshots
  HEADLESS              0 to run headed under Xvfb (default headless=1)

DOCKER RUN (on 188):
  docker run --rm \\
    -v /tmp/chatgpt-litellm-oauth.py:/work/script.py \\
    -v /tmp/mail_pw_acctN.txt:/run/mail_pw.txt \\
    -v /tmp/chatgpt_pw_acctN.txt:/run/chatgpt_pw.txt \\
    -v /tmp/screenshots-acctN:/work/screenshots \\
    -v /tmp:/work/out \\
    -e MAIL_USER=<email> \\
    -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \\
    -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \\
    -e AUTH_JSON_OUTPUT=/work/out/auth-acctN.json \\
    -e SCREENSHOT_DIR=/work/screenshots \\
    -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \\
    -e DISPLAY=:99 \\
    mcr.microsoft.com/playwright/python:v1.59.0-noble \\
    bash -c "Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & \\
             sleep 1 && \\
             pip install patchright -q --root-user-action=ignore && \\
             python3 /work/script.py"

KNOWN STILL-FLAKY (work in progress):
  - OTP throttling: OpenAI may rate-limit OTP emails after multiple recent
    requests; if /email-verification arrives but no new email lands within 60s,
    wait 10+ minutes before retrying.
  - mail.com webmail inbox parsing: get_otp() now waits for the top-ChatGPT-row
    timestamp to change vs baseline (proves it's THIS session's email),
    then clicks that row and extracts the 6-digit code.
"""

import os, re, sys, json, base64, time
import urllib.request
import urllib.parse
import urllib.error
# patchright = playwright fork with TLS/CDP stealth (needed to bypass CF on auth.openai.com SPA fetches)
from patchright.sync_api import sync_playwright

EMAIL      = os.environ["MAIL_USER"]
MAIL_PW    = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
CHATGPT_PW = open(os.environ["CHATGPT_PW_FILE"]).read().strip()
SS_DIR     = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
AUTH_OUT   = os.environ.get("AUTH_JSON_OUTPUT", "/work/auth.json")
CLIENT_ID  = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE  = "https://auth.openai.com"

os.makedirs(SS_DIR, exist_ok=True)

CODEX_HEADERS = {
    "Content-Type": "application/json",
    "Originator": "codex_cli_rs",
    "User-Agent": "codex_cli_rs/0.30.0 (Linux 5.15; x86_64) unknown",
}

def ss(page, name):
    path = f"{SS_DIR}/{name}.png"
    try:
        page.screenshot(path=path, full_page=False)
        print(f"  shot: {path}", flush=True)
    except Exception as e:
        print(f"  shot fail: {e}", flush=True)

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

# ── Step 1: get user_code via codex_cli_rs endpoint ─────────────────────────
print("[1] Request device code via /api/accounts/deviceauth/usercode...", flush=True)
status, body = http_post(
    f"{AUTH_BASE}/api/accounts/deviceauth/usercode",
    {"client_id": CLIENT_ID},
)
print(f"  status={status} body={body[:200]}", flush=True)
if status != 200:
    sys.exit(f"❌ Failed to get user_code: {body[:300]}")
device_data = json.loads(body)
DEVICE_AUTH_ID = device_data["device_auth_id"]
USER_CODE = device_data["user_code"]
INTERVAL = int(device_data.get("interval", "5"))
print(f"  ✅ user_code={USER_CODE}  device_auth_id={DEVICE_AUTH_ID[:30]}...", flush=True)

# ── Step 2: browser - navigate to verify page, fill user_code ────────────────
def mailcom_login(ctx):
    p = ctx.new_page()
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
    # wait login redirect to navigator.mail.com
    for _ in range(30):
        if "navigator" in p.url:
            break
        time.sleep(1)
    if "navigator" not in p.url:
        ss(p, "mailcom-fail")
        sys.exit(f"mail.com login failed url={p.url}")
    # dismiss interstitials: "Continue to Account" / upgrade prompts
    p.wait_for_timeout(3000)
    for sel in [
        "a:has-text('Continue to Account')",
        "button:has-text('Continue to Account')",
        "button:has-text('No, thanks')",
        "button:has-text('Maybe later')",
        "button:has-text('Skip')",
    ]:
        try:
            loc = p.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                print(f"  mail.com: clicking '{sel}'", flush=True)
                loc.first.click()
                p.wait_for_timeout(2000)
        except Exception:
            pass
    # wait for the actual mail iframe (name='mail') to appear and have content
    for attempt in range(20):
        mail_frame = next((fr for fr in p.frames if fr.name == "mail"), None)
        if mail_frame:
            try:
                txt = mail_frame.evaluate("() => document.body.innerText")
                if txt and len(txt) > 50:  # inbox loaded with content
                    print(f"  mail.com: inbox loaded ({len(txt)} chars)", flush=True)
                    break
            except Exception:
                pass
        print(f"  mail.com: waiting for inbox iframe... [{attempt+1}/20]", flush=True)
        time.sleep(2)
    ss(p, "mailcom-inbox")
    return p

def get_otp(mail_page, since_ts, max_wait=180):
    """Find topmost OpenAI/ChatGPT email, click it, extract 6-digit OTP from body.
    Reverted to original simple logic (proven to work — got 815911 first run).
    `since_ts` kept for signature compat, not currently used."""
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
                    if f2.name == "mail":
                        continue
                    try:
                        all_text += f2.evaluate("() => document.body.innerText") + "\n"
                    except Exception:
                        pass
                for m in re.finditer(r"\b(\d{6})\b", all_text):
                    ctx_s = max(0, m.start() - 100)
                    ctx = all_text[ctx_s: m.start() + 100]
                    if re.search(r"code|verify|openai|login", ctx, re.I):
                        return m.group(1), ctx.strip()
        print("  OTP not yet, retry in 5s...", flush=True)
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
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = ctx.new_page()

    # ── 2a. Navigate to verify page (会跳转到 /log-in) ──────────────────
    print("[2] Open auth.openai.com/codex/device...", flush=True)
    page.goto(f"{AUTH_BASE}/codex/device", wait_until="domcontentloaded")
    # Wait through CF Turnstile if any
    deadline = time.time() + 60
    while time.time() < deadline:
        title = page.title()
        body_head = page.content().lower()[:1500]
        if title and "moment" not in title.lower() and "performing security" not in body_head:
            break
        print(f"  [{int(deadline-time.time())}s] waiting CF... title={repr(title[:30])}", flush=True)
        time.sleep(3)
    ss(page, "01-after-cf")
    print(f"  url={page.url}  title={page.title()[:40]}", flush=True)

    # ── 2b. Fill email (Welcome back 登录页) ─────────────────────────────
    print(f"[3] Fill email: {EMAIL}", flush=True)
    email_sel = "input[type='email'], input[autocomplete='username'], input[name='email']"
    page.wait_for_selector(email_sel, timeout=20000)
    page.locator(email_sel).first.click()
    page.keyboard.type(EMAIL, delay=80)
    ss(page, "02-email-filled")
    btn = page.locator("button:has-text('Continue'), button[type='submit']")
    if btn.count() > 0:
        btn.first.click()
    else:
        page.keyboard.press("Enter")
    time.sleep(6)
    ss(page, "03-after-email")
    print(f"  url={page.url}", flush=True)

    # ── 2c. Fill password (字段B) ────────────────────────────────────────
    print(f"[4] Fill password 字段B (len={len(CHATGPT_PW)})", flush=True)
    page.wait_for_selector("input[type='password']", timeout=20000)
    page.locator("input[type='password']").first.click()
    page.keyboard.type(CHATGPT_PW, delay=80)
    ss(page, "04-pw-filled")
    btn = page.locator("button:has-text('Continue'), button:has-text('Sign in'), button[type='submit']")
    if btn.count() > 0:
        btn.first.click()
    else:
        page.keyboard.press("Enter")
    time.sleep(6)
    ss(page, "05-after-password")
    print(f"  url={page.url}", flush=True)

    # ── 2d. OTP if needed ────────────────────────────────────────────────
    body_text = page.content().lower()
    need_otp = any(k in body_text for k in (
        "verification code", "one-time", "verify your email", "check your email",
        "enter the code", "we sent", "enter code",
    ))
    print(f"[5] Need OTP: {need_otp}", flush=True)
    if need_otp:
        print("  Logging into mail.com 字段A...", flush=True)
        SCRIPT_START = int(time.time())  # passed to get_otp for staleness check
        mail_page = mailcom_login(ctx)
        otp, ctx_snip = get_otp(mail_page, SCRIPT_START)
        mail_page.close()
        if not otp:
            ss(page, "06-otp-timeout")
            sys.exit("❌ No OTP within 120s")
        print(f"  ✅ OTP: {otp}", flush=True)
        page.bring_to_front()
        # 用键盘 type, 模拟真实输入(SPA 监听 keyup 启用 Continue 按钮)
        otp_input = page.locator("input[name='code'], input[autocomplete='one-time-code'], input").first
        otp_input.click()
        page.keyboard.type(otp, delay=80)
        ss(page, "06-otp-filled")
        # submit
        sb = page.locator("button:has-text('Continue'), button:has-text('Verify'), button[type='submit']")
        if sb.count() > 0:
            sb.first.click()
        else:
            page.keyboard.press("Enter")
        # 等离开 /email-verification (最多 40s)
        print("  Waiting for /email-verification → next page...", flush=True)
        for _ in range(40):
            if "email-verification" not in page.url:
                break
            time.sleep(1)
        ss(page, "07-after-otp")
        print(f"  url={page.url}", flush=True)
        if "email-verification" in page.url:
            sys.exit("❌ OTP submit didn't advance past /email-verification")

    # ── 2d2. OAuth consent page: "Sign in to Codex with ChatGPT" → Continue ──
    if "/consent" in page.url or "codex/consent" in page.url:
        print("[5b] OAuth consent page — clicking Continue...", flush=True)
        # consent button is "Continue" (dark button)
        consent_btn = page.locator("button:has-text('Continue'), button:has-text('Allow'), button:has-text('Authorize')")
        if consent_btn.count() > 0:
            consent_btn.first.click()
        else:
            page.keyboard.press("Enter")
        # wait to leave consent
        for _ in range(30):
            if "/consent" not in page.url:
                break
            time.sleep(1)
        ss(page, "07b-after-consent")
        print(f"  url={page.url}", flush=True)

    # ── 2e. 输入 user_code (9 方框页面 — Use your device code to grant access) ───
    # 注意:URL 可能是 deviceauth/callback?code=... 但页面渲染的是 user_code 输入页
    # 必须输入 user_code 才能让 OpenAI 把当前 OAuth flow 绑到我们的 device_auth_id
    print(f"[6] Filling USER_CODE: {USER_CODE}", flush=True)
    # 找 9 个 1-char input boxes (or single input)
    user_code_clean = USER_CODE.replace("-", "")  # 9XBE-AG4JT → 9XBEAG4JT
    # 等页面渲染好(可能从 consent 跳过来)
    time.sleep(3)
    ss(page, "08-user-code-page")
    # 尝试找 9 个 1-char boxes
    inputs = [i for i in page.locator("input").all() if i.is_visible()]
    print(f"  visible inputs on page: {len(inputs)}", flush=True)
    if len(inputs) >= 9:
        # 9 boxes mode — fill one char each
        inputs[0].click()
        page.keyboard.type(user_code_clean, delay=80)
    elif inputs:
        inputs[0].click()
        page.keyboard.type(USER_CODE, delay=80)  # try with dash
    else:
        ss(page, "09-no-input")
        sys.exit("❌ no input field found on user_code page")
    ss(page, "09-code-filled")
    time.sleep(2)
    # Click the Continue button (NOT Cancel) — use get_by_role to be safe
    try:
        cont_btn = page.get_by_role("button", name="Continue", exact=True)
        if cont_btn.count() > 0 and cont_btn.first.is_enabled():
            cont_btn.first.click()
            print("  clicked Continue (by role)", flush=True)
        else:
            raise Exception("Continue not enabled or not found")
    except Exception as e:
        print(f"  get_by_role failed: {e}, fallback to keyboard Enter", flush=True)
        page.keyboard.press("Enter")
    time.sleep(6)
    ss(page, "10-after-authorize")
    print(f"  url={page.url}", flush=True)

    # ── 2f. Wait for completion ──────────────────────────────────────────
    print("[7] Wait for completion...", flush=True)
    for _ in range(30):
        body_lower = page.content().lower()
        if any(t in body_lower for t in ("may now return", "device authorized", "you can close",
                                          "signed in to codex", "successful", "all done", "successfully signed in")):
            print(f"  ✅ Browser shows success: url={page.url}", flush=True)
            break
        time.sleep(2)
    ss(page, "11-final")
    browser.close()

# ── Step 3: poll for authorization_code ────────────────────────────────
print("[8] Poll /api/accounts/deviceauth/token for auth code...", flush=True)
auth_code = None
code_challenge = None
code_verifier = None
for attempt in range(60):
    status, body = http_post(
        f"{AUTH_BASE}/api/accounts/deviceauth/token",
        {"device_auth_id": DEVICE_AUTH_ID, "user_code": USER_CODE},
    )
    if status == 200:
        d = json.loads(body)
        if "authorization_code" in d:
            auth_code = d["authorization_code"]
            code_challenge = d.get("code_challenge")
            code_verifier = d.get("code_verifier")
            print(f"  ✅ Got authorization_code", flush=True)
            break
    print(f"  attempt {attempt+1}: status={status} body={body[:100]}", flush=True)
    time.sleep(INTERVAL)

if not auth_code:
    sys.exit("❌ Failed to get authorization_code")

# ── Step 4: exchange code for tokens ───────────────────────────────────
print("[9] Exchange auth code → tokens at /oauth/token...", flush=True)
form_body = "&".join([
    "grant_type=authorization_code",
    f"code={urllib.parse.quote(auth_code)}",
    f"redirect_uri={urllib.parse.quote(f'{AUTH_BASE}/deviceauth/callback')}",
    f"client_id={CLIENT_ID}",
    f"code_verifier={urllib.parse.quote(code_verifier or '')}",
])
status, body = http_post(
    f"{AUTH_BASE}/oauth/token",
    form_body,
    extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
)
print(f"  status={status} body={body[:300]}", flush=True)
if status != 200:
    sys.exit(f"❌ Token exchange failed: {body[:500]}")

tok = json.loads(body)
access_token  = tok["access_token"]
refresh_token = tok.get("refresh_token", "")
id_token      = tok.get("id_token", access_token)

# decode JWT
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
with open(AUTH_OUT, "w") as f:
    json.dump(out, f, indent=2)

import datetime
print(f"\n✅ auth.json → {AUTH_OUT}", flush=True)
print(f"   account_id : {account_id}", flush=True)
print(f"   plan_type  : {plan_type}", flush=True)
print(f"   expires_at : {exp}  ({datetime.datetime.fromtimestamp(exp)})", flush=True)
print(f"   token_len  : {len(access_token)}", flush=True)
