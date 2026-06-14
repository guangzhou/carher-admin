#!/usr/bin/env python3
"""
cc-oauth-gmail-v3.py — Gmail 版,适配 claude.ai 新 verify-code (6-digit) 流程

WHY V3 (vs cc-oauth-full.py):
  cc-oauth-full.py 直接 goto magic-link 自动登录 claude.ai (旧路径)
  2026-05-26 起 claude.ai 改成: 邮件含 6 位 verify code, claude_page 停在 input page
  v3: 仿 cc-oauth-mailcom-v3.py 架构 (single patchright ctx + 4 page 流转),
      但把 mail.com webmail 段换成 Gmail (accounts.google.com + TOTP)

FLOW (单 patchright ctx, 4 page 共享):
  1. claude_page  goto OAUTH_URL → fill email → "Continue with email"
                  → stops at verify-code input page (kept alive)
  2. gmail_page   accounts.google.com → email → password → TOTP
                  → mail.google.com search Anthropic → 抓 magic-link href
  3. code_page    goto magic-link → 抓 body 里的 6-digit code
  4. claude_page  (still alive) → fill code → Verify
                  → Accept invite if Team → goto OAUTH_URL → Authorize → 抓 callback

ENV:
  CC_EMAIL        thomasmatthewlkgmx1915@gmail.com
  GMAIL_PW        Gmail 登录密码
  GMAIL_TOTP      Gmail 2FA TOTP secret
  CC_OAUTH_URL    https://claude.com/cai/oauth/authorize?... (从 claude setup-token 拿)

KNOWN ISSUE:
  Gmail 可能弹 "Verify it's you" reCAPTCHA (188 IP 被风控)
  v3 检测到 reCAPTCHA 后 screenshot + 退出 GMAIL_RECAPTCHA, 调用方需 fallback
"""
import os, re, time
import pyotp
from patchright.sync_api import sync_playwright

EMAIL = os.environ["CC_EMAIL"]
GMAIL_PW = os.environ["GMAIL_PW"]
GMAIL_TOTP = os.environ["GMAIL_TOTP"]
OAUTH_URL = os.environ["CC_OAUTH_URL"]
SS = "/work/screenshots"
os.makedirs(SS, exist_ok=True)


def shoot(p, name):
    try:
        p.screenshot(path=f"{SS}/{name}.png")
        print(f"  shot: {name}.png", flush=True)
    except Exception as e:
        print(f"  shot {name} failed: {e}", flush=True)


def safe_click_first(locator, page=None, label="element", timeout=10000):
    """Google sometimes leaves an invisible overlay over visible buttons."""
    try:
        locator.first.click(timeout=timeout)
        return True
    except Exception as e:
        print(f"  [click] normal click failed on {label}: {type(e).__name__}", flush=True)
    try:
        locator.first.click(force=True, timeout=timeout)
        return True
    except Exception as e:
        print(f"  [click] force click failed on {label}: {type(e).__name__}", flush=True)
    if page is not None:
        try:
            page.keyboard.press("Enter")
            return True
        except Exception as e:
            print(f"  [click] Enter fallback failed on {label}: {type(e).__name__}", flush=True)
    return False


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


