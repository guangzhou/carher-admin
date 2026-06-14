#!/usr/bin/env python3
"""
cc-oauth-full.py — Claude Code Max OAuth 全自动 (patchright + Gmail TOTP + magic-link)

KEY DISCOVERIES (2026-05-21, don't re-debug):
  1. claude.ai 登录路径是 "magic link" (邮件里点链接登录),不是 6 位 OTP code
  2. 卖号场景:claude.ai 注册时绑 gmail,真正登录认证走 gmail (password + Google 2FA TOTP)
     - 账号字段 `Q!*OP5qO9u2` 对应 **Gmail 密码**(不是 claude.ai 密码,claude.ai 没有密码登录)
     - TOTP secret 对应 **Gmail 2FA**(不是 claude.ai 2FA,claude.ai 暂无强制 2FA)
     - "辅助邮箱"字段实际很多场景没用上,因为 Gmail 直接收 claude.ai magic link
  3. Team 账号首次登录会弹 "Accept invite" 页(卖家先邀请到 Team workspace),
     必须点 Accept,然后**主动 navigate 回 OAuth URL**(否则跳到 /new 主页,绕过 Authorize)
  4. Cloudflare Turnstile 用 patchright 点 checkbox 自动通过
  5. setup-token 的 tmux session 接收 callback code 后,token 直接打印到 stdout
     (格式 `sk-ant-oat01-xxx`,1 年寿命)

FLOW (在 188 上跑,与 setup-token 配合):
  A. 外面: tmux 跑 `claude setup-token` 拿 OAuth URL → 传入本脚本 CC_OAUTH_URL
  B. 本脚本 (Docker patchright):
     1. claude_page  打开 OAuth URL → Turnstile → 填 email → Continue with email
     2. gmail_page   accounts.google.com 登 Gmail (password + Google TOTP)
     3. gmail_page   搜 "from:anthropic Secure link" → 点开第一封 → 抓 magic-link
     4. claude_page  goto magic-link → 自动登录 → 若 Team 邀请页 → Accept
     5. claude_page  goto OAuth URL 二次(因 Accept 后跳 /new)→ 点 Authorize
     6. 抓 callback URL ?code=xxx,return code
  C. 外面: tmux send-keys 把 code 粘回 setup-token → 打印 sk-ant-oat token

ENV:
  CC_EMAIL        thomasmatthewlkgmx1915@gmail.com
  GMAIL_PW        Q!*OP5qO9u2 (= Gmail 登录密码)
  GMAIL_TOTP      7fulcv2dcp5mgh4mo5wztqismwbauymt (Gmail 2FA TOTP secret)
  CC_OAUTH_URL    https://claude.com/cai/oauth/authorize?... (从 claude setup-token 拿)

DOCKER RUN (on 188):
  docker run --rm \\
    -v /path/to/cc-oauth-full.py:/work/script.py:ro \\
    -v /tmp/cc-screenshots:/work/screenshots \\
    -e CC_EMAIL=... -e GMAIL_PW='...' -e GMAIL_TOTP=... -e CC_OAUTH_URL='...' \\
    -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 \\
    mcr.microsoft.com/playwright/python:v1.59.0-noble \\
    bash -c "Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1 && \\
             pip install patchright pyotp -q --root-user-action=ignore && \\
             python3 /work/script.py"

KNOWN PITFALL:
  - Team 账号 Opus/Sonnet 可能被卖家其他买家打到 rate_limit_error;Haiku 通常还能用
    验证 token 有效性时用 Haiku 4.5 探针(input ≤ 10 tokens 即可)
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
    p.screenshot(path=f"{SS}/{name}.png")
    print(f"  shot: {name}.png", flush=True)


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


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width":1280,"height":800}, locale="en-US")
    claude_page = ctx.new_page()

    # ── Step 1: 打开 OAuth URL,过 Turnstile ──────────────────────
    print(f"[1] Open OAuth URL on claude_page", flush=True)
    trigger_ts = int(time.time())  # 记录触发时间,后面用来找 inbox 新邮件
    claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 90)
        time.sleep(3)
    shoot(claude_page, "01-claude-landing")
    print(f"  url={claude_page.url}", flush=True)

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
    print(f"  Triggered. trigger_ts={trigger_ts}", flush=True)

    # ── Step 3: 新 tab 登 Gmail ───────────────────────────────────
    print(f"[3] Open Gmail in new tab", flush=True)
    gmail_page = ctx.new_page()
    gmail_page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    # email
    gmail_page.wait_for_selector("input[type='email']", timeout=15000)
    gmail_page.locator("input[type='email']").first.click()
    gmail_page.keyboard.type(EMAIL, delay=80)
    gmail_page.get_by_role("button", name=re.compile(r"^next$", re.I)).first.click()
    time.sleep(6)
    shoot(gmail_page, "03-gmail-after-email")
    # password
    gmail_page.wait_for_selector("input[type='password']", timeout=15000)
    gmail_page.locator("input[type='password']").first.click()
    gmail_page.keyboard.type(GMAIL_PW, delay=80)
    gmail_page.get_by_role("button", name=re.compile(r"^next$", re.I)).first.click()
    time.sleep(6)
    shoot(gmail_page, "04-gmail-after-pw")
    # TOTP
    if "totp" in gmail_page.url.lower() or "2-step" in gmail_page.content().lower():
        totp = pyotp.TOTP(GMAIL_TOTP).now()
        print(f"  Gmail TOTP = {totp}", flush=True)
        # 找 visible input
        for inp in gmail_page.locator("input").all():
            if inp.is_visible() and inp.get_attribute("type") in ("text","tel",None):
                inp.click()
                gmail_page.keyboard.type(totp, delay=80)
                break
        gmail_page.get_by_role("button", name=re.compile(r"^next$|verify", re.I)).first.click()
        time.sleep(8)
        shoot(gmail_page, "05-gmail-after-totp")

    print(f"  Gmail logged in: url={gmail_page.url[:80]}", flush=True)

    # ── Step 4: 找最新 Anthropic 邮件,抓 secure link ──────────────
    print(f"[4] Search Anthropic emails for secure link", flush=True)
    # 用 Gmail search 找最新
    gmail_page.goto("https://mail.google.com/mail/u/0/#search/from%3Aanthropic+subject%3A(Secure+link)+newer_than%3A1d",
                    wait_until="domcontentloaded", timeout=30000)
    time.sleep(10)
    shoot(gmail_page, "06-gmail-search")

    # 点最上面那封 (第一个对话)
    # Gmail Web UI: 邮件列表里每行是 tr role=row,subject 在 td 里
    # 我直接找含 "Secure link to log in to Claude.ai" 文字的第一行点开
    secure_link = None
    try:
        # 点最上面那条 email row
        first_email = gmail_page.locator("tr").filter(has_text="Secure link to log in to Claude.ai").first
        first_email.click()
        time.sleep(6)
        shoot(gmail_page, "07-email-opened")
        # 邮件里找 "Sign in to Claude.ai" 链接
        # link href 通常是 https://claude.ai/magic-link?token=... 或 https://claude.ai/login?...
        # 用 page.locator('a').all() 提取所有链接
        for a in gmail_page.locator("a").all():
            href = a.get_attribute("href") or ""
            if "claude" in href.lower() and ("login" in href.lower() or "magic" in href.lower() or "verify" in href.lower() or "token=" in href.lower()):
                secure_link = href
                print(f"  found link: {href[:100]}", flush=True)
                break
        if not secure_link:
            # fallback: print page text to debug
            body = gmail_page.inner_text("body")[:2000]
            print(f"  ⚠️ no link found, page excerpt: {body!r}", flush=True)
    except Exception as e:
        print(f"  ⚠️ email click failed: {e}", flush=True)
        shoot(gmail_page, "07-fail")

    if not secure_link:
        print(f"  ❌ No secure link found", flush=True)
        br.close()
        raise SystemExit("NO_SECURE_LINK")

    # ── Step 5: claude_page 导航到 secure link ─────────────────────
    print(f"[5] Open secure link in claude_page", flush=True)
    claude_page.bring_to_front()
    claude_page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    shoot(claude_page, "08-after-magic-link")
    print(f"  url={claude_page.url[:120]}", flush=True)
    print(f"  body: {claude_page.inner_text('body')[:400]!r}", flush=True)

    # 可能又有 Turnstile
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)
        shoot(claude_page, "08b-after-ts")

    # ── Step 6: 如果有 Authorize 按钮就点 ──────────────────────────
    # ── Step 5b: 如果有 Team 邀请页面,先点 Accept invite ─────────
    body_text = claude_page.inner_text("body")
    if "Join " in body_text and "invited you to join" in body_text:
        print(f"[5b] Team invite page detected, clicking Accept", flush=True)
        accept_btn = claude_page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if accept_btn.count() == 0:
            accept_btn = claude_page.get_by_text(re.compile(r"^Accept invite$", re.I))
        if accept_btn.count() > 0:
            accept_btn.first.click()
            time.sleep(8)
            shoot(claude_page, "08c-after-accept-invite")
            print(f"  url after accept: {claude_page.url[:120]}", flush=True)

    # 可能再过 Turnstile
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)

    # ── Step 5c: Accept 后默认跳 /new, 主动 navigate 回原 OAuth URL ─
    if "/new" in claude_page.url or "claude.ai/projects" in claude_page.url or "claude.ai/chats" in claude_page.url:
        print(f"  [5c] At /new — navigate back to OAuth URL", flush=True)
        claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        shoot(claude_page, "08d-back-to-oauth")
        print(f"  url: {claude_page.url[:150]}", flush=True)
        body = claude_page.inner_text("body")[:400]
        print(f"  body: {body!r}", flush=True)

    print(f"[6] Click Authorize", flush=True)
    auth_btn = claude_page.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
    if auth_btn.count() > 0:
        shoot(claude_page, "09-authorize-page")
        auth_btn.first.click()
        time.sleep(6)
    shoot(claude_page, "10-after-authorize")
    print(f"  url={claude_page.url[:150]}", flush=True)

    # ── Step 7: 抓 callback code ──────────────────────────────────
    body_text = claude_page.inner_text("body")
    m = re.search(r"[?&]code=([^&\s]+)", claude_page.url)
    if m:
        print(f"\n✅ CALLBACK_CODE={m.group(1)}", flush=True)
    else:
        m = re.search(r"\b([a-zA-Z0-9_-]{50,}#[a-zA-Z0-9_-]{20,})\b", body_text)
        if m:
            print(f"\n✅ CALLBACK_CODE={m.group(1)}", flush=True)
        else:
            print(f"\n❌ No code found. body: {body_text[:1500]!r}", flush=True)

    br.close()
