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
# 2026-05-28: SPA POST /api/accounts/authorize/continue 需要 patchright 真 Chrome TLS;页面层 CF 需要 headed
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

# ── SMS OTP helpers (2026-06-02: handle "Phone number required" challenge) ──
# OpenAI risk-control may demand SMS phone verification before re-issuing a
# token. PHONE_NUMBER = national digits (country select defaults US +1);
# SMS_API_URL = a virtual-number inbox endpoint returning plain text where a
# real message line carries a 6-digit code and the idle state is "暂无短信|...".
PHONE_NUMBER = os.environ.get("PHONE_NUMBER", "").strip()
SMS_API_URL  = os.environ.get("SMS_API_URL", "").strip()

def _sms_fetch():
    if not SMS_API_URL:
        return ""
    try:
        req = urllib.request.Request(SMS_API_URL, headers={"User-Agent": "Mozilla/5.0"})
        return urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  sms fetch err: {e}", flush=True)
        return ""

def _sms_codes(txt):
    """Standalone 6-digit codes from SMS text, skipping the idle boilerplate line.
    Expiry dates like 2026-06-09 23:59:59 have no 6-consecutive-digit run."""
    codes = []
    for ln in txt.splitlines():
        if "暂无短信" in ln:
            continue
        codes.extend(re.findall(r"(?<!\d)(\d{6})(?!\d)", ln))
    return codes

def poll_sms_otp(baseline, timeout=150):
    """Wait for a 6-digit code NOT in baseline (the number may be reused across
    accounts, so old codes can linger)."""
    seen = set(baseline)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in _sms_codes(_sms_fetch()):
            if c not in seen:
                return c
        time.sleep(5)
    return ""

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
# OTP provider switch:
#   MAIL_OTP_PROVIDER=mailcom (default)  → browser-driven www.mail.com webmail
#   MAIL_OTP_PROVIDER=imap_qq            → imaplib + imap.qq.com:993 (字段A = QQ 16-char auth code)
#   MAIL_OTP_PROVIDER=imap               → generic IMAP (set IMAP_HOST/IMAP_PORT)
MAIL_OTP_PROVIDER = os.environ.get("MAIL_OTP_PROVIDER", "mailcom").lower()

def imap_host_port():
    if MAIL_OTP_PROVIDER == "imap_qq":
        return ("imap.qq.com", 993)
    return (os.environ.get("IMAP_HOST", "imap.qq.com"), int(os.environ.get("IMAP_PORT", "993")))

def imap_fetch_otp(since_ts, max_wait=180):
    """Poll IMAP for the latest OpenAI/ChatGPT login OTP. Returns (otp, ctx) or (None, None).
    `since_ts` filters mails newer than this Unix timestamp."""
    import imaplib, email as _email
    from email.header import decode_header as _dh
    host, port = imap_host_port()
    deadline = time.time() + max_wait
    last_seen_uid = None
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(host, port, timeout=20)
            M.login(EMAIL, MAIL_PW)
            M.select("INBOX")
            typ, data = M.search(None, "FROM", "tm.openai.com", "SUBJECT", "temporary")
            ids = data[0].split()
            # newest first
            for mid in reversed(ids[-5:]):
                typ, msg_data = M.fetch(mid, "(RFC822)")
                msg = _email.message_from_bytes(msg_data[0][1])
                # parse date
                try:
                    mail_ts = _email.utils.mktime_tz(_email.utils.parsedate_tz(msg["Date"]))
                except Exception:
                    mail_ts = 0
                if mail_ts < since_ts - 30:
                    continue  # too old
                body = ""
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        b = part.get_payload(decode=True)
                        if b:
                            body = b.decode(part.get_content_charset() or "utf-8", errors="replace")
                            break
                m = re.search(r"\b(\d{6})\b", body)
                if m:
                    code = m.group(1)
                    print(f"  IMAP: got OTP {code} from mail dated {msg['Date']}", flush=True)
                    try: M.logout()
                    except: pass
                    return code, body[:200]
            try: M.logout()
            except: pass
        except Exception as e:
            print(f"  IMAP fetch err: {e}", flush=True)
        print(f"  IMAP: OTP not yet (since_ts={since_ts}), retry in 10s...", flush=True)
        time.sleep(10)
    return None, None

def mailcom_login(ctx):
    if MAIL_OTP_PROVIDER == "outlook":
        return outlook_login(ctx)
    if MAIL_OTP_PROVIDER != "mailcom":
        print(f"  [skip] mailcom_login — using OTP provider={MAIL_OTP_PROVIDER}", flush=True)
        return None  # sentinel; get_otp will route by provider
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
    # 2026-06-18: mail.com 把邮件列表迁到 Shadow DOM, innerText 返回空,
    # 改用 [class*='mail-item'] 选择器探测 row 是否到位
    for attempt in range(20):
        mail_frame = next((fr for fr in p.frames if fr.name == "mail"), None)
        if mail_frame:
            try:
                n = mail_frame.locator("[class*='mail-item']").count()
                if n > 0:
                    print(f"  mail.com: inbox loaded (mail-item rows={n})", flush=True)
                    break
            except Exception:
                pass
        print(f"  mail.com: waiting for inbox iframe... [{attempt+1}/20]", flush=True)
        time.sleep(2)
    ss(p, "mailcom-inbox")
    return p

