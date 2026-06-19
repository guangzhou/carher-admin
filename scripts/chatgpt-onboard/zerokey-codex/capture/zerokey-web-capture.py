#!/usr/bin/env python3
"""
zerokey-web-capture.py — Log into chatgpt.com on 188 and capture a real
/backend-api/f/conversation POST (headers + cookies + body) so zerokey can
replay the web-chat session as an OpenAI-compatible API.

WHY this exists (read before re-debugging):
  - zerokey's ChatGPT provider replays a captured browser request. It REQUIRES
    the `openai-sentinel-proof-token` request header (it decodes it for the real
    UA + POW config). A bare OAuth token is NOT enough — we need the full
    browser request incl. cf_clearance cookie, which is bound to the 188 egress
    IP. Hence capture MUST run on 188 (JP exit), same host where zerokey runs.
  - CF on chatgpt.com requires patchright (real Chrome TLS) + headed (Xvfb).
    Login flow reused from chatgpt-litellm-oauth.py Phase 1.5.

ENV:
  MAIL_USER           kristine_free517@mail.com
  MAIL_LOGIN_PW_FILE  /run/mail_pw.txt      (webmail password, for OTP)
  CHATGPT_PW_FILE     /run/chatgpt_pw.txt   (ChatGPT login password)
  OUT_JSON            /work/out/zerokey-users.json   (zerokey temp/users.json)
  ZK_USER             username key inside users.json (default: kristine)
  SCREENSHOT_DIR      /work/screenshots
  CAPTURE_PROMPT      message to send to trigger the request (default "hi")

OUTPUT (OUT_JSON), zerokey temp/users.json shape:
  { "chatgpt": { "<ZK_USER>": {
      "username": "<ZK_USER>",
      "parsedFetch": { "url": "...", "method": "POST", "headers": {...}, "body": {...} },
      "sessions": [] } } }
"""

import os, re, sys, json, time
from patchright.sync_api import sync_playwright

EMAIL      = os.environ["MAIL_USER"]
MAIL_PW    = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
CHATGPT_PW = open(os.environ["CHATGPT_PW_FILE"]).read().strip()
SS_DIR     = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
OUT_JSON   = os.environ.get("OUT_JSON", "/work/out/zerokey-users.json")
ZK_USER    = os.environ.get("ZK_USER", "kristine")
PROMPT     = os.environ.get("CAPTURE_PROMPT", "hi")
OTP_FILE   = os.environ.get("OTP_FILE", "/work/out/otp.txt")
OTP_FILE_WAIT = int(os.environ.get("OTP_FILE_WAIT", "600"))
OTP_AUTO_ONLY = os.environ.get("OTP_AUTO_ONLY", "0") == "1"
OTP_AUTO_MAX = int(os.environ.get("OTP_AUTO_MAX", "240"))
OTP_RE = re.compile(r"\b(\d{6})\b")
SENDER_HINTS_RE = re.compile(r"openai|chatgpt|noreply", re.I)

os.makedirs(SS_DIR, exist_ok=True)


def ss(page, name):
    try:
        page.screenshot(path=f"{SS_DIR}/{name}.png", full_page=False)
        print(f"  shot: {SS_DIR}/{name}.png", flush=True)
    except Exception as e:
        print(f"  shot fail: {e}", flush=True)


