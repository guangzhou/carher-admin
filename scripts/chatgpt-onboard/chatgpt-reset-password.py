#!/usr/bin/env python3
"""
chatgpt-reset-password.py — Reset a ChatGPT/OpenAI account password via the
"Forgot password?" email flow (patchright + mail.com inbox), then set NEW_PASSWORD.

Resetting the password invalidates ALL existing sessions/tokens for the account,
which is exactly what we want to kick off external concurrent users.

ENV:
  MAIL_USER            <email>@mail.com
  MAIL_LOGIN_PW_FILE   /run/mail_pw.txt       (webmail password, to read reset email)
  NEW_PASSWORD_FILE    /run/new_pw.txt        (the new ChatGPT password to set)
  SCREENSHOT_DIR       /work/screenshots
  HEADLESS             0 = headed under Xvfb (default headed; CF blocks headless)

Runs inside mcr.microsoft.com/playwright/python image with patchright pip-installed.
"""
import os, re, sys, time
from patchright.sync_api import sync_playwright

EMAIL    = os.environ["MAIL_USER"]
MAIL_PW  = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
NEW_PW   = open(os.environ["NEW_PASSWORD_FILE"]).read().strip()
SS_DIR   = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
HEADLESS = os.environ.get("HEADLESS", "0") != "0"

os.makedirs(SS_DIR, exist_ok=True)

def ss(page, name):
    try:
        page.screenshot(path=f"{SS_DIR}/{name}.png", full_page=False)
        print(f"  shot: {SS_DIR}/{name}.png", flush=True)
    except Exception as e:
        print(f"  shot {name} failed: {e}", flush=True)

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
            btns.nth(i).click(); break
    for _ in range(30):
        if "navigator" in p.url: break
        time.sleep(1)
    if "navigator" not in p.url:
        ss(p, "mailcom-fail"); sys.exit(f"mail.com login failed url={p.url}")
    p.wait_for_timeout(3000)
    for sel in ["a:has-text('Continue to Account')", "button:has-text('Continue to Account')",
                "button:has-text('No, thanks')", "button:has-text('Maybe later')", "button:has-text('Skip')"]:
        try:
            loc = p.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(); p.wait_for_timeout(2000)
        except Exception:
            pass
    for attempt in range(20):
        mf = next((fr for fr in p.frames if fr.name == "mail"), None)
        if mf:
            try:
                txt = mf.evaluate("() => document.body.innerText")
                if txt and len(txt) > 50:
                    print(f"  mail.com inbox loaded ({len(txt)} chars)", flush=True); break
            except Exception:
                pass
        print(f"  waiting inbox iframe [{attempt+1}/20]", flush=True); time.sleep(2)
    ss(p, "mailcom-inbox")
    return p

def get_reset_link(mail_page, max_wait=180):
    """Find topmost OpenAI password-reset email, open it, extract the reset URL."""
    def valid_reset_href(h):
        low = h.lower()
        if "help.openai.com" in low or "articles/" in low or "unsub" in low:
            return False
        return bool(re.search(r"reset|password|auth\.openai|/u/", h, re.I))

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
                if not re.search(r"openai|chatgpt|reset|password|noreply", ln, re.I):
                    continue
                try:
                    fr.get_by_text(ln, exact=False).first.click(); time.sleep(2)
                except Exception:
                    continue
                # collect anchors from all non-list frames
                for f2 in mail_page.frames:
                    if f2.name == "mail":
                        continue
                    try:
                        hrefs = f2.evaluate(
                            "() => Array.from(document.querySelectorAll('a')).map(a=>a.href)")
                    except Exception:
                        hrefs = []
                    for h in hrefs:
                        if valid_reset_href(h):
                            return h
                    # also raw text URL fallback
                    try:
                        body = f2.evaluate("() => document.body.innerText")
                    except Exception:
                        body = ""
                    m = re.search(r"https?://\S*(?:reset|password|auth\.openai)\S*", body, re.I)
                    if m and valid_reset_href(m.group(0)):
                        return m.group(0).rstrip(").,>")
        print("  reset email not yet, retry 5s...", flush=True)
        time.sleep(5)
        for fr in mail_page.frames:
            if fr.name == "mail":
                try: fr.evaluate("() => document.location.reload()")
                except Exception: pass
        mail_page.wait_for_timeout(3000)
    return None

