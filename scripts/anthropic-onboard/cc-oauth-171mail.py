#!/usr/bin/env python3
"""
cc-oauth-171mail.py — Claude Code Max OAuth 全自动 (patchright + 171mail relay-mail)

KEY DIFFERENCE vs cc-oauth-full.py (Gmail) / cc-oauth-outlook.py:
  - 邮箱身份**免登录**:claude.ai 把 magic-link 邮件发到 Mayo_xxx@therapist.net,
    实际收件由 b.171mail.com relay-mail 服务托管,凭一个 `sk-ant-sid02-` 开头的
    relay token (账号字段第三段) 即可拉取该邮箱的最新 Claude magic-link。
  - 因此不需要 mail_pw / TOTP,只需要 RELAY_TOKEN。
  - 取信 URL:https://b.171mail.com/#/home/code?type=claude (token 模板,token 段留空)
    用户原话"粘贴令牌后点击获取"= **patchright 必须显式 fill 输入框,不是把 token 拼 URL**
    (实测 2026-05-25:URL 后塞 token=... → SPA 不识别,点"获取验证码"按空 token 投递 → 报"无效的令牌")

KEY DISCOVERIES (delta from Gmail/Outlook 流程):
  1. 卖号商提供的"成品号"含 3 段:email----mail_pw----relay_token
     - 第 1 段:claude.ai 注册邮箱 (例 Mayo_Haneynns@therapist.net)
     - 第 2 段:**mail.com webmail 密码** (本流程不消费,只在 relay token 失效时手动救场)
     - 第 3 段:**171mail relay token** (sk-ant-sid02- 开头,命名巧合不是 OAuth token)
  2. 171mail SPA UI 元素 (2026-05-25 实测):
     - 输入框附近 label 是 "查询令牌"
     - 触发按钮文字 "获取验证码"
     - 失败提示 "无效的令牌"
     - 项目选择已固定 type=claude (URL 段决定),无需选下拉
  3. Magic-link 抓取:在 171mail 渲染后的 DOM 里找 a[href*='claude'] 或纯文本 URL

FLOW:
  A. 外部 tmux 跑 `claude setup-token` 拿 OAuth URL → 传入 CC_OAUTH_URL
  B. 本脚本 (Docker patchright):
     1. claude_page  打开 OAuth URL → Turnstile → 填 email → "Continue with email"
     2. relay_page   goto b.171mail.com/#/home/code?type=claude
     3. relay_page   fill RELAY_TOKEN 到 "查询令牌" 输入框 + click "获取验证码"
     4. relay_page   等 XHR 返回 → 抓 magic-link
     5. claude_page  goto magic-link → 若 Team 邀请 → Accept → 主动 goto OAuth URL
     6. claude_page  点 Authorize → 抓 callback URL ?code=xxx
  C. 外部 tmux send-keys 粘 code → setup-token 打印 sk-ant-oat token

ENV:
  CC_EMAIL        Mayo_Haneynns@therapist.net (claude.ai 注册邮箱)
  RELAY_TOKEN     sk-ant-sid02-xxx (171mail 接码令牌,账号第三段)
  CC_OAUTH_URL    https://claude.com/cai/oauth/authorize?... (从 claude setup-token 拿)
"""
import os, re, time
from patchright.sync_api import sync_playwright

EMAIL = os.environ["CC_EMAIL"]
RELAY_TOKEN = os.environ["RELAY_TOKEN"]
OAUTH_URL = os.environ["CC_OAUTH_URL"]
SS = "/work/screenshots"
os.makedirs(SS, exist_ok=True)

# 171mail relay URL — type=claude 锁定项目,token 用 page.fill 显式投递
RELAY_URL = "https://b.171mail.com/#/home/code?type=claude"


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