def click_recaptcha_checkbox(page, max_wait=15):
    """找 reCAPTCHA iframe, 点 'I'm not a robot' checkbox.
    Google low-risk session 直接放行; high-risk 会跳图形挑战.
    Return True 如果点了, False 如果没找到 iframe."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        for fr in page.frames:
            url = (fr.url or "").lower()
            if "recaptcha" in url and "anchor" in url:
                try:
                    cb = fr.locator("#recaptcha-anchor, .recaptcha-checkbox").first
                    if cb.count() > 0:
                        print(f"  [recaptcha] clicking checkbox in iframe", flush=True)
                        cb.click(force=True)
                        time.sleep(5)
                        return True
                except Exception as e:
                    print(f"  [recaptcha] click err: {e}", flush=True)
        time.sleep(2)
    return False


def submit_after_recaptcha(page):
    """点完 reCAPTCHA 后再点 Next 提交"""
    nbtn = page.get_by_role("button", name=re.compile(r"^next$|continue", re.I))
    if nbtn.count() > 0:
        safe_click_first(nbtn, page, "post-recaptcha next")
        time.sleep(6)
        return True
    return False


def detect_gmail_block(page):
    """检测 Gmail 风控页 (Verify it's you / reCAPTCHA / 其他 challenge),
    return reason string 或 None."""
    try:
        body = page.inner_text("body").lower()[:3000]
        title = (page.title() or "").lower()
    except Exception:
        return None
    if "verify it's you" in body or "verify it’s you" in body:
        return "VERIFY_ITS_YOU"
    if "i'm not a robot" in body or "recaptcha" in body:
        return "RECAPTCHA"
    if "couldn't sign you in" in body or "couldn’t sign you in" in body:
        return "CANT_SIGN_IN"
    if "unusual activity" in body:
        return "UNUSUAL_ACTIVITY"
    if "try another way" in body and "next" in body and "password" not in body:
        return "EXTRA_VERIFICATION"
    return None


def bypass_verify_via_totp(page, totp_secret, max_attempts=2):
    """处理 Google 'Verify it's you' reCAPTCHA 页:
    点 'Try another way' → 选 Authenticator app → 输入 TOTP code.
    成功返回 True,否则 False."""
    print("  [bypass] try 'Try another way' to use TOTP", flush=True)
    try:
        tab = page.get_by_role("link", name=re.compile(r"try another way", re.I))
        if tab.count() == 0:
            tab = page.get_by_text(re.compile(r"try another way", re.I))
        if tab.count() == 0:
            print("  [bypass] no 'Try another way' link", flush=True)
            return False
        tab.first.click()
        time.sleep(5)
        shoot(page, "bypass-1-try-another-way")
    except Exception as e:
        print(f"  [bypass] click 'Try another way' failed: {e}", flush=True)
        return False

    # 现在是 "Choose how you want to sign in" 页, 找 "Authenticator app" / "Get a verification code"
    body = page.inner_text("body")
    print(f"  [bypass] options page body excerpt: {body[:500]!r}", flush=True)
    for label_pat in [r"authenticator app", r"get a verification code", r"google authenticator"]:
        try:
            opt = page.get_by_text(re.compile(label_pat, re.I))
            if opt.count() == 0:
                continue
            for i in range(opt.count()):
                el = opt.nth(i)
                if el.is_visible():
                    print(f"  [bypass] click option '{label_pat}'", flush=True)
                    el.click()
                    time.sleep(5)
                    shoot(page, "bypass-2-totp-page")
                    break
            break
        except Exception:
            continue

    # 输入 TOTP code
    code = pyotp.TOTP(totp_secret).now()
    print("  [bypass] TOTP generated", flush=True)
    for inp in page.locator("input").all():
        try:
            if inp.is_visible() and inp.get_attribute("type") in ("text", "tel", None):
                inp.click()
                page.keyboard.type(code, delay=80)
                break
        except Exception:
            continue
    nbtn = page.get_by_role("button", name=re.compile(r"^next$|verify|continue", re.I))
    if nbtn.count() > 0:
        safe_click_first(nbtn, page, "totp next")
    else:
        page.keyboard.press("Enter")
    time.sleep(8)
    shoot(page, "bypass-3-after-totp")

    # 验证是否过了风控
    block = detect_gmail_block(page)
    if block:
        print(f"  [bypass] still blocked after TOTP: {block}", flush=True)
        return False
    print(f"  [bypass] ✅ passed verify-it's-you. url={page.url[:100]}", flush=True)
    return True


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")

    # ── Step 1: claude_page 触发 email ────────────────────────────────
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

    # ── Step 2: gmail_page 登 Gmail + 找 magic-link ───────────────────
    print("[2] gmail_page: login Gmail + find magic-link", flush=True)
    gmail_page = ctx.new_page()
    gmail_page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    shoot(gmail_page, "3-gmail-landing")

    # email
    gmail_page.wait_for_selector("input[type='email']", timeout=15000)
    gmail_page.locator("input[type='email']").first.click()
    gmail_page.keyboard.type(EMAIL, delay=80)
    safe_click_first(gmail_page.get_by_role("button", name=re.compile(r"^next$", re.I)), gmail_page, "gmail email next")
    time.sleep(6)
    shoot(gmail_page, "3a-gmail-after-email")

    # 检测 Gmail 是否弹风控页 (在密码页之前)
    block = detect_gmail_block(gmail_page)
    if block in ("VERIFY_ITS_YOU", "RECAPTCHA"):
        print(f"  ⚠️ Gmail challenge: {block} — try reCAPTCHA checkbox first", flush=True)
        if click_recaptcha_checkbox(gmail_page):
            shoot(gmail_page, "3b1-after-recaptcha-click")
            submit_after_recaptcha(gmail_page)
            shoot(gmail_page, "3b2-after-recaptcha-submit")
            block = detect_gmail_block(gmail_page)
            if not block:
                print(f"  ✅ reCAPTCHA passed! url={gmail_page.url[:80]}", flush=True)

    if block:
        print(f"  ⚠️ Gmail still blocked: {block} — try TOTP bypass", flush=True)
        if not bypass_verify_via_totp(gmail_page, GMAIL_TOTP):
            shoot(gmail_page, "3b-gmail-blocked")
            br.close()
            raise SystemExit(f"GMAIL_BLOCKED_{block}")

    # password
    try:
        gmail_page.wait_for_selector("input[type='password']", timeout=15000)
    except Exception:
        print(f"  ❌ no password input. url={gmail_page.url}", flush=True)
        shoot(gmail_page, "3c-no-pw-input")
        br.close()
        raise SystemExit("GMAIL_NO_PW_INPUT")
    gmail_page.locator("input[type='password']").first.click()
    gmail_page.keyboard.type(GMAIL_PW, delay=80)
    safe_click_first(gmail_page.get_by_role("button", name=re.compile(r"^next$", re.I)), gmail_page, "gmail password next")
    time.sleep(6)
    shoot(gmail_page, "3d-gmail-after-pw")

    # TOTP (Gmail 2FA)
    url_l = gmail_page.url.lower()
    body_l = gmail_page.content().lower()
    if "totp" in url_l or "2-step" in body_l or "challenge" in url_l:
        totp = pyotp.TOTP(GMAIL_TOTP).now()
        print("  Gmail TOTP generated", flush=True)
        filled = False
        for inp in gmail_page.locator("input").all():
            try:
                if inp.is_visible() and inp.get_attribute("type") in ("text", "tel", None):
                    inp.click()
                    gmail_page.keyboard.type(totp, delay=80)
                    filled = True
                    break
            except Exception:
                continue
        if filled:
            safe_click_first(gmail_page.get_by_role("button", name=re.compile(r"^next$|verify", re.I)), gmail_page, "gmail totp verify")
            time.sleep(8)
            shoot(gmail_page, "3e-gmail-after-totp")

    # 再检测一次风控页 (TOTP 后可能还有挑战)
    block = detect_gmail_block(gmail_page)
    if block:
        print(f"  ⚠️ Gmail block after password/TOTP: {block} — try TOTP bypass", flush=True)
        if not bypass_verify_via_totp(gmail_page, GMAIL_TOTP):
            shoot(gmail_page, "3f-gmail-blocked-post-pw")
            br.close()
            raise SystemExit(f"GMAIL_BLOCKED_{block}")

    print(f"  Gmail logged in: url={gmail_page.url[:80]}", flush=True)

    # 搜 Anthropic Secure link 邮件 (最近 1 天)
    gmail_page.goto(
        "https://mail.google.com/mail/u/0/#search/from%3Aanthropic+subject%3A(Secure+link)+newer_than%3A1d",
        wait_until="domcontentloaded", timeout=30000)
    time.sleep(10)
    shoot(gmail_page, "4-gmail-search")

    secure_link = None
    try:
        first_email = gmail_page.locator("tr").filter(has_text="Secure link to log in to Claude.ai").first
        first_email.click()
        time.sleep(6)
        shoot(gmail_page, "4a-email-opened")
        for a in gmail_page.locator("a").all():
            try:
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            h = href.lower()
            if "claude" in h and ("login" in h or "magic" in h or "verify" in h or "token=" in h):
                secure_link = href
                print("  found link: [redacted]", flush=True)
                break
        if not secure_link:
            body = gmail_page.inner_text("body")[:2000]
            m = re.search(r"(https?://[^\s\"'<>]*claude[^\s\"'<>]*(?:login|magic|verify|token=)[^\s\"'<>]*)", body, re.I)
            if m:
                secure_link = m.group(1)
                print("  found link via text regex: [redacted]", flush=True)
            else:
                print(f"  ⚠️ no link found, page excerpt: {body[:500]!r}", flush=True)
    except Exception as e:
        print(f"  ⚠️ email click failed: {e}", flush=True)
        shoot(gmail_page, "4b-fail")

    if not secure_link:
        br.close()
        raise SystemExit("NO_SECURE_LINK")

    # ── Step 3: code_page goto magic-link, 抓 6-digit code ────────────
    print("[3] code_page: open magic-link + extract 6-digit code", flush=True)
    code_page = ctx.new_page()
    code_page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    if "moment" in (code_page.title() or "").lower():
        wait_past_turnstile(code_page, 60)
        time.sleep(3)
    shoot(code_page, "5-magic-link")
    body = code_page.inner_text("body")
    m = re.search(r"\b(\d{6})\b", body)
    if not m:
        print(f"  ❌ no 6-digit code. body: {body[:500]!r}", flush=True)
        br.close()
        raise SystemExit("NO_VERIFY_CODE")
    verify_code = m.group(1)
    print(f"  ✅ verify code: {verify_code}", flush=True)

    # ── Step 4: 回 claude_page (still alive) ──────────────────────────
    print("[4] claude_page: enter verify code + complete OAuth", flush=True)
    claude_page.bring_to_front()
    code_in = claude_page.locator("input[placeholder='Enter verification code']").first
    if code_in.count() == 0 or not code_in.is_visible():
        print(f"  ⚠️ verify-code input no longer visible on claude_page", flush=True)
        shoot(claude_page, "6a-stale-claude-page")
        br.close()
        raise SystemExit("CLAUDE_PAGE_STALE")
    code_in.click()
    code_in.fill(verify_code)
    shoot(claude_page, "6-code-filled")
    vbtn = claude_page.get_by_role("button", name=re.compile(r"verify email address|verify|continue", re.I))
    if vbtn.count() > 0:
        vbtn.first.click()
    else:
        claude_page.keyboard.press("Enter")
    time.sleep(10)
    shoot(claude_page, "7-after-verify")

    body_text = claude_page.inner_text("body")
    if "invited you to join" in body_text.lower():
        print("  Team invite — clicking Accept", flush=True)
        accept_btn = claude_page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if accept_btn.count() > 0:
            accept_btn.first.click()
            time.sleep(8)
            shoot(claude_page, "7a-after-accept")

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
