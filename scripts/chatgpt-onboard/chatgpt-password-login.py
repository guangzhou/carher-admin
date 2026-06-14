#!/usr/bin/env python3
"""
chatgpt-password-login.py — Playwright 登录 auth.openai.com（密码 + mail.com OTP），提取 auth.json

用法（188 上 Docker 运行）：
  MAIL_USER=EmilyOconnorgvg@mail.com \
  MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \      # webmail 密码（字段A）
  CHATGPT_PW_FILE=/run/chatgpt_pw.txt \      # ChatGPT 登录密码（字段B）
  AUTH_JSON_OUTPUT=/work/auth.json \
  SCREENSHOT_DIR=/work/screenshots \
  python3 chatgpt-password-login.py
"""

import os, re, sys, json, base64, time
from playwright.sync_api import sync_playwright

EMAIL       = os.environ["MAIL_USER"]
MAIL_PW     = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
CHATGPT_PW  = open(os.environ["CHATGPT_PW_FILE"]).read().strip()
SS_DIR      = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
AUTH_OUT    = os.environ.get("AUTH_JSON_OUTPUT", "/work/auth.json")

os.makedirs(SS_DIR, exist_ok=True)

def ss(page, name):
    path = f"{SS_DIR}/{name}.png"
    page.screenshot(path=path, full_page=False)
    print(f"  shot: {path}")

def mailcom_login(ctx):
    """在新 page 登录 mail.com，返回 page"""
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
    p.wait_for_timeout(5000)
    if "navigator" not in p.url:
        ss(p, "mailcom-login-fail")
        sys.exit(f"mail.com 登录失败 url={p.url}")
    print(f"  mail.com 登录成功 url={p.url}")
    ss(p, "mailcom-inbox")
    return p