# ── mail.com OTP (ported from chatgpt-litellm-oauth.py) ───────────────────
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
    for _ in range(30):
        if "navigator" in p.url:
            break
        time.sleep(1)
    if "navigator" not in p.url:
        ss(p, "mailcom-fail")
        print("  mail.com login may have failed url=" + p.url, flush=True)
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
                loc.first.click()
                p.wait_for_timeout(2000)
        except Exception:
            pass
    # wait for inbox content to actually render (mail.com shows a skeleton
    # screen first; poll frames for a real sender keyword, force-reload to
    # break skeleton stall) — ported from chatgpt-litellm-oauth.py
    SENDER_RE = re.compile(r"(openai|chatgpt|noreply@tm\.openai|noreply@)", re.I)
    loaded = False
    for attempt in range(45):
        for fr in p.frames:
            try:
                txt = fr.evaluate("() => document.body.innerText")
            except Exception:
                continue
            if not txt or len(txt) < 200:
                continue
            if SENDER_RE.search(txt):
                print(f"  mail.com: inbox loaded ({len(txt)} chars)", flush=True)
                loaded = True
                break
        if loaded:
            break
        if attempt > 0 and attempt % 10 == 0:
            print(f"  mail.com: skeleton stall — reload (attempt {attempt})", flush=True)
            try:
                p.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
        print(f"  mail.com: waiting inbox... [{attempt+1}/45]", flush=True)
        time.sleep(2)
    if not loaded:
        print("  mail.com: WARN inbox keyword never appeared — proceeding anyway", flush=True)
    ss(p, "mailcom-inbox")
    return p


def find_mail_frame(page):
    """Return mail.com inbox iframe (name=mail), polling up to ~25s."""
    deadline = time.time() + 25
    while time.time() < deadline:
        for fr in page.frames:
            if fr.name == "mail":
                return fr
        time.sleep(2)
    return None


def extract_otp_from_open_mail(mail_frame, page):
    """Extract 6-digit OTP from opened message body (skip inbox list frame)."""
    texts = []
    for fr in page.frames:
        try:
            if fr.name == "mail":
                continue
            texts.append(fr.evaluate("() => document.body.innerText"))
        except Exception:
            pass
    try:
        texts.append(mail_frame.evaluate("() => document.body.innerText"))
    except Exception:
        pass
    for text in texts:
        if not SENDER_HINTS_RE.search(text) and "code" not in text.lower():
            continue
        m = OTP_RE.search(text)
        if m:
            return m.group(1)
    return None


def get_otp(mail_page, max_wait=None):
    """Poll mail.com inbox for OpenAI OTP — fully automated (chatgpt-login-session pattern)."""
    if max_wait is None:
        max_wait = OTP_AUTO_MAX
    deadline = time.time() + max_wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        frame = find_mail_frame(mail_page)
        if frame:
            try:
                text = frame.evaluate("() => document.body.innerText")
            except Exception:
                text = ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for ln in lines:
                if not SENDER_HINTS_RE.search(ln):
                    continue
                m = OTP_RE.search(ln)
                if m:
                    print(f"  OTP found in inbox list (attempt {attempt})", flush=True)
                    return m.group(1)
                try:
                    frame.get_by_text(ln, exact=False).first.click(timeout=5000)
                    time.sleep(5)
                    ss(mail_page, "mailcom-message-opened")
                    code = extract_otp_from_open_mail(frame, mail_page)
                    if code:
                        print(f"  OTP found in opened mail (attempt {attempt})", flush=True)
                        return code
                except Exception:
                    pass
        # legacy frame scan fallback
        for fr in mail_page.frames:
            try:
                text = fr.evaluate("() => document.body.innerText")
            except Exception:
                continue
            if not text or len(text) < 50:
                continue
            if not SENDER_HINTS_RE.search(text):
                continue
            for m in OTP_RE.finditer(text):
                ctx = text[max(0, m.start() - 100): m.start() + 100]
                if re.search(r"code|verify|openai|login", ctx, re.I):
                    print(f"  OTP found via frame scan (attempt {attempt})", flush=True)
                    return m.group(1)
        print(f"  OTP not yet, retry in 5s... (attempt {attempt})", flush=True)
        time.sleep(5)
        try:
            mail_page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        mail_page.wait_for_timeout(3000)
    return None


