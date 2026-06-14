#!/usr/bin/env python3
"""
cc-oauth-mailcom-v3.py — single-context patchright, 适配 claude.ai 新 verify-code 流程

WHY V3 (vs v1 / v2):
  v1: 嵌套 patchright_pw() + vanilla_pw() 触发 asyncio loop 冲突
  v2: 4 段串行 + storage_state 跨 patchright context — D 段白屏 (cross-context restore 失败)
  v3: **single patchright context**, 仿 cc-oauth-171mail.py 架构, claude_page 全程 alive
      mail.com 也用 patchright 渲染 (avoid vanilla playwright 进程嵌套), 实测可行性

FLOW (4 page 共享同一 patchright ctx):
  1. claude_page  goto OAUTH_URL → fill email → "Continue with email"
                  → stops at verify-code input page (page kept alive)
  2. mail_page    goto www.mail.com → click Log in → fill email/password → submit
                  → find latest Anthropic/Claude email in inbox
  3. code_page    goto magic-link (from inbox) → wait → extract 6-digit verify code
  4. claude_page  (still alive on verify-code input page)
                  → fill code → "Verify Email Address"
                  → if "/new" or invite: navigate back to OAUTH_URL
                  → click Authorize → capture callback code

ENV:
  CC_EMAIL        e.g. Mayo_Haneynns@therapist.net
  MAIL_PW         mail.com webmail password
  CC_OAUTH_URL    https://claude.com/cai/oauth/authorize?...
"""
import os, re, time
from urllib.parse import urlparse, parse_qs, unquote
from patchright.sync_api import sync_playwright

EMAIL = os.environ["CC_EMAIL"]
MAIL_PW = os.environ["MAIL_PW"]
OAUTH_URL = os.environ["CC_OAUTH_URL"]
SS = "/work/screenshots"
os.makedirs(SS, exist_ok=True)


def shoot(p, name):
    try:
        p.screenshot(path=f"{SS}/{name}.png")
        print(f"  shot: {name}.png", flush=True)
    except Exception as e:
        print(f"  shot {name} failed: {e}", flush=True)


def wait_past_turnstile(page, max_wait=90):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        title = (page.title() or "").lower()
        body = page.content().lower()[:3000]
        if "just a moment" not in title and "performing security verification" not in body:
            return True
        for fr in page.frames:
            url = (fr.url or "").lower()
            if "challenges.cloudflare" in url or "turnstile" in url:
                try:
                    cb = fr.locator("input[type='checkbox']").first
                    if cb.count() > 0 and cb.is_visible():
                        print(f"  [turnstile] clicking", flush=True)
                        cb.click(force=True)
                        time.sleep(4)
                except Exception:
                    pass
        time.sleep(3)
    return False