def get_otp(mail_page, since_ts, max_wait=90):
    """轮询 mail.com 收件箱，取 OpenAI 发来的 6 位 OTP（since_ts 之后的邮件）"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        for fr in mail_page.frames:
            if fr.name != "mail":
                continue
            text = fr.evaluate("() => document.body.innerText")
            for ln in text.splitlines():
                if not re.search(r"openai|chatgpt|noreply", ln, re.I):
                    continue
                # 尝试点开
                try:
                    fr.get_by_text(ln, exact=False).first.click()
                    time.sleep(2)
                except Exception:
                    continue
                # 从所有 frame 收 6 位候选
                all_text = ""
                for f2 in mail_page.frames:
                    try:
                        all_text += f2.evaluate("() => document.body.innerText") + "\n"
                    except Exception:
                        pass
                for m in re.finditer(r"\b(\d{6})\b", all_text):
                    ctx_s = max(0, m.start() - 100)
                    ctx = all_text[ctx_s: m.start() + 100]
                    if re.search(r"code|verify|openai|login", ctx, re.I):
                        return m.group(1), ctx.strip()
        print("  OTP 未到，5s 后重试...")
        time.sleep(5)
        # 刷新收件箱
        for fr in mail_page.frames:
            if fr.name == "mail":
                try:
                    fr.evaluate("() => document.location.reload()")
                except Exception:
                    pass
        mail_page.wait_for_timeout(3000)
    return None, None

# ── 主流程 ──────────────────────────────────────────────────────────────────
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        java_script_enabled=True,
        ignore_https_errors=False,
    )
    # 抹掉 webdriver 标记
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
        Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
    """)

    # ── 1. 从 chatgpt.com 发起登录（带正确 OAuth redirect 参数）──────────────
    page = ctx.new_page()
    print(f"[1] 打开 chatgpt.com → Log in")
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    ss(page, "01-login-page")

    # 点 Log in 按钮
    login_btn = page.locator("button:has-text('Log in'), a:has-text('Log in')")
    if login_btn.count() > 0:
        login_btn.first.click()
        page.wait_for_timeout(3000)
    ss(page, "01b-after-click-login")

    # 填邮箱（email 步骤）
    email_input = page.locator("input[type='email'], input[autocomplete='username'], input[name='email']")
    if email_input.count() > 0:
        email_input.first.fill(EMAIL)
        cont = page.locator("button:has-text('Continue'), button:has-text('继续')")
        if cont.count() > 0:
            cont.first.click()
            page.wait_for_timeout(2000)
    ss(page, "02-after-email")

    # ── 2. 填密码 ─────────────────────────────────────────────────────────────
    print(f"[2] 填密码 (len={len(CHATGPT_PW)})")
    # 等待跳出 CF Verifying 阶段（auth.openai.com → chatgpt.com callback）
    # 最多等 50s，每 2s 轮询 URL 和 title
    import time as _time
    deadline2 = _time.time() + 50
    while _time.time() < deadline2:
        cur_url = page.url
        cur_title = page.title()
        print(f"  [{int(_time.time()-deadline2+50):.0f}s] url={cur_url}  title={cur_title[:40]}")
        if "password" in cur_url or page.query_selector("input[type='password']"):
            print("  ✅ 到达密码页")
            break
        if "api/auth/error" in cur_url and "Just a moment" not in cur_title:
            # CF 已完成，显示真实错误
            ss(page, "02b-auth-error")
            print(f"  ❌ 认证错误页: {page.content()[:300]}")
            sys.exit("OAuth 失败，见截图 02b-auth-error")
        _time.sleep(2)
    else:
        ss(page, "02b-timeout")
        sys.exit(f"❌ 50s 内未到达密码页，url={page.url}")
    ss(page, "02b-password-page")
    page.locator("input[type='password']").first.fill(CHATGPT_PW)
    page.locator("button:has-text('Continue'), button:has-text('继续')").first.click()
    page.wait_for_timeout(4000)
    ss(page, "03-after-password")

    # ── 3. 判断是否需要 OTP ───────────────────────────────────────────────────
    body = page.content()
    need_otp = any(kw in body for kw in ["收件箱", "verification", "验证码", "one-time", "OTP", "check your"])
    print(f"[3] 需要 OTP: {need_otp}")

    if need_otp:
        since_ts = int(time.time()) - 60  # 允许 60s 前的邮件

        print("[3a] 登录 mail.com 等 OTP...")
        mail_page = mailcom_login(ctx)

        otp, ctx_snippet = get_otp(mail_page, since_ts)
        mail_page.close()

        if not otp:
            ss(page, "03-otp-timeout")
            sys.exit("❌ 90s 内未收到 OTP")

        print(f"✅ OTP: {otp}  ctx={ctx_snippet[:80]}")

        # 填 OTP
        otp_input = page.locator(
            "input[autocomplete='one-time-code'], input[type='text'], input[name='code']"
        )
        otp_input.first.fill(otp)
        page.locator("button:has-text('Continue'), button:has-text('继续')").first.click()
        page.wait_for_timeout(4000)
        ss(page, "04-after-otp")

    # ── 4. 提取 session token ─────────────────────────────────────────────────
    print("[4] 提取 auth token...")
    page.goto("https://chatgpt.com/api/auth/session", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    ss(page, "05-session")

    try:
        raw = page.locator("pre, body").first.inner_text()
        data = json.loads(raw)
        access_token = data.get("accessToken", "")
    except Exception as e:
        access_token = ""
        print(f"  parse error: {e}")

    if not access_token:
        print(f"  body preview: {page.content()[:500]}")
        sys.exit("❌ 未能提取 accessToken，见截图 05-session.png")

    # 解码 JWT
    try:
        payload_raw = access_token.split(".")[1]
        payload_raw += "=" * (-len(payload_raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_raw))
        exp = payload.get("exp", int(time.time()) + 3600)
        oai_auth = payload.get("https://api.openai.com/auth", {})
        account_id = oai_auth.get("chatgpt_account_id", "")
        plan_type  = oai_auth.get("chatgpt_plan_type", "?")
    except Exception as e:
        print(f"  JWT decode error: {e}")
        exp, account_id, plan_type = int(time.time()) + 3600, "", "?"

    auth_json = {
        "access_token":  access_token,
        "refresh_token": "",       # 浏览器 session 无 refresh_token，litellm 仍可用到过期
        "id_token":      access_token,
        "expires_at":    exp,
        "account_id":    account_id,
    }

    with open(AUTH_OUT, "w") as f:
        json.dump(auth_json, f, indent=2)

    import datetime
    print(f"\n✅ auth.json → {AUTH_OUT}")
    print(f"   account_id : {account_id}")
    print(f"   plan_type  : {plan_type}")
    print(f"   expires_at : {exp}  ({datetime.datetime.fromtimestamp(exp)})")
    print(f"   token_len  : {len(access_token)}")

    browser.close()