# ── chatgpt.com login helpers (ported) ────────────────────────────────────
def submit_form(p):
    try:
        btns = p.evaluate("""() => {
            return [...document.querySelectorAll('button')].filter(b => {
                const t = (b.innerText||'').trim();
                return /^(Continue|Sign in|Submit|Verify|Log in)$/i.test(t)
                    && !/google|apple|phone|microsoft/i.test(t)
                    && (b.type === 'submit' || b.closest('form'));
            }).map(b => { const r=b.getBoundingClientRect();
                return {text:b.innerText.trim(), x:r.x, y:r.y, w:r.width, h:r.height}; });
        }""")
        for b in btns:
            if b["w"] > 0 and b["h"] > 0:
                p.mouse.click(b["x"] + b["w"] / 2, b["y"] + b["h"] / 2)
                print(f"    submit click: '{b['text']}'", flush=True)
                return
    except Exception as e:
        print(f"    submit dump fail: {e}", flush=True)
    try:
        p.keyboard.press("Enter")
        return
    except Exception:
        pass
    p.evaluate("() => { const f=document.querySelector('form'); if(f)(f.requestSubmit?f.requestSubmit():f.submit()); }")


def wait_cf(p, max_wait=90):
    deadline = time.time() + max_wait
    clicked = False
    while time.time() < deadline:
        try:
            if p.locator("input[type='email'], input[autocomplete='username']").count() > 0:
                return True
            title = p.title()
            body = p.content().lower()[:2000]
        except Exception:
            title, body = "", ""
        cf = ("verify you are human" in body or "challenges.cloudflare" in body
              or "turnstile" in body or "just a moment" in title.lower())
        if cf and not clicked:
            try:
                pos = p.evaluate("""() => {
                    for (const f of document.querySelectorAll('iframe')) {
                        const s=(f.src||'').toLowerCase(), t=(f.title||'').toLowerCase();
                        if (s.includes('cloudflare')||s.includes('turnstile')||t.includes('challenge')||t.includes('verify')){
                            const r=f.getBoundingClientRect();
                            if(r.width>0&&r.height>0) return {x:r.x,y:r.y,w:r.width,h:r.height};
                        }
                    } return null; }""")
                cx, cy = (pos["x"] + 30, pos["y"] + pos["h"] / 2) if pos else (510, 450)
                p.mouse.move(cx - 40, cy - 25, steps=10); time.sleep(0.3)
                p.mouse.move(cx, cy, steps=12); time.sleep(0.3)
                p.mouse.click(cx, cy)
                clicked = True
                print(f"    clicked CF @ ({int(cx)},{int(cy)})", flush=True)
            except Exception as e:
                print(f"    CF click failed: {e}", flush=True)
        time.sleep(2)
    return p.locator("input[type='email'], input[autocomplete='username']").count() > 0


def clear_cf(page, max_wait=90):
    """On chatgpt.com app pages a Cloudflare Turnstile checkbox may gate access.
    Click it and wait until the challenge clears. Returns True if cleared/absent."""
    deadline = time.time() + max_wait
    clicked = 0
    while time.time() < deadline:
        try:
            body = page.content().lower()[:3000]
            title = page.title().lower()
        except Exception:
            body, title = "", ""
        cf = ("verify you are human" in body or "challenges.cloudflare" in body
              or "turnstile" in body or "just a moment" in title)
        if not cf:
            return True
        try:
            pos = page.evaluate("""() => {
                for (const f of document.querySelectorAll('iframe')) {
                    const s=(f.src||'').toLowerCase(), t=(f.title||'').toLowerCase();
                    if (s.includes('cloudflare')||s.includes('turnstile')||t.includes('challenge')||t.includes('verify')){
                        const r=f.getBoundingClientRect();
                        if(r.width>0&&r.height>0) return {x:r.x,y:r.y,w:r.width,h:r.height};
                    }
                } return null; }""")
            cx, cy = (pos["x"] + 30, pos["y"] + pos["h"] / 2) if pos else (408, 360)
            page.mouse.move(cx - 40, cy - 25, steps=10); time.sleep(0.3)
            page.mouse.move(cx, cy, steps=12); time.sleep(0.3)
            page.mouse.click(cx, cy)
            clicked += 1
            print(f"    clear_cf: clicked turnstile @ ({int(cx)},{int(cy)}) [{clicked}]", flush=True)
        except Exception as e:
            print(f"    clear_cf click err: {e}", flush=True)
        time.sleep(3)
    return False