def get_mail_frame(page, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for fr in page.frames:
            if fr.name == "mail":
                return fr
        time.sleep(2)
    return None


def unwrap_deref(url):
    if "deref-mail.com" in url or "redirectUrl=" in url:
        try:
            qs = parse_qs(urlparse(url).query)
            if "redirectUrl" in qs:
                return unquote(qs["redirectUrl"][0])
        except Exception:
            pass
    return url


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")

    # ── Step 1: claude_page trigger email ────────────────────────────
    print("[1] claude_page: open OAUTH_URL + trigger email", flush=True)
    claude_page = ctx.new_page()
    claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 90)
        time.sleep(3)
    shoot(claude_page, "1-claude-landing")

    claude_page.wait_for_selector("input[type='email'], input[name='email']", timeout=15000)
    claude_page.locator("input[type='email'], input[name='email']").first.click()
    claude_page.keyboard.type(EMAIL, delay=70)
    btn = claude_page.get_by_role("button", name=re.compile(r"continue with email", re.I))
    if btn.count() == 0:
        btn = claude_page.get_by_role("button", name=re.compile(r"^continue$|next", re.I))
    if btn.count() > 0:
        btn.first.click()
    else:
        claude_page.keyboard.press("Enter")

    try:
        claude_page.wait_for_selector("input[placeholder='Enter verification code']", timeout=20000)
        print("  ✅ claude_page at verify-code input page", flush=True)
    except Exception:
        body = claude_page.inner_text("body")[:1000]
        print(f"  ⚠️ verify-code page not reached. body: {body!r}", flush=True)
        shoot(claude_page, "1b-no-verify-page")
        br.close()
        raise SystemExit("CLAUDE_NO_VERIFY_PAGE")
    shoot(claude_page, "2-verify-code-page")

    # ── Step 2: mail_page login mail.com (same ctx, patchright) ─────
    print("[2] mail_page: login mail.com + find magic-link", flush=True)
    mail_page = ctx.new_page()
    mail_page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    shoot(mail_page, "3-mailcom-landing")

    # cookie banner
    for label_pat in [r"accept all", r"agree", r"i agree", r"^ok$"]:
        try:
            b = mail_page.get_by_role("button", name=re.compile(label_pat, re.I))
            if b.count() > 0 and b.first.is_visible():
                print(f"  dismissing cookie banner via '{label_pat}'", flush=True)
                b.first.click()
                time.sleep(2)
                break
        except Exception:
            pass

    # click Log in
    login_clicked = False
    for sel in ["a:has-text('Log in')", "button:has-text('Log in')", "a[href*='login']"]:
        try:
            cand = mail_page.locator(sel)
            for i in range(cand.count()):
                el = cand.nth(i)
                if el.is_visible():
                    print(f"  clicking login via '{sel}' #{i}", flush=True)
                    el.click()
                    login_clicked = True
                    break
            if login_clicked:
                break
        except Exception:
            pass
    time.sleep(5)

    try:
        mail_page.wait_for_selector(
            "input[placeholder='Email address'], input[type='email'], input[name='username']",
            timeout=20000)
    except Exception:
        shoot(mail_page, "3b-no-form")
        br.close()
        raise SystemExit("MAILCOM_NO_LOGIN_FORM")

    e_in = mail_page.locator("input[placeholder='Email address']").first
    if e_in.count() == 0:
        e_in = mail_page.locator("input[type='email']").first
    e_in.click()
    e_in.fill(EMAIL)
    p_in = mail_page.locator("input[placeholder='Password']").first
    if p_in.count() == 0:
        p_in = mail_page.locator("input[type='password']").first
    p_in.click()
    p_in.fill(MAIL_PW)
    shoot(mail_page, "3c-filled")

    btns = mail_page.locator("button:has-text('Log in'), input[type='submit']")
    clicked = False
    for i in range(btns.count()):
        b = btns.nth(i)
        if b.is_visible():
            box = b.bounding_box()
            if box and box["y"] > 50:
                b.click()
                clicked = True
                break
    if not clicked:
        mail_page.keyboard.press("Enter")
    time.sleep(10)
    shoot(mail_page, "3d-after-login")

    # wait for mail to arrive
    time.sleep(45)
    fr = get_mail_frame(mail_page, timeout=30)
    if not fr:
        shoot(mail_page, "3e-no-mail-frame")
        br.close()
        raise SystemExit("MAILCOM_NO_MAIL_FRAME")
    shoot(mail_page, "3f-inbox")

    SENDER_RE = re.compile(r"anthropic|claude|secure link", re.I)
    secure_link = None
    for attempt in range(5):
        if secure_link:
            break
        print(f"  [scan {attempt+1}/5]", flush=True)
        try:
            text = fr.evaluate("() => document.body.innerText")
        except Exception:
            time.sleep(8)
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        target = None
        for ln in lines:
            if SENDER_RE.search(ln):
                target = ln
                break
        if not target:
            time.sleep(15)
            try:
                refresh = fr.locator("button:has-text('Refresh'), [title*='Refresh' i]").first
                if refresh.count() > 0 and refresh.is_visible():
                    refresh.click()
                    time.sleep(5)
            except Exception:
                pass
            continue
        try:
            fr.get_by_text(target, exact=False).first.click()
            time.sleep(6)
        except Exception:
            time.sleep(8)
            continue
        for f in mail_page.frames:
            try:
                for a in f.locator("a").all():
                    try:
                        href = a.get_attribute("href") or ""
                    except Exception:
                        continue
                    h = href.lower()
                    if "claude" in h and ("login" in h or "magic" in h or "verify" in h or "token=" in h):
                        secure_link = href
                        break
                if secure_link:
                    break
                body = f.evaluate("() => document.body.innerText")
                m = re.search(r"(https?://[^\s\"'<>]*claude[^\s\"'<>]*(?:login|magic|verify|token=)[^\s\"'<>]*)", body, re.I)
                if m:
                    secure_link = m.group(1)
                    break
            except Exception:
                continue
        if not secure_link:
            time.sleep(15)
    shoot(mail_page, "3g-final")

    if not secure_link:
        br.close()
        raise SystemExit("NO_SECURE_LINK")

    secure_link = unwrap_deref(secure_link)
    print(f"  magic-link: {secure_link[:120]}", flush=True)

    # ── Step 3: code_page open magic-link, extract 6-digit code ─────
    print("[3] code_page: open magic-link + extract 6-digit code", flush=True)
    code_page = ctx.new_page()
    code_page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    if "moment" in (code_page.title() or "").lower():
        wait_past_turnstile(code_page, 60)
        time.sleep(3)
    shoot(code_page, "4-magic-link")
    body = code_page.inner_text("body")
    m = re.search(r"\b(\d{6})\b", body)
    if not m:
        print(f"  ❌ no 6-digit code. body: {body[:500]!r}", flush=True)
        br.close()
        raise SystemExit("NO_VERIFY_CODE")
    verify_code = m.group(1)
    print(f"  ✅ verify code: {verify_code}", flush=True)

    # ── Step 4: switch back to claude_page (still alive) ────────────
    print("[4] claude_page: enter verify code + complete OAuth", flush=True)
    claude_page.bring_to_front()
    code_in = claude_page.locator("input[placeholder='Enter verification code']").first
    if code_in.count() == 0 or not code_in.is_visible():
        print(f"  ⚠️ verify-code input no longer visible on claude_page", flush=True)
        shoot(claude_page, "5a-stale-claude-page")
        br.close()
        raise SystemExit("CLAUDE_PAGE_STALE")
    code_in.click()
    code_in.fill(verify_code)
    shoot(claude_page, "5-code-filled")
    vbtn = claude_page.get_by_role("button", name=re.compile(r"verify email address|verify|continue", re.I))
    if vbtn.count() > 0:
        vbtn.first.click()
    else:
        claude_page.keyboard.press("Enter")
    time.sleep(10)
    shoot(claude_page, "6-after-verify")

    body_text = claude_page.inner_text("body")
    if "invited you to join" in body_text.lower():
        print("  Team invite — clicking Accept", flush=True)
        accept_btn = claude_page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if accept_btn.count() > 0:
            accept_btn.first.click()
            time.sleep(8)
            shoot(claude_page, "7-after-accept")

    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)

    if any(x in claude_page.url for x in ("/new", "claude.ai/projects", "claude.ai/chats")):
        print("  At /new — navigate back to OAUTH_URL", flush=True)
        claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        shoot(claude_page, "8-back-to-oauth")

    print("  Click Authorize", flush=True)
    auth_btn = claude_page.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
    if auth_btn.count() > 0:
        auth_btn.first.click()
        time.sleep(6)
    shoot(claude_page, "9-after-authorize")

    m = re.search(r"[?&]code=([^&\s]+)", claude_page.url)
    if m:
        print(f"\n✅ CALLBACK_CODE={m.group(1)}", flush=True)
    else:
        body_text = claude_page.inner_text("body")
        m = re.search(r"\b([a-zA-Z0-9_-]{50,}#[a-zA-Z0-9_-]{20,})\b", body_text)
        if m:
            print(f"\n✅ CALLBACK_CODE={m.group(1)}", flush=True)
        else:
            print(f"\n❌ No code. body: {body_text[:1500]!r}", flush=True)
    br.close()
