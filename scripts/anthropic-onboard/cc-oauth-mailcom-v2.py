#!/usr/bin/env python3
"""
cc-oauth-mailcom-v2.py — claude.ai 新 verify-code 流程版 (2026-05-25 起)

WHY V2:
  claude.ai magic-link 流程改了:
    1. tab A: 输 email → 跳 "Enter verification code" 页 (session-bound)
    2. magic-link 邮件点开 → 显示 6 位 code 而不是直接登录
    3. 在 tab A 输 6 位 code → 完成登录 → OAuth Authorize
  v1 脚本断在第 2 步, 因为它期待点 magic-link 直接登入。

ARCHITECTURE (4 段串行, 不嵌套 sync_playwright 避 asyncio loop 冲突):
  A. patchright: goto OAUTH_URL → trigger email → 看到 verify-code 页 → 存 storage_state
  B. vanilla:    登 mail.com → 取最新 claude.ai magic-link (含 deref-mail.com unwrap)
  C. patchright: goto magic-link → 抓 6 位 verify code
  D. patchright: restore storage_state → 输 verify code → Accept invite → Authorize → 抓 callback

storage_state 在 /work/state.json (docker mount), 让段 A 写、段 D 读.

ENV:
  CC_EMAIL        e.g. Mayo_Haneynns@therapist.net
  MAIL_PW         mail.com webmail 密码
  CC_OAUTH_URL    https://claude.com/cai/oauth/authorize?...
"""
import os, re, time
from urllib.parse import urlparse, parse_qs, unquote
from patchright.sync_api import sync_playwright as patchright_pw
from playwright.sync_api import sync_playwright as vanilla_pw

EMAIL = os.environ["CC_EMAIL"]
MAIL_PW = os.environ["MAIL_PW"]
OAUTH_URL = os.environ["CC_OAUTH_URL"]
SS = "/work/screenshots"
STATE_FILE = "/work/state.json"
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


# ── Step A: patchright trigger email + save storage_state ────────────
print("[A] patchright: goto OAUTH_URL, trigger email, save state", flush=True)
with patchright_pw() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    page = ctx.new_page()
    page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    if "moment" in (page.title() or "").lower():
        wait_past_turnstile(page, 90)
        time.sleep(3)
    shoot(page, "A1-landing")

    page.wait_for_selector("input[type='email'], input[name='email']", timeout=15000)
    page.locator("input[type='email'], input[name='email']").first.click()
    page.keyboard.type(EMAIL, delay=70)
    btn = page.get_by_role("button", name=re.compile(r"continue with email", re.I))
    if btn.count() == 0:
        btn = page.get_by_role("button", name=re.compile(r"^continue$|next", re.I))
    if btn.count() > 0:
        btn.first.click()
    else:
        page.keyboard.press("Enter")

    try:
        page.wait_for_selector("input[placeholder='Enter verification code']", timeout=20000)
        print("  ✅ verify-code page loaded", flush=True)
    except Exception:
        body = page.inner_text("body")[:1000]
        print(f"  ⚠️ verify-code page not reached. body: {body!r}", flush=True)
        shoot(page, "A2-no-verify-page")
        br.close()
        raise SystemExit("CLAUDE_NO_VERIFY_PAGE")
    shoot(page, "A2-verify-code-page")

    ctx.storage_state(path=STATE_FILE)
    print(f"  ✅ storage_state saved to {STATE_FILE}", flush=True)
    br.close()


# ── Step B: vanilla mail.com fetch magic-link ────────────────────────
print("[B] vanilla: fetch magic-link from mail.com", flush=True)
secure_link = None
with vanilla_pw() as vpw:
    mb = vpw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    mctx = mb.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    mp = mctx.new_page()
    mp.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    shoot(mp, "B1-mailcom-landing")

    for label_pat in [r"accept all", r"agree", r"i agree", r"^ok$"]:
        try:
            b = mp.get_by_role("button", name=re.compile(label_pat, re.I))
            if b.count() > 0 and b.first.is_visible():
                print(f"  dismissing cookie banner via '{label_pat}'", flush=True)
                b.first.click()
                time.sleep(2)
                break
        except Exception:
            pass

    login_clicked = False
    for sel in ["a:has-text('Log in')", "button:has-text('Log in')", "a[href*='login']"]:
        try:
            cand = mp.locator(sel)
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
        mp.wait_for_selector(
            "input[placeholder='Email address'], input[type='email'], input[name='username']",
            timeout=20000)
    except Exception:
        shoot(mp, "B2-no-form")
        mb.close()
        raise SystemExit("MAILCOM_NO_LOGIN_FORM")

    e_in = mp.locator("input[placeholder='Email address']").first
    if e_in.count() == 0:
        e_in = mp.locator("input[type='email']").first
    e_in.click()
    e_in.fill(EMAIL)
    p_in = mp.locator("input[placeholder='Password']").first
    if p_in.count() == 0:
        p_in = mp.locator("input[type='password']").first
    p_in.click()
    p_in.fill(MAIL_PW)
    shoot(mp, "B2-filled")

    btns = mp.locator("button:has-text('Log in'), input[type='submit']")
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
        mp.keyboard.press("Enter")
    time.sleep(10)
    shoot(mp, "B3-after-login")

    time.sleep(45)
    fr = get_mail_frame(mp, timeout=30)
    if not fr:
        shoot(mp, "B4-no-mail-frame")
        mb.close()
        raise SystemExit("MAILCOM_NO_MAIL_FRAME")
    shoot(mp, "B4-inbox")

    SENDER_RE = re.compile(r"anthropic|claude|secure link", re.I)
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
        for f in mp.frames:
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
    shoot(mp, "B5-final")
    mb.close()