def _cf_present(page):
    try:
        title = page.title(); head = page.content().lower()[:2000]
    except Exception:
        return True
    if not title:
        return True
    return ("moment" in title.lower() or "performing security" in head
            or "verify you are human" in head or "just a moment" in head)

def wait_through_cf(page, max_wait=120):
    """Let patchright auto-solve CF (pure wait, like the OAuth flow). Click only as
    last resort after 60s, trying the real Turnstile iframe checkbox first."""
    deadline = time.time() + max_wait
    clicked = False
    while time.time() < deadline:
        if not _cf_present(page):
            return True
        elapsed = time.time() - (deadline - max_wait)
        if not clicked and elapsed > 55:
            # last resort: click inside the Turnstile iframe's checkbox
            try:
                done = False
                for fr in page.frames:
                    u = (fr.url or "").lower()
                    if "challenges.cloudflare" in u or "turnstile" in u:
                        try:
                            cb = fr.locator("input[type='checkbox'], label, body").first
                            cb.click(timeout=4000)
                            print(f"  ✓ clicked Turnstile checkbox in frame {u[:60]}", flush=True)
                            done = True; break
                        except Exception as e:
                            print(f"  frame click err: {e}", flush=True)
                if not done:
                    # fixed-coords fallback (viewport 1280x800)
                    page.mouse.move(360, 330, steps=10); time.sleep(0.3)
                    page.mouse.move(408, 360, steps=15); time.sleep(0.4)
                    page.mouse.click(408, 360)
                    print("  ✓ clicked CF fallback coords (408,360)", flush=True)
                clicked = True
                time.sleep(4); continue
            except Exception as e:
                print(f"  CF click err: {e}", flush=True)
        time.sleep(3)
    print("  ⚠ CF still present after wait", flush=True)
    return False