def is_logged_in(page):
    try:
        li = page.locator("button:has-text('Log in'), a:has-text('Log in'), button:has-text('Sign up for free')")
        if li.count() > 0 and li.first.is_visible():
            return False
    except Exception:
        pass
    return True


def login_chatgpt(ctx, page):
    page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded")
    time.sleep(3)
    # there may be an intermediate "Log in" / "Stay logged out" button
    for sel in ["button:has-text('Log in')", "a:has-text('Log in')",
                "[data-testid='login-button']"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                time.sleep(3)
                break
        except Exception:
            pass
    wait_cf(page)
    page.wait_for_selector("input[type='email'], input[autocomplete='username']", timeout=30000)
    page.locator("input[type='email']").first.click()
    page.keyboard.type(EMAIL, delay=80)
    submit_form(page)
    time.sleep(5)
    print(f"    after email url={page.url[:100]}", flush=True)
    for _ in range(15):
        if "password" in page.url.lower() or "passkey" in page.url.lower():
            break
        time.sleep(1)
    if "passkey" in page.url.lower() or "auth_challenge" in page.url.lower():
        try:
            alt = page.locator("a, button").filter(has_text=re.compile(r"password|another.*(way|method)", re.I))
            if alt.count() > 0:
                alt.first.click()
                time.sleep(4)
        except Exception:
            pass
    try:
        page.wait_for_selector("input[type='password']", timeout=15000)
        page.locator("input[type='password']").first.click()
        page.keyboard.type(CHATGPT_PW, delay=80)
        ss(page, "pw-filled")
        submit_form(page)
        time.sleep(6)
        print(f"    after pw url={page.url[:100]}", flush=True)
    except Exception as e:
        print(f"    password step skipped: {e}", flush=True)

    need_otp = "verification" in page.url or "verification" in page.content().lower()[:5000]
    if not need_otp:
        for _ in range(15):
            if "verification" in page.url or "verification" in page.content().lower()[:3000]:
                need_otp = True
                break
            time.sleep(1)
    if need_otp:
        print("  need OTP - mail.com auto (OTP_AUTO_ONLY=%s)" % OTP_AUTO_ONLY, flush=True)
        otp = None
        try:
            mp = mailcom_login(ctx)
            otp = get_otp(mp)
            try:
                mp.close()
            except Exception:
                pass
        except Exception as e:
            print(f"  mail.com auto error: {e}", flush=True)
        if not otp and not OTP_AUTO_ONLY:
            # file fallback when not in strict auto mode
            try:
                if os.path.exists(OTP_FILE):
                    os.remove(OTP_FILE)
            except Exception:
                pass
            print(f"  >>> OTP_WAIT_FILE: write the 6-digit code to {OTP_FILE} (waiting up to {OTP_FILE_WAIT}s)", flush=True)
            deadline = time.time() + OTP_FILE_WAIT
            while time.time() < deadline:
                try:
                    if os.path.exists(OTP_FILE):
                        v = open(OTP_FILE).read().strip()
                        m = re.search(r"\d{6}", v)
                        if m:
                            otp = m.group(0)
                            print(f"  got OTP from file: {otp}", flush=True)
                            break
                except Exception:
                    pass
                time.sleep(3)
        elif not otp and OTP_AUTO_ONLY:
            print("  OTP auto failed (OTP_AUTO_ONLY=1, no manual fallback)", flush=True)
        if otp:
            print(f"  OTP={otp}", flush=True)
            page.locator("input").first.click()
            page.keyboard.type(otp, delay=80)
            submit_form(page)
            time.sleep(8)
            print(f"    after OTP url={page.url[:100]}", flush=True)
            ss(page, "otp-submitted")
            # wait for the auth→chatgpt.com session callback to fully complete,
            # otherwise navigating away lands us in anonymous (logged-out) mode
            for _ in range(40):
                u = page.url
                if "chatgpt.com" in u and "auth" not in u and "verification" not in u:
                    break
                # click any post-OTP continue / stay-signed-in prompts
                for sel in ["button:has-text('Continue')", "button:has-text('Yes')",
                            "button:has-text('Stay signed in')",
                            "button:has-text('Verify')", "[data-testid='continue-button']"]:
                    try:
                        loc = page.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click()
                            time.sleep(2)
                    except Exception:
                        pass
                time.sleep(2)
            print(f"    post-OTP settled url={page.url[:100]}", flush=True)
            ss(page, "post-otp-settled")
            # late-cookie: the chatgpt.com session can land a few seconds after the
            # OAuth callback; reload a few times before treating it as anonymous.
            for r in range(4):
                if is_logged_in(page):
                    break
                print(f"    post-OTP not logged-in yet, reload {r+1}/4", flush=True)
                try:
                    page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                    time.sleep(5)
                    clear_cf(page)
                    time.sleep(3)
                except Exception:
                    pass
            print(f"    post-OTP login state={is_logged_in(page)} url={page.url[:80]}", flush=True)
        else:
            print("  OTP fetch failed", flush=True)


# ── main: login → send message → capture f/conversation request ───────────
captured = {"done": False, "data": None}


def main():
    PROFILE_DIR = os.environ.get("PROFILE_DIR", "/work/profile")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        time.sleep(5)
        clear_cf(page)
        time.sleep(2)
        logged_in = True
        try:
            li = page.locator("button:has-text('Log in'), a:has-text('Log in')")
            if li.count() > 0 and li.first.is_visible():
                logged_in = False
        except Exception:
            pass
        if not logged_in:
            print("[1] not logged in → running login flow", flush=True)
            login_chatgpt(ctx, page)
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            time.sleep(5)
            clear_cf(page)
            time.sleep(2)
        else:
            print("[1] reusing persisted session (already logged in)", flush=True)

        # verify logged-in; if anonymous, trigger silent SSO (auth cookie exists,
        # no OTP needed) by clicking Log in and waiting for redirect back
        if not is_logged_in(page):
            print("[1b] still anonymous → silent SSO via Log in", flush=True)
            for attempt in range(3):
                try:
                    lg = page.locator("button:has-text('Log in'), a:has-text('Log in')")
                    if lg.count() > 0:
                        lg.first.click()
                        time.sleep(4)
                        clear_cf(page)
                        # may show an account chooser / continue
                        for sel in ["button:has-text('Continue')",
                                    f"button:has-text('{EMAIL}')",
                                    "[data-testid='continue-button']"]:
                            try:
                                loc = page.locator(sel)
                                if loc.count() > 0 and loc.first.is_visible():
                                    loc.first.click()
                                    time.sleep(3)
                            except Exception:
                                pass
                except Exception as e:
                    print(f"    sso click err: {e}", flush=True)
                for _ in range(20):
                    if "chatgpt.com" in page.url and "auth" not in page.url:
                        break
                    time.sleep(2)
                if is_logged_in(page):
                    print("[1b] SSO success — now logged in", flush=True)
                    break
                # if SSO bounced to full login, run the password+OTP flow
                if "auth.openai.com" in page.url or "/auth/login" in page.url:
                    print("[1b] SSO needs full login → running login flow", flush=True)
                    login_chatgpt(ctx, page)
                    page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                    time.sleep(5)
                    clear_cf(page)
                    if is_logged_in(page):
                        break
        ss(page, "app-loaded")
        print(f"[1] logged_in={is_logged_in(page)} url={page.url[:80]}", flush=True)

        # attach request capture for the REAL conversation POST (not /prepare)
        def on_request(req):
            try:
                u = req.url
                if req.method == "POST" and "/backend-api/" in u:
                    print(f"  [POST] {u}", flush=True)
                path = u.split("?")[0].rstrip("/")
                is_conv = req.method == "POST" and (
                    path.endswith("/backend-api/f/conversation")
                    or path.endswith("/backend-api/conversation")
                )
                if is_conv:
                    if captured["done"]:
                        return
                    hdrs = req.all_headers()
                    pd = req.post_data
                    body = {}
                    if pd:
                        try:
                            body = json.loads(pd)
                        except Exception:
                            body = {}
                    captured["data"] = {"url": u, "method": "POST", "headers": hdrs, "body": body}
                    captured["done"] = True
                    print(f"  [CAPTURED] {u}  headers={len(hdrs)} bodyKeys={list(body.keys())[:6]}", flush=True)
            except Exception as e:
                print(f"  on_request err: {e}", flush=True)

        page.on("request", on_request)

        # dismiss any promo/announcement modal (e.g. "ChatGPT Images 2.0")
        # whose transparent backdrop intercepts composer clicks
        for _ in range(3):
            for sel in [
                "[data-testid='modal-close-button']",
                "button[aria-label='Close']",
                "button[aria-label='Close dialog']",
                "div[role='dialog'] button:has(svg)",
            ]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        print(f"    dismissed modal via {sel}", flush=True)
                        time.sleep(1)
                except Exception:
                    pass
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(1)
        ss(page, "after-modal-dismiss")

        if not is_logged_in(page):
            ss(page, "still-anon")
            sys.exit("❌ still logged out (anonymous) — refusing to capture anonymous session")

        # type a prompt into the composer and send
        print(f"[2] send prompt to trigger capture: {PROMPT!r}", flush=True)
        composer = None
        for sel in ["#prompt-textarea", "div[contenteditable='true']", "textarea"]:
            try:
                page.wait_for_selector(sel, timeout=15000)
                composer = page.locator(sel).first
                if composer.count() > 0:
                    break
            except Exception:
                continue
        if composer is None:
            ss(page, "no-composer")
            sys.exit("❌ composer not found")
        try:
            composer.click(timeout=8000)
        except Exception:
            try:
                composer.click(force=True, timeout=8000)
            except Exception:
                page.evaluate("() => { const e=document.querySelector('#prompt-textarea'); if(e) e.focus(); }")
        page.keyboard.type(PROMPT, delay=60)
        time.sleep(1)
        ss(page, "prompt-typed")
        # try send button, fallback Enter
        sent = False
        for sel in ["button[data-testid='send-button']", "button[aria-label*='Send']"]:
            try:
                b = page.locator(sel)
                if b.count() > 0 and b.first.is_enabled():
                    b.first.click()
                    sent = True
                    break
            except Exception:
                pass
        if not sent:
            page.keyboard.press("Enter")

        # wait for capture
        for _ in range(60):
            if captured["done"]:
                break
            time.sleep(1)
        ss(page, "after-send")

        if not captured["done"]:
            sys.exit("❌ never captured /backend-api/f/conversation POST")

        data = captured["data"]
        # sanity: must contain sentinel proof token + cookie
        h = {k.lower(): v for k, v in data["headers"].items()}
        if "openai-sentinel-proof-token" not in h:
            print("  ⚠ WARNING: openai-sentinel-proof-token missing from captured headers!", flush=True)
        if "cookie" not in h:
            print("  ⚠ WARNING: cookie missing from captured headers!", flush=True)

        users = {"chatgpt": {ZK_USER: {"username": ZK_USER, "parsedFetch": data, "sessions": []}}}
        os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
        with open(OUT_JSON, "w") as f:
            json.dump(users, f, indent=2)
        print(f"✅ wrote {OUT_JSON}", flush=True)
        print(f"   headers captured: {sorted(h.keys())}", flush=True)

        ctx.close()


if __name__ == "__main__":
    main()
