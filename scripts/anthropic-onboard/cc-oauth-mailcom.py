#!/usr/bin/env python3
"""
cc-oauth-mailcom.py — mail.com (1and1) webmail 变体,平行 cc-oauth-outlook.py / cc-oauth-full.py

CONTEXT (2026-05-25):
  171mail relay token 模式实测踩坑——这批"Max 20× 成品号"的 sk-ant-sid02- token
  常被服务端拒为"无效的令牌"(已过期 / 已消费 / 卖家给错)。**用户原话**:
    "邮箱登录地址:mail.com"
  + mail_pw 字段 (e.g. DmKP5ifOiUU) 实际是 mail.com webmail 登录密码。
  绕过 171mail relay,直接登 www.mail.com webmail 收 Claude magic-link 是稳定 fallback。

适用账号特征:
  - email 域名是 mail.com 系 (@therapist.net / @gmx.com / @consultant.com 都属 1and1)
  - 卖家成品号字段 `email----mail_pw----...` 第 2 段是 mail.com webmail 密码
  - 卖家文案提"邮箱登录地址:mail.com"

DIFFERENCES from Outlook flow:
  - Step 3: www.mail.com (不是 login.live.com); login form 用 placeholder 定位
  - Step 4: 邮件列表在 frame[name='mail'] iframe 里 (mail.com webmail SPA 特性)
    搜 Anthropic / Secure link / Claude.ai 字样
  - 没有 "Stay signed in" / "Use your password" 切换页

HYBRID BROWSER STRATEGY (2026-05-25 实测):
  - Step 1-2, 5-7 (claude.ai) → **patchright** 必须 (CF Turnstile 看 TLS 指纹)
  - Step 3-4 (mail.com)         → **vanilla playwright** 必须
    (实测 patchright stealth 改装跟 mail.com "Log in" SPA hash anchor 不兼容,
     a:has-text('Log in') click 后页面不跳;vanilla playwright + webmail-otp 路径
     2026-05-18 跑通过)
  两个 sync_playwright 各启独立 browser instance,共存于同一 Python 进程。

ENV:
  CC_EMAIL        Mayo_Haneynns@therapist.net
  MAIL_PW         mail.com webmail 密码 (e.g. DmKP5ifOiUU)
  CC_OAUTH_URL    https://claude.com/cai/oauth/authorize?...

Docker 调用同 outlook (patchright==1.59.1 + playwright v1.59.0-noble image 自带 vanilla playwright)。
"""
import os, re, time
from patchright.sync_api import sync_playwright as patchright_pw
from playwright.sync_api import sync_playwright as vanilla_pw

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
    """mail.com webmail 邮件列表在 frame[name='mail'] iframe 里"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for fr in page.frames:
            if fr.name == "mail":
                return fr
        time.sleep(2)
    return None


with patchright_pw() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    claude_page = ctx.new_page()

    # ── Step 1: 打开 OAuth URL,过 Turnstile ──────────────────────
    print(f"[1] Open OAuth URL on claude_page", flush=True)
    claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 90)
        time.sleep(3)
    shoot(claude_page, "01-claude-landing")

    # ── Step 2: 填 email + Continue with email ────────────────────
    print(f"[2] Fill email + trigger magic link", flush=True)
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
    time.sleep(6)
    shoot(claude_page, "02-claude-after-email")

    # ── Step 3-4 (vanilla playwright): mail.com 登录 + 取信 ────────
    # 必须用 vanilla playwright,patchright stealth 改装 click "Log in" 失效
    print(f"[3] Open vanilla playwright for mail.com side-channel", flush=True)
    secure_link = None
    with vanilla_pw() as vpw:
        mail_br = vpw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
        mail_ctx = mail_br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
        mail_page = mail_ctx.new_page()
        mail_page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
        shoot(mail_page, "03-mailcom-landing")

        # 处理 cookie consent banner (GDPR 弹层常挡住按钮)
        for label_pat in [r"accept all", r"agree", r"i agree", r"同意", r"接受", r"^ok$"]:
            try:
                b = mail_page.get_by_role("button", name=re.compile(label_pat, re.I))
                if b.count() > 0 and b.first.is_visible():
                    print(f"  [3a] dismissing cookie banner via '{label_pat}'", flush=True)
                    b.first.click()
                    time.sleep(2)
                    break
            except Exception:
                pass

        # 点 "Log in" — vanilla playwright 跑得动这个 SPA hash anchor
        login_clicked = False
        for sel in [
            "a:has-text('Log in')",
            "button:has-text('Log in')",
            "a[href*='login']",
        ]:
            try:
                cand = mail_page.locator(sel)
                for i in range(cand.count()):
                    el = cand.nth(i)
                    if el.is_visible():
                        print(f"  [3b] clicking login via '{sel}' #{i}", flush=True)
                        el.click()
                        login_clicked = True
                        break
                if login_clicked:
                    break
            except Exception as e:
                print(f"    {sel} err: {e}", flush=True)

        time.sleep(5)
        shoot(mail_page, "03c-after-login-click")

        # email + password 同页 (mail.com 标准)
        try:
            mail_page.wait_for_selector(
                "input[placeholder='Email address'], input[placeholder*='Email' i], "
                "input[type='email'], input[name='username']",
                timeout=20000,
            )
        except Exception:
            body = mail_page.inner_text("body")[:1200]
            print(f"  ⚠️ login form not loaded. url={mail_page.url} body: {body!r}", flush=True)
            shoot(mail_page, "03e-mailcom-no-form")
            mail_br.close()
            raise SystemExit("MAILCOM_NO_LOGIN_FORM")

        email_input = mail_page.locator("input[placeholder='Email address']").first
        if email_input.count() == 0:
            email_input = mail_page.locator("input[type='email']").first
        email_input.click()
        email_input.fill(EMAIL)

        pw_input = mail_page.locator("input[placeholder='Password']").first
        if pw_input.count() == 0:
            pw_input = mail_page.locator("input[type='password']").first
        pw_input.click()
        pw_input.fill(MAIL_PW)
        shoot(mail_page, "04-mailcom-filled")

        # 点 "Log in" 按钮 (页面上有多个,选可见且非顶部 nav 的)
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
        shoot(mail_page, "05-mailcom-after-login")
        print(f"  after-login url: {mail_page.url[:120]}", flush=True)

        # ── Step 4: 进 mail frame, 找 Anthropic Secure link ──────
        print(f"[4] Wait for mail frame, then scan for Anthropic email", flush=True)
        time.sleep(60)

        fr = get_mail_frame(mail_page, timeout=30)
        if not fr:
            body = mail_page.inner_text("body")[:1000]
            print(f"  ⚠️ no mail frame after login. body: {body!r}", flush=True)
            shoot(mail_page, "06-no-mail-frame")
            mail_br.close()
            raise SystemExit("MAILCOM_NO_MAIL_FRAME")

        shoot(mail_page, "06-mailcom-inbox")
        print(f"  mail frame found", flush=True)

        SENDER_HINTS_RE = re.compile(r"anthropic|claude|secure link", re.I)

        # 重试 5 次,每次 sleep 15s 等邮件到/list 刷新
        for attempt in range(5):
            if secure_link:
                break
            print(f"  [4.{attempt+1}] scanning inbox", flush=True)
            try:
                text = fr.evaluate("() => document.body.innerText")
            except Exception as e:
                print(f"    eval err: {e}", flush=True)
                time.sleep(8)
                continue

            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            target_line = None
            for ln in lines:
                if SENDER_HINTS_RE.search(ln):
                    target_line = ln
                    print(f"    hint match: {ln[:80]!r}", flush=True)
                    break

            if not target_line:
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
                fr.get_by_text(target_line, exact=False).first.click()
                time.sleep(6)
                shoot(mail_page, f"07-mailcom-opened-{attempt}")
            except Exception as e:
                print(f"    click err: {e}", flush=True)
                time.sleep(8)
                continue

            # 邮件正文可能在 mail frame 或另一 frame;扫所有 frame
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
                            print(f"    found via a[href]: {href[:120]}", flush=True)
                            break
                    if secure_link:
                        break
                    body = f.evaluate("() => document.body.innerText")
                    m = re.search(r"(https?://[^\s\"'<>]*claude[^\s\"'<>]*(?:login|magic|verify|token=)[^\s\"'<>]*)", body, re.I)
                    if m:
                        secure_link = m.group(1)
                        print(f"    found via innerText: {secure_link[:120]}", flush=True)
                        break
                except Exception:
                    continue

            if not secure_link:
                print(f"    no claude link in opened email,等 15s 再试", flush=True)
                time.sleep(15)

        shoot(mail_page, "08-mailcom-final")
        mail_br.close()

    if not secure_link:
        print(f"  ❌ No secure link found after retries", flush=True)
        br.close()
        raise SystemExit("NO_SECURE_LINK")

    # ── Step 5+ : 同 Gmail / Outlook 流程 ─────────────────────────
    print(f"[5] Open secure link in claude_page", flush=True)
    claude_page.bring_to_front()
    claude_page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    shoot(claude_page, "09-after-magic-link")

    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)

    body_text = claude_page.inner_text("body")
    if "invited you to join" in body_text:
        print(f"[5b] Team invite — clicking Accept", flush=True)
        accept_btn = claude_page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if accept_btn.count() > 0:
            accept_btn.first.click()
            time.sleep(8)
            shoot(claude_page, "09b-after-accept")

    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)

    if any(x in claude_page.url for x in ("/new", "claude.ai/projects", "claude.ai/chats")):
        print(f"  [5c] At /new — navigate back to OAuth URL", flush=True)
        claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        shoot(claude_page, "09c-back-to-oauth")

    print(f"[6] Click Authorize", flush=True)
    auth_btn = claude_page.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
    if auth_btn.count() > 0:
        auth_btn.first.click()
        time.sleep(6)
    shoot(claude_page, "10-after-authorize")

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