if not secure_link:
    raise SystemExit("NO_SECURE_LINK")

if "deref-mail.com" in secure_link or "redirectUrl=" in secure_link:
    try:
        qs = parse_qs(urlparse(secure_link).query)
        if "redirectUrl" in qs:
            real = unquote(qs["redirectUrl"][0])
            print(f"  unwrapped deref: {real[:160]}", flush=True)
            secure_link = real
    except Exception:
        pass


# ── Step C: patchright open magic-link, extract verify code ──────────
print(f"[C] patchright: open magic-link, extract 6-digit verify code", flush=True)
verify_code = None
with patchright_pw() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    page = ctx.new_page()
    page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    if "moment" in (page.title() or "").lower():
        wait_past_turnstile(page, 60)
        time.sleep(3)
    shoot(page, "C1-magic-link")
    body = page.inner_text("body")
    m = re.search(r"\b(\d{6})\b", body)
    if m:
        verify_code = m.group(1)
        print(f"  ✅ verify code: {verify_code}", flush=True)
    else:
        print(f"  ⚠️ no 6-digit code. body: {body[:500]!r}", flush=True)
    br.close()

if not verify_code:
    raise SystemExit("NO_VERIFY_CODE")


# ── Step D: restore state, enter verify code, complete OAuth ─────────
print(f"[D] patchright: restore state, enter verify code, complete OAuth", flush=True)
with patchright_pw() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        storage_state=STATE_FILE,
    )
    page = ctx.new_page()
    page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    if "moment" in (page.title() or "").lower():
        wait_past_turnstile(page, 60)
        time.sleep(3)
    shoot(page, "D1-back-to-oauth")

    code_in = page.locator("input[placeholder='Enter verification code']").first
    if code_in.count() > 0 and code_in.is_visible():
        print(f"  filling code", flush=True)
        code_in.click()
        code_in.fill(verify_code)
        shoot(page, "D2-code-filled")
        vbtn = page.get_by_role("button", name=re.compile(r"verify email address|verify|continue", re.I))
        if vbtn.count() > 0:
            vbtn.first.click()
        else:
            page.keyboard.press("Enter")
        time.sleep(10)
    else:
        print(f"  no verify-code input visible (maybe already past)", flush=True)
    shoot(page, "D3-after-verify")

    body_text = page.inner_text("body")
    if "invited you to join" in body_text.lower():
        print(f"  [D] Team invite — clicking Accept", flush=True)
        accept_btn = page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if accept_btn.count() > 0:
            accept_btn.first.click()
            time.sleep(8)
            shoot(page, "D4-after-accept")

    if "moment" in (page.title() or "").lower():
        wait_past_turnstile(page, 60)
        time.sleep(3)

    if any(x in page.url for x in ("/new", "claude.ai/projects", "claude.ai/chats")):
        print(f"  [D] At /new — navigate back to OAuth URL", flush=True)
        page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        shoot(page, "D5-back-to-oauth")

    print(f"  [D] Click Authorize", flush=True)
    auth_btn = page.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
    if auth_btn.count() > 0:
        auth_btn.first.click()
        time.sleep(6)
    shoot(page, "D6-after-authorize")

    m = re.search(r"[?&]code=([^&\s]+)", page.url)
    if m:
        print(f"\n✅ CALLBACK_CODE={m.group(1)}", flush=True)
    else:
        body_text = page.inner_text("body")
        m = re.search(r"\b([a-zA-Z0-9_-]{50,}#[a-zA-Z0-9_-]{20,})\b", body_text)
        if m:
            print(f"\n✅ CALLBACK_CODE={m.group(1)}", flush=True)
        else:
            print(f"\n❌ No code. body: {body_text[:1500]!r}", flush=True)
    br.close()