def scan_for_magic_link(page):
    """
    从 171mail 渲染后的页面里捞 claude.ai magic-link。
    考虑两种渲染:
      - 邮件正文 <a href="https://claude.ai/..."> 直接渲染
      - 仅显示纯文本 URL (innerText 含 https://claude.ai/...)
    """
    # 优先 a[href]
    for a in page.locator("a").all():
        href = (a.get_attribute("href") or "").lower()
        if "claude" in href and ("login" in href or "magic" in href or "verify" in href or "token=" in href):
            return a.get_attribute("href")
    # 退化 innerText 正则
    body = page.inner_text("body")
    m = re.search(r"(https?://[^\s\"'<>]*claude[^\s\"'<>]*(?:login|magic|verify|token=)[^\s\"'<>]*)", body, re.I)
    if m:
        return m.group(1)
    return None


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    claude_page = ctx.new_page()

    # ── Step 1: 打开 OAuth URL,过 Turnstile ──────────────────────
    print(f"[1] Open OAuth URL on claude_page", flush=True)
    trigger_ts = int(time.time())
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

    # ── Step 3: 新 tab 开 171mail relay (空 token 模板) ─────────────
    print(f"[3] Open 171mail relay + fill token", flush=True)
    relay_page = ctx.new_page()
    # 给 claude.ai 一点投递时间 (邮件链路通常 < 30s)
    time.sleep(8)
    relay_page.goto(RELAY_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    shoot(relay_page, "03-relay-landing")
    print(f"  relay url: {relay_page.url[:120]}", flush=True)

    # 找 "查询令牌" 输入框 + fill RELAY_TOKEN
    # 实测 SPA 元素特征:textarea 或 input[type=text] 在 "查询令牌" 标签附近
    filled = False
    for sel in [
        "textarea",
        "input[type='text']",
        "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])",
    ]:
        try:
            inputs = relay_page.locator(sel).all()
            for inp in inputs:
                if inp.is_visible():
                    inp.click()
                    # 先清干净 (可能 SPA 默认填了占位)
                    relay_page.keyboard.press("Control+a")
                    relay_page.keyboard.press("Delete")
                    inp.fill(RELAY_TOKEN)
                    print(f"  [relay] filled token via selector '{sel}' (len={len(RELAY_TOKEN)})", flush=True)
                    filled = True
                    break
            if filled:
                break
        except Exception as e:
            print(f"  selector {sel} failed: {e}", flush=True)

    if not filled:
        print(f"  ⚠️ no input found, dumping body for debug", flush=True)
        print(f"  body: {relay_page.inner_text('body')[:1500]!r}", flush=True)
        shoot(relay_page, "03b-no-input")

    time.sleep(2)
    shoot(relay_page, "03c-after-fill")

    # 点 "获取验证码" 按钮
    clicked = False
    for label_pat in [r"^获取验证码$", r"获取验证码", r"^获取$", r"^fetch$", r"^submit$"]:
        try:
            b = relay_page.get_by_role("button", name=re.compile(label_pat, re.I))
            if b.count() > 0 and b.first.is_visible():
                print(f"  [relay] clicking '{label_pat}'", flush=True)
                b.first.click()
                clicked = True
                break
        except Exception:
            pass
    if not clicked:
        # text fallback
        try:
            t = relay_page.get_by_text(re.compile(r"获取验证码", re.I))
            if t.count() > 0:
                t.first.click()
                clicked = True
                print(f"  [relay] clicked via text fallback", flush=True)
        except Exception:
            pass

    time.sleep(6)
    shoot(relay_page, "03d-after-click-fetch")

    # ── Step 4: 在 relay_page 找 claude.ai magic-link ──────────────
    print(f"[4] Scan relay page for claude magic-link", flush=True)
    secure_link = None
    deadline = time.time() + 120
    while time.time() < deadline:
        # 检查无效令牌
        body_now = relay_page.inner_text("body")
        if "无效的令牌" in body_now or "令牌错误" in body_now or "token" in body_now.lower() and "invalid" in body_now.lower():
            print(f"  ❌ relay rejected token: {body_now[:400]!r}", flush=True)
            shoot(relay_page, "04-token-rejected")
            br.close()
            raise SystemExit("RELAY_TOKEN_INVALID")

        secure_link = scan_for_magic_link(relay_page)
        if secure_link:
            print(f"  found link: {secure_link[:100]}", flush=True)
            break

        # 邮件可能 30-60s 才到, 多试 "获取验证码"
        time.sleep(8)
        try:
            b = relay_page.get_by_role("button", name=re.compile(r"获取验证码|^获取$", re.I))
            if b.count() > 0 and b.first.is_visible() and b.first.is_enabled():
                b.first.click()
                time.sleep(3)
        except Exception:
            pass

    shoot(relay_page, "04-relay-fetched")

    if not secure_link:
        body = relay_page.inner_text("body")[:2500]
        print(f"  ❌ No claude magic-link in relay. body excerpt: {body!r}", flush=True)
        shoot(relay_page, "04-fail")
        br.close()
        raise SystemExit("NO_SECURE_LINK")

    # ── Step 5: claude_page goto magic-link ───────────────────────
    print(f"[5] Open magic-link in claude_page", flush=True)
    claude_page.bring_to_front()
    claude_page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    shoot(claude_page, "05-after-magic-link")
    print(f"  url={claude_page.url[:120]}", flush=True)

    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)
        shoot(claude_page, "05b-after-ts")

    # ── Step 5b: 若 Team 邀请页 → Accept invite ────────────────────
    body_text = claude_page.inner_text("body")
    if "invited you to join" in body_text:
        print(f"[5b] Team invite detected, clicking Accept", flush=True)
        accept_btn = claude_page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if accept_btn.count() == 0:
            accept_btn = claude_page.get_by_text(re.compile(r"^Accept invite$", re.I))
        if accept_btn.count() > 0:
            accept_btn.first.click()
            time.sleep(8)
            shoot(claude_page, "05c-after-accept-invite")

    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)

    # ── Step 5c: Accept 后跳 /new → 主动 navigate 回 OAuth URL ─────
    if "/new" in claude_page.url or "claude.ai/projects" in claude_page.url or "claude.ai/chats" in claude_page.url:
        print(f"  [5c] At /new — navigate back to OAuth URL", flush=True)
        claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        shoot(claude_page, "05d-back-to-oauth")

    # ── Step 6: 点 Authorize ──────────────────────────────────────
    print(f"[6] Click Authorize", flush=True)
    auth_btn = claude_page.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
    if auth_btn.count() > 0:
        shoot(claude_page, "06-authorize-page")
        auth_btn.first.click()
        time.sleep(6)
    shoot(claude_page, "07-after-authorize")
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