def _submit_form(p_page):
    """Click the black primary Continue button (exclude Google/Apple/phone social)."""
    try:
        btns = p_page.evaluate("""() => {
            return [...document.querySelectorAll('button')].filter(b => {
                const t = (b.innerText||'').trim();
                return /^(Continue|Sign in|Submit|Verify|Log in|Next)$/i.test(t)
                    && !/google|apple|phone|microsoft/i.test(t)
                    && (b.type === 'submit' || b.closest('form'));
            }).map(b => { const r=b.getBoundingClientRect();
                return {text:b.innerText.trim(), x:r.x, y:r.y, w:r.width, h:r.height}; });
        }""")
        for b in btns:
            if b['w'] > 0 and b['h'] > 0:
                p_page.mouse.click(b['x']+b['w']/2, b['y']+b['h']/2)
                print(f"    submit click: '{b['text']}'", flush=True); return
    except Exception as e:
        print(f"    submit dump fail: {e}", flush=True)
    try: p_page.keyboard.press("Enter"); print("    submit fallback: Enter", flush=True)
    except Exception: pass

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=HEADLESS, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    page = ctx.new_page()

    # ── 1. auth.openai.com/codex/device → type email → OpenAI password page ──
    # The chatgpt.com modal can display the email while React validation still
    # treats it as empty. The Codex device entrypoint uses the stable auth flow.
    print("[1] open auth.openai.com/codex/device", flush=True)
    page.goto("https://auth.openai.com/codex/device", wait_until="domcontentloaded")
    time.sleep(3)
    wait_through_cf(page)
    ss(page, "01-login")
    page.wait_for_selector("input[type='email'], input[autocomplete='username']", timeout=30000)
    # type with verify-retry: React can swallow the first keystrokes / clear on hydrate
    typed_ok = False
    for attempt in range(4):
        try:
            el = page.locator("input[type='email']").first
            el.click()
            time.sleep(0.4)
            el.fill("")              # clear any partial
            page.keyboard.type(EMAIL, delay=90)
            time.sleep(0.6)
            val = el.input_value()
        except Exception as e:
            print(f"  email type attempt {attempt} err: {e}", flush=True)
            val = ""
        print(f"  email field value after type [{attempt}]: {val!r}", flush=True)
        if val.strip().lower() == EMAIL.lower():
            typed_ok = True; break
        time.sleep(1)
    ss(page, "02-after-email")     # capture BEFORE submit, to see the real field state
    if not typed_ok:
        sys.exit(f"❌ failed to type email into field (last value empty) — see 02-after-email.png")
    _submit_form(page)
    time.sleep(5)
    print(f"  after email url={page.url[:100]}", flush=True)
    for _ in range(30):
        if "password" in page.url.lower() or page.query_selector("input[type='password']"):
            break
        time.sleep(2)
    ss(page, "03-password-page")

    # ── 2. click "Forgot password?" on the OpenAI password page ──
    print("[2] click Forgot password", flush=True)
    clicked = False
    for sel in ["a:has-text('Forgot password')", "button:has-text('Forgot password')",
                "a:has-text('Forgot')", "text=Forgot password?"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(); clicked = True
                print(f"  clicked {sel}", flush=True); break
        except Exception:
            pass
    if not clicked:
        print("  no Forgot link visible; dumping page links", flush=True)
        try:
            links = page.evaluate("() => [...document.querySelectorAll('a,button')]"
                                  ".map(e=>e.innerText.trim()).filter(Boolean).slice(0,30)")
            print("  links:", links, flush=True)
        except Exception:
            pass
    time.sleep(4)
    ss(page, "04-after-forgot")
    print(f"  url after forgot: {page.url}", flush=True)

    # ── 3. read reset link from mail.com ──
    print("[3] login mail.com, fetch reset link", flush=True)
    mp = mailcom_login(ctx)
    link = get_reset_link(mp)
    mp.close()
    if not link:
        ss(page, "05-no-reset-link"); sys.exit("❌ no reset link found in mailbox within timeout")
    print(f"  reset link: {link[:120]}", flush=True)

    # ── 4. open reset link, set new password ──
    print("[4] open reset link, set new password", flush=True)
    page.goto(link, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    wait_through_cf(page)
    ss(page, "06-reset-page")
    # fill all visible password fields
    pws = page.locator("input[type='password']")
    n = pws.count()
    print(f"  password fields: {n}", flush=True)
    if n == 0:
        # maybe an intermediate "reset password" confirm button first
        cont = page.locator("button:has-text('Continue'), button:has-text('Reset password'), a:has-text('Reset password')")
        if cont.count() > 0:
            cont.first.click(); page.wait_for_timeout(3000)
            ss(page, "06b-after-continue")
            pws = page.locator("input[type='password']"); n = pws.count()
            print(f"  password fields after continue: {n}", flush=True)
    if n == 0:
        sys.exit("❌ no password input on reset page (see 06-reset-page.png)")
    for i in range(n):
        pws.nth(i).fill(NEW_PW)
    ss(page, "07-filled")
    submit = page.locator("button:has-text('Reset password'), button:has-text('Continue'), "
                          "button:has-text('Save'), button:has-text('Update'), button[type='submit']")
    if submit.count() > 0:
        submit.first.click()
    page.wait_for_timeout(5000)
    ss(page, "08-after-submit")
    body = ""
    try: body = page.content()
    except Exception: pass
    url = page.url
    print(f"  final url: {url}", flush=True)
    ok = any(k in body for k in ["has been reset", "password was", "successfully", "Sign in",
                                 "log in", "Password updated", "changed"]) or "log-in" in url
    if ok:
        print(f"🎉 {EMAIL} password reset OK", flush=True)
    else:
        print(f"⚠️  reset result inconclusive for {EMAIL} (check 08-after-submit.png)", flush=True)
        print(body[:400], flush=True)
    browser.close()
