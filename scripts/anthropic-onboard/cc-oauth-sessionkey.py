#!/usr/bin/env python3
"""
cc-oauth-sessionkey.py — Session cookie 直接注入,跳过所有邮箱介质 (Gmail/Outlook/mail.com/171mail)。

KEY INSIGHT (2026-05-25):
  卖号商"Max 20× 成品号"的格式声明是:
    Claude账号/邮箱----邮箱密码----接码令牌----Claude Sk
  实际给 3 段 (接码令牌位常省略):
    Mayo_xxx@therapist.net----DmKP5ifOiUU----sk-ant-sid02-...
  最后一段 `sk-ant-sid02-...` 的 prefix 是 Anthropic 自家命名规律:
    sk-ant-api03-  = API key
    sk-ant-oat01-  = OAuth token (claude setup-token 拿的)
    sk-ant-sid02-  = **Session ID v2** ← 直接是 claude.ai 的 session cookie
  → "免登录邮箱流程" = 把这段 session cookie 注入 .claude.ai domain → 已登录态
  → 不消费 mail_pw 也不需要 magic-link

FLOW:
  A. 外部 tmux 跑 `claude setup-token` 拿 OAuth URL → 传入 CC_OAUTH_URL
  B. 本脚本 (Docker patchright):
     1. ctx.add_cookies([{name:'sessionKey', value:SESSION_KEY, domain:'.claude.ai'}])
     2. claude_page.goto(OAUTH_URL) → 已登录 → 跳到 Authorize 页 (或 Team invite)
     3. 若 Team 邀请 → Accept → 主动 goto OAUTH_URL 二次
     4. 点 Authorize → 抓 callback URL ?code=xxx
  C. 外部 tmux send-keys 粘 code → setup-token 打印 sk-ant-oat token

ENV:
  CC_EMAIL        Mayo_Haneynns@therapist.net (只用于日志/identification)
  SESSION_KEY     sk-ant-sid02-... (cookie value)
  CC_OAUTH_URL    https://claude.com/cai/oauth/authorize?...

COOKIE NAME 候选 (按命中概率):
  - sessionKey   (Anthropic CLI ~/.claude/.credentials.json 也用此 key)
  - session_key
  - claude_session
  脚本一次性注入多个候选,domain 加 .claude.ai / .anthropic.com 都加。
"""
import os, re, time
from patchright.sync_api import sync_playwright

EMAIL = os.environ.get("CC_EMAIL", "(unknown)")
SESSION_KEY = os.environ["SESSION_KEY"]
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


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")

    # ── Step 1: 注入 sessionKey cookie ─────────────────────────────
    print(f"[1] Inject sessionKey cookie (email={EMAIL}, key_len={len(SESSION_KEY)})", flush=True)
    expires = int(time.time()) + 30 * 86400  # 30 days
    cookie_candidates = []
    for name in ["sessionKey", "session_key", "claude_session", "anthropic_session"]:
        for domain in [".claude.ai", ".claude.com", ".anthropic.com"]:
            cookie_candidates.append({
                "name": name,
                "value": SESSION_KEY,
                "domain": domain,
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
                "expires": expires,
            })
    ctx.add_cookies(cookie_candidates)
    print(f"  injected {len(cookie_candidates)} cookie variants", flush=True)

    claude_page = ctx.new_page()

    # ── Step 2: goto OAuth URL — 若 cookie 生效,直接到 Authorize 页 ─
    print(f"[2] Open OAuth URL", flush=True)
    claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 90)
        time.sleep(3)
    shoot(claude_page, "01-after-cookie-goto")
    print(f"  url: {claude_page.url[:150]}", flush=True)
    body_excerpt = claude_page.inner_text("body")[:600]
    print(f"  body: {body_excerpt!r}", flush=True)

    # 诊断: 如果 cookie 有效,URL 应该是 oauth/authorize 页或 invite 页
    # 如果 cookie 无效,URL 会跳 login 页 (含 email input)
    if "input" in claude_page.content().lower() and "email" in body_excerpt.lower() \
            and ("continue" in body_excerpt.lower() or "log in" in body_excerpt.lower()):
        print(f"  ⚠️ cookie 似乎未生效, 页面仍要求 email login", flush=True)
        # 不立即退,让后续 Accept invite / Authorize 检测兜底; 但记录
        shoot(claude_page, "01b-cookie-not-applied")

    # ── Step 3: 若 Team 邀请页 → Accept invite ─────────────────────
    body_text = claude_page.inner_text("body")
    if "invited you to join" in body_text:
        print(f"[3] Team invite detected, clicking Accept", flush=True)
        accept_btn = claude_page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if accept_btn.count() == 0:
            accept_btn = claude_page.get_by_text(re.compile(r"^Accept invite$", re.I))
        if accept_btn.count() > 0:
            accept_btn.first.click()
            time.sleep(8)
            shoot(claude_page, "02-after-accept-invite")

    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)

    # ── Step 4: Accept 后默认跳 /new → 主动 goto OAuth URL ──────────
    if any(x in claude_page.url for x in ("/new", "claude.ai/projects", "claude.ai/chats")):
        print(f"  [4] At /new — navigate back to OAuth URL", flush=True)
        claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        shoot(claude_page, "03-back-to-oauth")
        print(f"  url: {claude_page.url[:150]}", flush=True)

    # ── Step 5: 点 Authorize ──────────────────────────────────────
    print(f"[5] Click Authorize", flush=True)
    shoot(claude_page, "04-pre-authorize")
    auth_btn = claude_page.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
    if auth_btn.count() > 0:
        auth_btn.first.click()
        time.sleep(6)
    else:
        print(f"  ⚠️ no Authorize button. body: {claude_page.inner_text('body')[:600]!r}", flush=True)
    shoot(claude_page, "05-after-authorize")
    print(f"  url: {claude_page.url[:150]}", flush=True)

    # ── Step 6: 抓 callback code ──────────────────────────────────
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