# ── outlook.live.com provider (2026-06-22: hotmail acct) ────────────────────
# 走 login.live.com → outlook.live.com inbox; 邮件 subject + body 都明文(没 Shadow DOM)
# 用 div[role='option']/div[role='listitem'] 拿 row text, regex 6位 OTP
def outlook_login(ctx):
    if MAIL_OTP_PROVIDER != "outlook":
        return None
    p = ctx.new_page()
    entry = ("https://login.live.com/login.srf?wa=wsignin1.0&rpsnv=13&ct=" + str(int(time.time()))
             + "&rver=7.0.6738.0&wp=MBI_SSL&wreply=https%3a%2f%2foutlook.live.com%2fowa%2f%3frealm%3dhotmail.com&id=292841&aadredir=1&CBCXT=out&lw=1&fl=dob,easi2&cobrandid=90015")
    p.goto(entry, timeout=30000)
    time.sleep(3)
    ss(p, "outlook-landing")
    # email
    e = p.locator("input[type='email'], input[name='loginfmt'], input#i0116").first
    e.wait_for(timeout=20000); e.click(); e.fill(""); e.type(EMAIL, delay=60)
    nb = p.get_by_role("button", name="Next", exact=True)
    if nb.count() == 0:
        nb = p.locator("input[type='submit'], input#idSIButton9, button[type='submit']")
    nb.first.click(); time.sleep(4)
    # password
    pwi = p.locator("input[type='password'], input#i0118, input[name='passwd']").first
    pwi.wait_for(timeout=20000); pwi.click(); pwi.type(MAIL_PW, delay=60)
    sb = p.get_by_role("button", name="Next", exact=True)
    if sb.count() == 0:
        sb = p.get_by_role("button", name="Sign in", exact=True)
    if sb.count() == 0:
        sb = p.locator("input[type='submit'], input#idSIButton9, button[type='submit']")
    sb.first.click(); time.sleep(6)
    # KMSI "Stay signed in?" — click No
    for sf in [
        lambda: p.get_by_role("button", name="No", exact=True),
        lambda: p.locator("input#idBtn_Back"),
        lambda: p.locator("button:has-text('No')"),
    ]:
        try:
            loc = sf()
            if loc.count() > 0 and loc.first.is_visible():
                print("  outlook: KMSI click No", flush=True)
                loc.first.click(); time.sleep(4)
                break
        except Exception:
            pass
    # 等 inbox 渲染
    for _ in range(20):
        if "outlook.live.com" in p.url and "/mail/" in p.url:
            break
        time.sleep(2)
    ss(p, "outlook-inbox")
    print(f"  outlook: inbox url={p.url[:80]}", flush=True)
    return p

def outlook_get_otp(mail_page, since_ts, max_wait=180):
    """outlook inbox 找最新 OpenAI OTP 邮件. subject 含 6位数字 + 必须含相对时间(今日)."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            mail_page.reload(wait_until="domcontentloaded"); time.sleep(5)
        except Exception:
            pass
        ss(mail_page, "outlook-poll")
        items = mail_page.locator("div[role='option'], div[role='listitem']").all()
        for it in items[:10]:  # 倒序最新在前
            try:
                txt = it.inner_text(timeout=2000)
            except Exception:
                continue
            tl = txt.lower()
            if ("openai" not in tl) and ("chatgpt" not in tl):
                continue
            if ("login code" not in tl) and ("verification code" not in tl) and ("temporary" not in tl):
                continue
            # 今日邮件: "1:36" 时钟 或 "now"/"min ago"
            has_recent = any(m in tl for m in ["now", "min ago", "minute", "sec", "几秒", "几分", "刚刚"])
            has_clock = bool(re.search(r"\b\d{1,2}:\d{2}\b", tl))
            if not (has_recent or has_clock):
                continue
            m = re.search(r"\b(\d{6})\b", txt)
            if m:
                code = m.group(1)
                print(f"  outlook: OTP candidate {code} from row {txt[:80]!r}", flush=True)
                # 点开邮件正文确认
                try:
                    it.click(); time.sleep(3)
                    ss(mail_page, "outlook-open")
                    body_loc = mail_page.locator("div[role='document'], div[role='region']").first
                    body = body_loc.inner_text(timeout=3000) if body_loc.count() > 0 else mail_page.content()
                    m2 = re.search(r"\b(\d{6})\b", body)
                    if m2:
                        return m2.group(1), body[:200]
                    return code, txt[:200]
                except Exception as e:
                    print(f"  outlook: open err {e}", flush=True)
                    return code, txt[:200]
        print(f"  outlook: no fresh OTP, retry 8s (elapsed {int(time.time() - (deadline - max_wait))}s)", flush=True)
        time.sleep(8)
    return None, None

def get_otp(mail_page, since_ts, max_wait=180):
    """Find topmost OpenAI/ChatGPT email, click it, extract 6-digit OTP from body.

    2026-06-18: mail.com 把邮件列表 + body 都迁到 Shadow DOM,evaluate(innerText)
    返回空。改用:
      - 列表行: mf.locator("[class*='mail-item']") 枚举,text_content() 穿透 Shadow
      - 邮件正文: 实测在 frame name='detail-body-iframe' 里;直接 outerHTML 拿
        到 OTP (不能跨所有 frame 扫,否则 ad 的 siteId/mid 全是 6 位 false positive)
    `since_ts` kept for signature compat, not currently used.
    """
    if MAIL_OTP_PROVIDER == "outlook":
        return outlook_get_otp(mail_page, since_ts, max_wait=max_wait)
    if MAIL_OTP_PROVIDER != "mailcom":
        return imap_fetch_otp(since_ts, max_wait=max_wait)

    def find_body_frame():
        # mail.com 邮件正文 iframe name='detail-body-iframe'
        # (R&D mailcom-otp-extract-rnd.py 实证)
        return next(
            (
                f
                for f in mail_page.frames
                if "detail-body" in (f.name or "") or "detail-body" in (f.url or "")
            ),
            None,
        )

    def extract_otp_from_body():
        bf = find_body_frame()
        if not bf:
            return None, None
        try:
            html = bf.evaluate("() => document.documentElement.outerHTML") or ""
        except Exception:
            return None, None
        # 邮件正文里 OTP 是唯一 6 位数字。优先找 "code is" / "verification" 附近的;
        # 兜底取 outerHTML 里第一个独立 6 位 token。
        for m in re.finditer(r"\b(\d{6})\b", html):
            ctx_s = max(0, m.start() - 200)
            ctx_e = min(len(html), m.end() + 200)
            ctx = html[ctx_s:ctx_e]
            if re.search(r"code|verify|verification|login|openai|chatgpt", ctx, re.I):
                return m.group(1), ctx
        m = re.search(r"\b(\d{6})\b", html)
        if m:
            return m.group(1), html[max(0, m.start()-100):m.end()+100]
        return None, None

    deadline = time.time() + max_wait
    while time.time() < deadline:
        mf = next((fr for fr in mail_page.frames if fr.name == "mail"), None)
        if not mf:
            print("  mail frame missing, retry 5s", flush=True)
            time.sleep(5)
            continue
        try:
            rows = mf.locator("[class*='mail-item']")
            cnt = rows.count()
        except Exception as e:
            print(f"  rows count err: {e}", flush=True)
            cnt = 0
        clicked = False
        for i in range(min(cnt, 15)):
            try:
                t = (rows.nth(i).text_content(timeout=1500) or "").strip()
            except Exception:
                continue
            if not re.search(r"openai|chatgpt|noreply", t, re.I):
                continue
            print(f"  candidate row[{i}]: {t[:120]!r}", flush=True)
            row_el = rows.nth(i)
            opened = False
            try:
                row_el.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            for action_name, do_action in [
                ("dblclick", lambda: row_el.dblclick(timeout=4000)),
                ("subj-link-click", lambda: row_el.locator(":scope a, :scope [role='link'], :scope span").first.click(timeout=3000)),
                ("evaluate-dispatch", lambda: row_el.evaluate(
                    "el => { el.dispatchEvent(new MouseEvent('dblclick', {bubbles:true, cancelable:true, view:window})); }")),
            ]:
                try:
                    do_action()
                    time.sleep(3.5)
                    bf = find_body_frame()
                    if bf:
                        print(f"  ✓ {action_name} opened body frame", flush=True)
                        opened = True
                        break
                    print(f"  {action_name} no body frame yet", flush=True)
                except Exception as e:
                    print(f"  {action_name} err: {e}", flush=True)
            if opened:
                clicked = True
                break
        if clicked:
            # 等 detail-body-iframe 出现 (mail.com 异步加载邮件正文)
            bf = None
            for _ in range(10):
                bf = find_body_frame()
                if bf:
                    break
                time.sleep(1)
            print(f"  body frame: {bf.name if bf else 'NONE'} (frames_total={len(mail_page.frames)})", flush=True)
            otp, ctx = extract_otp_from_body()
            if otp:
                return otp, ctx.strip() if ctx else ""
            print("  clicked but no OTP in detail-body-iframe, continue", flush=True)
        print(f"  OTP not yet (rows={cnt}), retry in 5s...", flush=True)
        time.sleep(5)
        try:
            mf.evaluate("() => document.location.reload()")
        except Exception:
            pass
        mail_page.wait_for_timeout(3000)
    return None, None

HEADLESS = os.environ.get("HEADLESS", "0") != "0"  # default headed (CF 2026: headless 被拒,headed via Xvfb 放行)

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

    # ── PHASE 1.5: chatgpt.com login ────────────────────────────────────────
    # 2026-06-18: ChatGPT Settings → Security no longer exposes a reliable
    # Codex/device-code toggle. Do not click generic Security switches here:
    # they are MFA/session controls. The real device binding is the
    # auth.openai.com/codex/device flow below.
    print("[1.5] chatgpt.com login (no settings switch click)", flush=True)

    def _submit_form(p_page):
        """触发 React form submit: 优先 click 黑色 Continue 按钮,fallback Enter, fallback requestSubmit"""
        # find 第一个 visible black/primary Continue button (排除 social with Google/Apple/phone)
        try:
            btns = p_page.evaluate("""() => {
                return [...document.querySelectorAll('button')].filter(b => {
                    const t = (b.innerText||'').trim();
                    return /^(Continue|Sign in|Submit|Verify|Log in)$/i.test(t)
                        && !/google|apple|phone|microsoft/i.test(t)
                        && (b.type === 'submit' || b.closest('form'));
                }).map(b => {
                    const r = b.getBoundingClientRect();
                    return {text: b.innerText.trim(), type: b.type||'', x: r.x, y: r.y, w: r.width, h: r.height};
                });
            }""")
            for b in btns:
                if b['w'] > 0 and b['h'] > 0:
                    p_page.mouse.click(b['x']+b['w']/2, b['y']+b['h']/2)
                    print(f"    submit click: '{b['text']}' @ ({int(b['x']+b['w']/2)},{int(b['y']+b['h']/2)})", flush=True)
                    return
        except Exception as e:
            print(f"    submit btn dump fail: {e}", flush=True)
        # fallback: Enter
        try: p_page.keyboard.press("Enter"); print("    submit fallback: Enter", flush=True); return
        except: pass
        # last: requestSubmit
        p_page.evaluate("() => { const f=document.querySelector('form'); if (f) (f.requestSubmit?f.requestSubmit():f.submit()); }")
        print("    submit fallback: form.requestSubmit", flush=True)

    def wait_chatgpt_cf(p_page, max_wait=90):
        """chatgpt.com login picker may show a Cloudflare Turnstile gate first."""
        deadline = time.time() + max_wait
        clicked = False
        while time.time() < deadline:
            try:
                if p_page.locator("input[type='email'], input[autocomplete='username']").count() > 0:
                    return True
                title = p_page.title()
                body = p_page.content().lower()[:2000]
            except Exception:
                title, body = "", ""
            cf_present = (
                "verify you are human" in body
                or "challenges.cloudflare" in body
                or "turnstile" in body
                or "just a moment" in title.lower()
            )
            if cf_present and not clicked:
                try:
                    pos = p_page.evaluate("""() => {
                        for (const f of document.querySelectorAll('iframe')) {
                            const src = (f.src || '').toLowerCase();
                            const title = (f.title || '').toLowerCase();
                            if (src.includes('cloudflare') || src.includes('turnstile') ||
                                title.includes('challenge') || title.includes('verify')) {
                                const r = f.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    return {x: r.x, y: r.y, w: r.width, h: r.height};
                                }
                            }
                        }
                        return null;
                    }""")
                    if pos:
                        cx = pos["x"] + 30
                        cy = pos["y"] + pos["h"] / 2
                    else:
                        cx, cy = 510, 450
                    p_page.mouse.move(cx - 40, cy - 25, steps=10)
                    time.sleep(0.3)
                    p_page.mouse.move(cx, cy, steps=12)
                    time.sleep(0.3)
                    p_page.mouse.click(cx, cy)
                    clicked = True
                    print(f"    clicked chatgpt.com CF @ ({int(cx)},{int(cy)})", flush=True)
                except Exception as e:
                    print(f"    chatgpt.com CF click failed: {e}", flush=True)
            time.sleep(2)
        return p_page.locator("input[type='email'], input[autocomplete='username']").count() > 0

    def fill_login_and_otp(p_page, need_pwd=True):
        """复用 email→password→OTP 步骤"""
        wait_chatgpt_cf(p_page)
        p_page.wait_for_selector("input[type='email'], input[autocomplete='username']", timeout=20000)
        p_page.locator("input[type='email']").first.click()
        p_page.keyboard.type(EMAIL, delay=80)
        _submit_form(p_page)
        time.sleep(5)
        print(f"    after email submit url={p_page.url[:100]}", flush=True)
        if need_pwd:
            for _ in range(15):
                if "password" in p_page.url.lower() or "passkey" in p_page.url.lower(): break
                time.sleep(1)
            # passkey challenge → click through to password
            if "passkey" in p_page.url.lower() or "auth_challenge" in p_page.url.lower():
                print(f"    passkey challenge detected, switching to password...", flush=True)
                try:
                    alt = p_page.locator("a, button").filter(has_text=re.compile(r"password|another.*(way|method)", re.I))
                    if alt.count() > 0:
                        alt.first.click()
                        time.sleep(4)
                        print(f"    after passkey bypass url={p_page.url[:100]}", flush=True)
                except Exception as e:
                    print(f"    passkey bypass failed: {e}", flush=True)
            try:
                p_page.wait_for_selector("input[type='password']", timeout=15000)
                p_page.locator("input[type='password']").first.click()
                p_page.keyboard.type(CHATGPT_PW, delay=80)
                ss(p_page, "p15a2-pw-filled")
                _submit_form(p_page)
                time.sleep(6)
                print(f"    after pw submit url={p_page.url[:100]}", flush=True)
            except Exception as e:
                print(f"    password step skipped: {e}", flush=True)
        if "email-verification" in p_page.url or "verification" in p_page.content().lower()[:5000]:
            print("  need OTP - fetching from mail.com", flush=True)
            mp = mailcom_login(ctx)
            otp, _ = get_otp(mp, int(time.time()) - 600)
            if mp is not None:
                mp.close()
            if otp:
                print(f"  ✅ OTP={otp}", flush=True)
                p_page.locator("input").first.click()
                p_page.keyboard.type(otp, delay=80)
                _submit_form(p_page)
                time.sleep(8)
                print(f"    after OTP submit url={p_page.url[:100]}", flush=True)
            else:
                print("  ⚠ OTP fetch failed", flush=True)
        else:
            # retry: 等 url 变 email-verification (sleep 6 后可能还没 redirect)
            for _ in range(15):
                if "email-verification" in p_page.url or "verification" in p_page.content().lower()[:3000]:
                    print("  need OTP (after retry) - fetching from mail.com", flush=True)
                    mp = mailcom_login(ctx)
                    otp, _ = get_otp(mp, int(time.time()) - 600)
                    if mp is not None:
                        mp.close()
                    if otp:
                        print(f"  ✅ OTP={otp}", flush=True)
                        p_page.locator("input").first.click()
                        p_page.keyboard.type(otp, delay=80)
                        _submit_form(p_page)
                        time.sleep(8)
                        print(f"    after OTP submit url={p_page.url[:100]}", flush=True)
                    else:
                        print("  ⚠ OTP fetch failed", flush=True)
                    break
                time.sleep(1)

    chat_page = ctx.new_page()
    chat_page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded")
    time.sleep(3)
    for _ in range(30):
        t = chat_page.title()
        if t and "moment" not in t.lower(): break
        time.sleep(2)
    ss(chat_page, "p15a-chatgpt-picker")
    fill_login_and_otp(chat_page)
    for i in range(40):
        time.sleep(1)
        u = chat_page.url
        if "chatgpt.com" in u and "/auth" not in u and "/login" not in u:
            break
    print(f"  after chatgpt.com login url={chat_page.url[:100]}", flush=True)
    ss(chat_page, "p15b-chatgpt-logged-in")
    logged = "chatgpt.com" in chat_page.url and "/auth" not in chat_page.url
    if logged:
        print("  chatgpt.com login ok; enabling Codex device-code toggle...", flush=True)
        try:
            chat_page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=45000)
            time.sleep(7)
            ss(chat_page, "p15c-security")
            # scroll panel to bottom so all switches are loaded
            try:
                chat_page.evaluate("""() => {
                    const nodes = [...document.querySelectorAll('*')].filter(el => {
                        const s = getComputedStyle(el);
                        return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 20;
                    });
                    nodes.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                    if (nodes[0]) nodes[0].scrollTop = nodes[0].scrollHeight;
                }""")
                time.sleep(2)
            except Exception:
                pass
            switches = chat_page.locator("button[role='switch']")
            exact_re = re.compile(r"codex|device\s*code|device-code|device authorization|device auth|设备代码|设备授权|设备码", re.I)
            reject_re = re.compile(r"mfa|authenticator|text message|password|passkey|security key|session|多因素|身份验证|短信|密码|通行密钥|安全密钥|会话|受信任设备|活跃会话", re.I)
            target_sw = None
            for idx in range(switches.count()):
                sw = switches.nth(idx)
                try:
                    label = sw.evaluate("""el => {
                        const parts = [];
                        let p = el;
                        for (let i = 0; i < 5; i++) {
                            if (!p) break;
                            const text = (p.innerText || '').trim();
                            if (text) parts.push(text);
                            p = p.parentElement;
                        }
                        return parts.join('\\n---\\n');
                    }""")
                    if exact_re.search(label) and not reject_re.search(label):
                        target_sw = sw
                        print(f"  matched Codex switch idx={idx} aria={sw.get_attribute('aria-checked')}", flush=True)
                        break
                except Exception:
                    pass
            if target_sw is None:
                print("  ⚠ Codex toggle not found in Security panel", flush=True)
            else:
                before = target_sw.get_attribute("aria-checked")
                if before != "true":
                    target_sw.click(force=True)
                    time.sleep(5)
                after = target_sw.get_attribute("aria-checked")
                print(f"  Codex toggle: {before} → {after}", flush=True)
                ss(chat_page, "p15d-toggle-set")
        except Exception as e:
            print(f"  ⚠ toggle step exception: {e}", flush=True)
    else:
        print("  ❌ chatgpt.com 未登录; proceeding to device flow may require full login", flush=True)
    chat_page.close()
    print("[1.5] done — proceeding to OAuth device flow", flush=True)

    # ── 2a. Navigate to verify page (会跳转到 /log-in) ──────────────────
    print("[2] Open auth.openai.com/codex/device...", flush=True)
    page.goto(f"{AUTH_BASE}/codex/device", wait_until="domcontentloaded")
    # Wait through CF Turnstile (2026: 强制要求 user click checkbox,patchright TLS 指纹不够)
    deadline = time.time() + 90
    clicked_cf = False
    dumped = False
    while time.time() < deadline:
        title = page.title()
        body_head = page.content().lower()[:1500]
        if title and "moment" not in title.lower() and "performing security" not in body_head:
            break
        # 第一次进入时 dump 所有 iframe 帮诊断
        elapsed = time.time() - (deadline - 90)
        if not dumped and elapsed > 5:
            try:
                frames_info = page.evaluate("""() => {
                    return [...document.querySelectorAll('iframe')].map(f => {
                        const r = f.getBoundingClientRect();
                        return {src: f.src||'', title: f.title||'', id: f.id||'', name: f.name||'',
                                x: r.x, y: r.y, w: r.width, h: r.height};
                    });
                }""")
                print(f"  IFRAMES ({len(frames_info)}):", flush=True)
                for fi in frames_info:
                    print(f"    src={fi['src'][:80]!r} title={fi['title']!r} id={fi['id']!r} box={int(fi['w'])}x{int(fi['h'])}", flush=True)
                dumped = True
            except Exception as e:
                print(f"  iframe dump failed: {e}", flush=True)
        # 主动 click CF Turnstile checkbox - 扫所有 iframe,找 cloudflare/challenges/turnstile 标记
        if not clicked_cf and elapsed > 8:
            try:
                pos = page.evaluate("""() => {
                    const iframes = [...document.querySelectorAll('iframe')];
                    for (const f of iframes) {
                        const src = (f.src||'').toLowerCase();
                        const title = (f.title||'').toLowerCase();
                        if (src.includes('cloudflare') || src.includes('challenges') ||
                            src.includes('turnstile') || title.includes('cloudflare') ||
                            title.includes('challenge') || title.includes('verify')) {
                            const r = f.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                return {x: r.x, y: r.y, w: r.width, h: r.height,
                                        src: f.src, title: f.title};
                            }
                        }
                    }
                    return null;
                }""")
                if pos:
                    cx = pos['x'] + 30
                    cy = pos['y'] + pos['h'] / 2
                    print(f"  found CF iframe: title={pos['title']!r} box={int(pos['w'])}x{int(pos['h'])} @ ({int(pos['x'])},{int(pos['y'])})", flush=True)
                    # 模拟真人:先 hover 再 click
                    page.mouse.move(cx - 50, cy - 30, steps=10)
                    time.sleep(0.3)
                    page.mouse.move(cx, cy, steps=15)
                    time.sleep(0.4)
                    page.mouse.click(cx, cy)
                    clicked_cf = True
                    print(f"  ✓ clicked CF Turnstile @ ({int(cx)},{int(cy)})", flush=True)
                    time.sleep(3)
                    continue
                else:
                    if not clicked_cf:
                        # 兜底:无 iframe 命中,用屏幕坐标基于截图位置点击(checkbox ~210,335)
                        print(f"  no CF iframe matched,fallback to fixed coords (210, 335)", flush=True)
                        page.mouse.move(160, 305, steps=10)
                        time.sleep(0.3)
                        page.mouse.move(210, 335, steps=15)
                        time.sleep(0.4)
                        page.mouse.click(210, 335)
                        clicked_cf = True
                        time.sleep(3)
                        continue
            except Exception as e:
                print(f"  CF click attempt failed: {e}", flush=True)
        print(f"  [{int(deadline-time.time())}s] waiting CF... title={repr(title[:30])}", flush=True)
        time.sleep(3)
    ss(page, "01-after-cf")
    print(f"  url={page.url}  title={page.title()[:40]}", flush=True)

    # ── 2b. Fill email (Welcome back 登录页) — OR skip if session 已有 (/choose-an-account) ──
    print(f"[3] Fill email: {EMAIL}", flush=True)
    # Detect /choose-an-account 页 (session 已建立, 不需要重新登)
    if "choose-an-account" in page.url or "choose" in page.url.lower():
        print("  ✓ session 已建立 (/choose-an-account 页) - click account 跳过 email/password/OTP", flush=True)
        ss(page, "02-choose-account")
        try:
            # click 第一个 account button (only one account in this ctx)
            acc_btn = page.locator(f"button:has-text('{EMAIL}'), button:has-text('analeah'), [role='button']:has-text('{EMAIL.split('@')[0]}')")
            if acc_btn.count() == 0:
                # fallback: any clickable button containing email username
                acc_btn = page.locator("button, [role='button']").filter(has_text=EMAIL.split('@')[0])
            if acc_btn.count() > 0:
                acc_btn.first.click()
                print(f"  ✓ clicked account button", flush=True)
            else:
                # last fallback: first button on page
                page.locator("button").first.click()
                print("  ⚠ fallback: clicked first button", flush=True)
        except Exception as e:
            print(f"  account click failed: {e}", flush=True)
        time.sleep(5)
        print(f"  after account click url={page.url[:100]}", flush=True)
        ss(page, "03-after-account")
    else:
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

    # ── 2c. Fill password (字段B) — OR skip if session 已建立 ─────────────
    # passkey challenge → click "Try another way" → password page
    if "passkey" in page.url.lower() or "auth_challenge" in page.url.lower():
        print(f"[3.5] Passkey challenge detected, clicking 'Try another way'...", flush=True)
        before_url = page.url
        clicked = False
        for strat in ("role-button", "role-link", "text-exact", "text-regex"):
            try:
                if strat == "role-button":
                    loc = page.get_by_role("button", name=re.compile(r"try another way", re.I))
                elif strat == "role-link":
                    loc = page.get_by_role("link", name=re.compile(r"try another way", re.I))
                elif strat == "text-exact":
                    loc = page.locator("text=Try another way")
                else:
                    loc = page.get_by_text(re.compile(r"try another way", re.I))
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    clicked = True
                    print(f"  ✓ clicked Try another way via {strat}", flush=True)
                    break
            except Exception as e:
                print(f"  {strat} failed: {e}", flush=True)
        if clicked:
            for _ in range(15):
                time.sleep(1)
                if page.url != before_url:
                    break
            ss(page, "03b-passkey-bypass")
            print(f"  url after passkey bypass={page.url}", flush=True)
        else:
            print(f"  ⚠ no Try another way control found", flush=True)
            ss(page, "03b-passkey-bypass-noclick")
    if "password" in page.url.lower():
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
    else:
        print(f"[4] password step skipped (url={page.url[:80]})", flush=True)

    # ── 2d. OTP if needed ────────────────────────────────────────────────
    body_text = page.content().lower()
    # NB: the /add-phone page text also contains "one-time code"/"we'll send",
    # which would falsely trigger the email-OTP detour. Exclude it explicitly —
    # /add-phone is handled by the manual-handoff block below.
    phone_challenge_url = any(p in page.url for p in ("/add-phone", "/phone-verification"))
    need_otp = (not phone_challenge_url) and any(k in body_text for k in (
        "verification code", "one-time", "verify your email", "check your email",
        "enter the code", "we sent", "enter code",
    ))
    print(f"[5] Need OTP: {need_otp}", flush=True)
    if need_otp:
        print("  Logging into mail.com 字段A...", flush=True)
        SCRIPT_START = int(time.time())  # passed to get_otp for staleness check
        mail_page = mailcom_login(ctx)
        otp, ctx_snip = get_otp(mail_page, SCRIPT_START)
        if mail_page is not None:
            mail_page.close()
        if not otp:
            ss(page, "06-otp-timeout")
            sys.exit("❌ No OTP within 120s")
        print(f"  ✅ OTP: {otp}", flush=True)
        page.bring_to_front()
        # Prefer dedicated code inputs by priority (avoid grabbing a country-code
        # search box inside a react-aria <Select> via a broad union .first).
        otp_input = None
        for _sel in ("input[autocomplete='one-time-code']", "input[name='code']",
                     "input[inputmode='numeric']", "input[type='tel']"):
            _loc = page.locator(_sel)
            if _loc.count() > 0 and _loc.first.is_visible():
                otp_input = _loc.first
                break
        if otp_input is None:
            _vis = [i for i in page.locator("input").all() if i.is_visible()]
            otp_input = _vis[0] if _vis else None
        if otp_input is None:
            ss(page, "06-no-otp-input")
            sys.exit("❌ no OTP input found on verification page")
        # Focus via JS (no pointer click → not blocked by a react-aria overlay)
        # then keyboard.type to keep keyup events that enable the Continue button.
        try:
            otp_input.evaluate("el => el.focus()")
        except Exception:
            pass
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

    # ── 2d-phone. "Phone number required" risk-control challenge ─────────────
    # OpenAI sometimes forces phone binding (/add-phone) before re-issuing a
    # token. If PHONE_NUMBER + SMS_API_URL are set, fill the phone, then poll
    # the virtual-number SMS inbox for the 6-digit code. If they're not set,
    # screenshot and hand off to a human.
    pc = page.content().lower()
    if any(p in page.url for p in ("/add-phone", "/phone-verification")) or ("phone number" in pc and any(k in pc for k in (
            "add your phone", "phone number required", "we'll send", "we will send",
            "verify it", "one-time code"))):
        print("[5a] 'Phone number required' page detected", flush=True)
        ss(page, "5a0-phone-required")
        print(f"  url={page.url}", flush=True)
        if not PHONE_NUMBER or not SMS_API_URL:
            print("  ⏸ no PHONE_NUMBER/SMS_API_URL — MANUAL handoff", flush=True)
            sys.exit("⏸ MANUAL_PHONE_REQUIRED")
        sms_baseline = _sms_codes(_sms_fetch())
        print(f"  sms baseline codes: {sms_baseline}", flush=True)
        # phone field is the tel input (country-code <Select> defaults to US +1)
        phone_in = None
        tel = page.locator("input[type='tel']")
        if tel.count() > 0:
            phone_in = tel.first
        else:
            vis = [i for i in page.locator("input").all() if i.is_visible()]
            phone_in = vis[-1] if vis else None
        if phone_in is None:
            ss(page, "5a1-no-phone-input")
            sys.exit("❌ no phone input found on phone-required page")
        # Focus via JS (no pointer click → not blocked by react-aria overlay)
        try:
            phone_in.evaluate("el => el.focus()")
        except Exception:
            pass
        page.keyboard.type(PHONE_NUMBER, delay=80)
        ss(page, "5a2-phone-filled")
        cb = page.get_by_role("button", name="Continue", exact=True)
        if cb.count() > 0 and cb.first.is_enabled():
            cb.first.click()
        else:
            page.keyboard.press("Enter")
        time.sleep(6)
        ss(page, "5a3-after-phone")
        print(f"  url={page.url}", flush=True)
        pc2 = page.content().lower()
        if "not valid" in pc2 or "invalid" in pc2:
            ss(page, "5a3b-phone-invalid")
            sys.exit(f"❌ phone rejected as invalid: {PHONE_NUMBER}")
        print("  polling SMS api for OTP...", flush=True)
        sms_otp = poll_sms_otp(sms_baseline, timeout=150)
        if not sms_otp:
            ss(page, "5a4-sms-timeout")
            sys.exit("❌ no new SMS OTP within 150s")
        print(f"  ✅ SMS OTP: {sms_otp}", flush=True)
        otp_in2 = None
        for _sel in ("input[autocomplete='one-time-code']", "input[name='code']",
                     "input[inputmode='numeric']", "input[type='tel']"):
            _loc = page.locator(_sel)
            if _loc.count() > 0 and _loc.first.is_visible():
                otp_in2 = _loc.first
                break
        if otp_in2 is None:
            _vis = [i for i in page.locator("input").all() if i.is_visible()]
            otp_in2 = _vis[0] if _vis else None
        try:
            otp_in2.evaluate("el => el.focus()")
        except Exception:
            pass
        page.keyboard.type(sms_otp, delay=80)
        ss(page, "5a5-sms-filled")
        cb2 = page.get_by_role("button", name="Continue", exact=True)
        if cb2.count() > 0 and cb2.first.is_enabled():
            cb2.first.click()
        else:
            page.keyboard.press("Enter")
        for _ in range(30):
            if "add-phone" not in page.url.lower() and "phone" not in page.url.lower():
                break
            time.sleep(1)
        ss(page, "5a6-after-sms")
        print(f"  url={page.url}", flush=True)

    # ── 2d2. OAuth consent page: "Sign in to Codex with ChatGPT" → Continue ──
    if "/consent" in page.url or "codex/consent" in page.url:
        print("[5b] OAuth consent page — clicking Continue...", flush=True)
        # consent button is "Continue" (dark button)
        consent_btn = page.locator("button:has-text('Continue'), button:has-text('Allow'), button:has-text('Authorize')")

        if consent_btn.count() > 0 and not consent_btn.first.is_enabled():
            ss(page, "07a-consent-disabled")
            try:
                txt = page.evaluate("() => document.body.innerText")[:3000]
                print(f"  consent body text:\n{txt}", flush=True)
            except Exception:
                pass
            sys.exit("❌ consent Continue disabled; not clicking Security/MFA switches")

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
