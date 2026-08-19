#!/usr/bin/env python3
"""
chatgpt-litellm-oauth.py — Full re-OAuth for a ChatGPT Pro account on 188

KEY DISCOVERIES (don't re-debug these):
  1. CF on auth.openai.com blocks browser SPA POSTs to /api/accounts/authorize/continue
     when traffic comes from playwright bundled chromium (TLS/HTTP2 fingerprint).
     → SOLVED: use `patchright` (playwright fork w/ TLS stealth) + image v1.59.0-noble
        (which ships chromium-1217 matching patchright's expectations).
  2. CF passes `Originator: codex_cli_rs` requests for /api/accounts/deviceauth/*
     and /oauth/token, so curl can drive Phase 1 + Phase 3 from 188 directly.
  3. Page renders need Xvfb (headed) — headless triggers CF Turnstile.
  4. From 188 (公司内网, NOT a cloud DC IP), auth.openai.com geolocates to JP
     and serves the normal login flow.

FLOW:
  Phase 1 (curl): POST /api/accounts/deviceauth/usercode → user_code
  Phase 2 (browser via patchright):
    - GET /codex/device → redirects to /log-in (Welcome back)
    - type email (delay=80) → Continue → /log-in/password
    - type password (delay=80) → Continue → /email-verification
    - if OTP needed: open mail.com in new page (字段A), wait for new ChatGPT
      email (top-row timestamp must change from baseline), grab 6-digit code
    - back on auth.openai.com: type OTP → Continue → wait URL leaves /email-verification
    - back to /codex/device → fill user_code → Authorize
  Phase 3 (curl):
    - POST /api/accounts/deviceauth/token (poll until 200) → authorization_code + code_verifier
    - POST /oauth/token (grant_type=authorization_code) → access_token / refresh_token / id_token
  Phase 4: decode JWT → write auth.json

ENV:
  MAIL_USER             EmilyOconnorgvg@mail.com
  MAIL_LOGIN_PW_FILE    /run/mail_pw.txt    (字段A, webmail password)
  CHATGPT_PW_FILE       /run/chatgpt_pw.txt (字段B, ChatGPT login password)
  AUTH_JSON_OUTPUT      /work/out/auth-acct-N.json
  SCREENSHOT_DIR        /work/screenshots
  HEADLESS              0 to run headed under Xvfb (default headless=1)

DOCKER RUN (on 188):
  docker run --rm \\
    -v /tmp/chatgpt-litellm-oauth.py:/work/script.py \\
    -v /tmp/mail_pw_acctN.txt:/run/mail_pw.txt \\
    -v /tmp/chatgpt_pw_acctN.txt:/run/chatgpt_pw.txt \\
    -v /tmp/screenshots-acctN:/work/screenshots \\
    -v /tmp:/work/out \\
    -e MAIL_USER=<email> \\
    -e MAIL_LOGIN_PW_FILE=/run/mail_pw.txt \\
    -e CHATGPT_PW_FILE=/run/chatgpt_pw.txt \\
    -e AUTH_JSON_OUTPUT=/work/out/auth-acctN.json \\
    -e SCREENSHOT_DIR=/work/screenshots \\
    -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \\
    -e DISPLAY=:99 \\
    mcr.microsoft.com/playwright/python:v1.59.0-noble \\
    bash -c "Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 & \\
             sleep 1 && \\
             pip install patchright -q --root-user-action=ignore && \\
             python3 /work/script.py"

KNOWN STILL-FLAKY (work in progress):
  - OTP throttling: OpenAI may rate-limit OTP emails after multiple recent
    requests; if /email-verification arrives but no new email lands within 60s,
    wait 10+ minutes before retrying.
  - mail.com webmail inbox parsing: get_otp() now waits for the top-ChatGPT-row
    timestamp to change vs baseline (proves it's THIS session's email),
    then clicks that row and extracts the 6-digit code.
"""

import os, re, sys, json, base64, time
import urllib.request
import urllib.parse
import urllib.error
# 2026-05-28: SPA POST /api/accounts/authorize/continue 需要 patchright 真 Chrome TLS;页面层 CF 需要 headed
from patchright.sync_api import sync_playwright

EMAIL      = os.environ["MAIL_USER"]
MAIL_PW    = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
CHATGPT_PW = open(os.environ["CHATGPT_PW_FILE"]).read().strip()
SS_DIR     = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
AUTH_OUT   = os.environ.get("AUTH_JSON_OUTPUT", "/work/auth.json")
CLIENT_ID  = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE  = "https://auth.openai.com"

os.makedirs(SS_DIR, exist_ok=True)

CODEX_HEADERS = {
    "Content-Type": "application/json",
    "Originator": "codex_cli_rs",
    "User-Agent": "codex_cli_rs/0.30.0 (Linux 5.15; x86_64) unknown",
}

# ── TOTP (authenticator app 2FA) ────────────────────────────────────────────
# 2026-07-25: acct-124..126 起飞书表带 2FA 密钥(base32 seed, 2fa.fun 同源)。
# 这些号密码提交后落 authenticator-app 挑战页,没有 "Try with email" 退路,
# 必须本地算 6 位 TOTP 填进去。无 pyotp 依赖(镜像里没有),用 hmac 自己算。
TOTP_SECRET = (os.environ.get("TOTP_SECRET") or "").strip().replace(" ", "").upper()
# 2026-08-15: GPT 密码搞不定(停在 /log-in/password 报 "Incorrect email address
# or password")的号, 强制走一次性验证码登录: 即使落到密码页也不填密码, 点
# 'Log in with a one-time code' 切邮箱 OTP。不设此 env 默认行为不变。
FORCE_OTP_LOGIN = os.environ.get("FORCE_OTP_LOGIN") == "1"


def _click_one_time_code_switch(page):
    """密码页底部点 'Log in with a one-time code' 切验证码登录。成功返回 True。"""
    for _sel in ("button:has-text('Log in with a one-time code')",
                 "a:has-text('Log in with a one-time code')",
                 "button:has-text('one-time code')",
                 "a:has-text('one-time code')",
                 "text=/log in with a one-time code|验证码登录|邮箱验证码/i"):
        try:
            _l = page.locator(_sel).first
            if _l.count() > 0 and _l.is_visible(timeout=1500):
                _l.click(timeout=5000)
                print(f"  clicked one-time-code switch {_sel!r}", flush=True)
                time.sleep(4)
                return True
        except Exception:
            pass
    return False

def totp_now(secret=None, t=None):
    """RFC6238 TOTP-SHA1, 30s window, 6 digits。secret = base32(无 padding 亦可)。"""
    import hmac, hashlib, struct
    s = (secret or TOTP_SECRET)
    if not s:
        return None
    s = s.replace(" ", "").upper()
    s += "=" * ((8 - len(s) % 8) % 8)          # base32 需 8 的倍数 padding
    key = base64.b32decode(s, casefold=True)
    ctr = int((t if t is not None else time.time()) // 30)
    mac = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"

PUSH_AUTH_BODY = (
    "approve on your", "we sent a notification", "open the chatgpt app",
    "上批准", "向你的设备发送通知", "打开 chatgpt 应用", "重新发送提示",
)
# 'Try with email' 会本地化(中文 '试试电子邮件'); 只匹配英文会漏点 → push-auth 页干等超时
TRY_EMAIL_RE = re.compile(
    r"try with email|use email|试试电子邮件|使用电子邮件|改用电子邮件|电子邮件", re.I)


def _is_push_auth(pg):
    """push-auth("手机批准")挑战页判定。URL 判定不够: 实测密码提交后 URL 可能仍停在
    /log-in/password, 只有正文变成 "在你的 <设备> 上批准"(acct-122 2026-07-25)。"""
    try:
        if "push-auth" in (pg.url or "").lower():
            return True
        body = (pg.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        return False
    return any(k in body for k in PUSH_AUTH_BODY)


def _click_try_with_email(pg, label="push-auth"):
    """点 push-auth 页的 'Try with email' 退回邮箱 OTP(多策略 + 多语言)。

    必须先等页面渲染完: 密码提交后 URL 已经是 push-auth, 但正文/按钮还是空白
    (acct-122 实证截图全白), 立刻去找按钮必然找不到, 6 轮全落空 → 误判"按钮不存在"。
    """
    try:
        pg.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    # 等按钮真正出现(最长 ~30s), 顺带打印页面上的候选按钮文案便于诊断
    for _w in range(20):
        try:
            texts = pg.evaluate(
                "() => [...document.querySelectorAll(\"button,a,[role='button']\")]"
                ".map(e => (e.innerText||'').trim()).filter(t => t && t.length < 40)")
        except Exception:
            texts = []
        if texts:
            if _w == 0 or any(TRY_EMAIL_RE.search(t) for t in texts):
                print(f"  [{label}] page buttons: {texts[:8]}", flush=True)
            if any(TRY_EMAIL_RE.search(t) for t in texts):
                break
        time.sleep(1.5)
    for _ in range(6):
        for how in ("role-button", "role-link", "text", "any"):
            try:
                if how == "role-button":
                    loc = pg.get_by_role("button", name=TRY_EMAIL_RE)
                elif how == "role-link":
                    loc = pg.get_by_role("link", name=TRY_EMAIL_RE)
                elif how == "text":
                    loc = pg.get_by_text(TRY_EMAIL_RE)
                else:
                    loc = pg.locator("button, a, [role='button']").filter(has_text=TRY_EMAIL_RE)
                if loc.count() > 0 and loc.first.is_visible():
                    try:
                        loc.first.click(timeout=4000)
                    except Exception:
                        loc.first.click(timeout=4000, force=True)
                    # 点击后页面是异步切换的: 立刻判定往往还停在 push-auth 正文,
                    # 会误判成"没点动"(acct-122 实证按钮明明找到了却报 not clickable)。
                    # 轮询等它离开 push-auth / 出现验证码输入框, 最长 ~24s。
                    for _c in range(16):
                        time.sleep(1.5)
                        if not _is_push_auth(pg):
                            print(f"  [{label}] fell back to email OTP via {how}"
                                  f" url={pg.url[:80]}", flush=True)
                            return True
                        try:
                            if pg.locator("input[autocomplete='one-time-code'], "
                                          "input[inputmode='numeric'], "
                                          "input[name='code']").count() > 0:
                                print(f"  [{label}] OTP input appeared via {how}", flush=True)
                                return True
                        except Exception:
                            pass
            except Exception:
                pass
        time.sleep(1.5)
    print(f"  [{label}] 'Try with email' not clickable", flush=True)
    return False


def _is_totp_page(pg):
    """当前页是否 authenticator-app 挑战(而非邮箱 OTP)。"""
    try:
        u = (pg.url or "").lower()
        if "authenticator" in u or "mfa" in u or "totp" in u:
            return True
        body = (pg.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        return False
    # 邮箱 OTP 页说 "sent to <email>"/"check your email";authenticator 页说 "authenticator app"
    if any(k in body for k in ("authenticator app", "authentication app", "验证器应用",
                               "身份验证器", "two-factor authentication code",
                               "6-digit code from your", "enter the code from your")):
        return True
    return False

def ss(page, name):
    path = f"{SS_DIR}/{name}.png"
    try:
        page.screenshot(path=path, full_page=False)
        print(f"  shot: {path}", flush=True)
    except Exception as e:
        print(f"  shot fail: {e}", flush=True)

def _enable_codex_toggle_inline(page):
    """在**当前已登录会话**内打开 Codex device-code toggle, 然后回到原 URL。

    为什么必须就地做: 撞 consent-disabled 时浏览器已是登录态。另起浏览器跑独立
    toggle 脚本要重新登录 = 多付一次 mail.com 取码; 该类号一次 OAuth 已需 1 次取码,
    串成 3 段后整体成功率被压到 ~20%(acct-112 实证 10 次全废)。就地开 = 零额外取码。

    返回 True 表示 toggle 已为 on(本来就开或本次点开)。
    """
    origin_url = page.url
    try:
        page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=GOTO_MS)
        time.sleep(4)
        # 此浏览器已在 auth.openai.com 完成认证, 但 chatgpt.com 域可能还没建立会话
        # → Security 页会渲染成登录页, 一个 switch 都找不到(acct-112 截图实证)。
        # 点 chatgpt.com 的 "Log in" 会走 SSO 静默回来, **不需要再取一次 OTP**。
        for _sso in range(2):
            try:
                body = (page.evaluate("() => document.body.innerText") or "")[:600]
            except Exception:
                body = ""
            if not re.search(r"log in|sign up|登录|注册", body, re.I):
                break
            print(f"  [toggle] chatgpt.com 未建立会话 → 走 SSO (try {_sso+1})", flush=True)
            # 直接 goto /auth/login: 会 302 到 auth.openai.com/authorize, 那边已有会话 →
            # 静默重定向回 chatgpt.com 并落 cookie, 无需再取 OTP。比点按钮稳(按钮文案/
            # data-testid 常变, acct-112 实证 5 个选择器全没匹配上, 白等一轮)。
            try:
                page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=GOTO_MS)
                time.sleep(10)
            except Exception as e:
                print(f"  [toggle] SSO goto err: {e}", flush=True)
            # 若停在 auth.openai.com 的 Continue/继续 上, 推一把。
            # 注意: 不要用泛化的 has-text('Continue') —— chatgpt.com 匿名页上那是
            # "Continue with Google", 一点就跳 accounts.google.com(acct-112 实证)。
            # 优先用 dump 出的真实 testid, 且显式排除第三方登录按钮。
            try:
                for sel in ("[data-testid='login-button']",
                            "button:has-text('Log in')", "button:has-text('登录')",
                            "button[type='submit']"):
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible(timeout=1200):
                        _t = (loc.inner_text() or "").strip()
                        if re.search(r"google|apple|microsoft|phone", _t, re.I):
                            continue
                        loc.click(timeout=4000)
                        print(f"  [toggle] SSO pushed via {sel!r} text={_t[:20]!r}", flush=True)
                        time.sleep(6)
                        break
            except Exception:
                pass
            ss(page, f"07e0-after-sso-{_sso}")
            print(f"  [toggle] after SSO url={page.url[:90]}", flush=True)
            page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=GOTO_MS)
            time.sleep(5)
        # Security 是 React SPA, 走美国代理渲染慢, 等 switch 真出现(实证 acct-93 sleep 4 太短)
        sw = page.locator("button[role='switch']")
        for _w in range(20):  # ~40s
            time.sleep(2)
            try:
                page.mouse.wheel(0, 600)  # 触发懒加载
            except Exception:
                pass
            sw = page.locator("button[role='switch']")
            if sw.count() > 0:
                print(f"  [toggle] security page: {sw.count()} switches after {(_w+1)*2}s", flush=True)
                break
        ss(page, "07e1-security-for-toggle")

        want = re.compile(r"codex|device\s*code|device-code|device authorization|设备代码|设备授权|设备码", re.I)
        deny = re.compile(r"mfa|authenticator|passkey|session|2fa|password|text message|短信|密码|通行密钥|会话", re.I)
        for i in range(min(sw.count(), 12)):
            try:
                lbl = sw.nth(i).evaluate(
                    "el => (el.getAttribute('aria-label')||'') + ' ' + "
                    "(el.closest('div')?.parentElement?.innerText || el.parentElement?.innerText || '')"
                ) or ""
                if not want.search(lbl) or deny.search(lbl):
                    continue
                if (sw.nth(i).get_attribute("aria-checked") or "") == "true":
                    print(f"  [toggle] idx={i} already on", flush=True)
                    return True
                # radix switch 对普通 click 不总响应; force → mouse-box-center 兜底
                for how, fn in (
                    ("force", lambda s=sw.nth(i): s.click(force=True, timeout=4000)),
                    ("mouse-box", lambda s=sw.nth(i): (
                        s.bounding_box() and page.mouse.click(
                            s.bounding_box()["x"] + s.bounding_box()["width"] / 2,
                            s.bounding_box()["y"] + s.bounding_box()["height"] / 2))),
                ):
                    try:
                        fn()
                    except Exception as e:
                        print(f"  [toggle] {how} raised: {e}", flush=True)
                    time.sleep(2)
                    if (sw.nth(i).get_attribute("aria-checked") or "") == "true":
                        print(f"  [toggle] idx={i} enabled via {how}", flush=True)
                        ss(page, f"07e2-toggle-{i}")
                        return True
            except Exception:
                continue
        print("  [toggle] no Codex/device-code switch matched", flush=True)
        ss(page, "07e2-toggle-not-found")
        return False
    except Exception as e:
        print(f"  [toggle] err: {e}", flush=True)
        return False
    finally:
        try:
            page.goto(origin_url, wait_until="domcontentloaded", timeout=GOTO_MS)
            time.sleep(3)
            ss(page, "07e3-back-to-consent")
        except Exception:
            pass


def _enter_email_resilient(p_page, email, label="email"):
    """Enter an email into a login/device email field without the brittle
    hard `.first.click()` that hangs 30s when the field is prefilled and the
    page is auto-submitting (element 'not enabled' / 'detached from DOM').

    Order: detect prefill (skip) → fill() → click+type → JS set+dispatch.
    Never raises: if all fail the page is usually already auto-advancing, so
    we log and let the caller proceed to the submit/next-page wait.
    Returns True if the field ended up holding the email (or was prefilled)."""
    sel = "input[type='email'], input[autocomplete='username'], input[name='email']"
    try:
        p_page.wait_for_selector(sel, timeout=20000)
    except Exception as e:
        print(f"    [{label}] no email field appeared: {str(e)[:60]}", flush=True)
        return False
    fld = p_page.locator(sel).first
    try:
        cur = (fld.input_value(timeout=3000) or "").strip()
    except Exception:
        cur = ""
    if cur.lower() == email.lower():
        print(f"    [{label}] email already prefilled ({cur}) — skip typing", flush=True)
        return True
    # click-type (真键入) 优先: fill() 不触发 React onChange → Continue 按钮不激活
    # → email 提交无效 → 密码页永不出现 (实证 acct-93 密码号卡这, "email entered
    # via fill" 后停在 /log-in 密码框不出)。真键入触发 onChange 才能前进。
    for how in ("click-type", "fill", "js"):
        try:
            if how == "fill":
                fld.fill(email, timeout=5000)
            elif how == "click-type":
                fld.click(timeout=5000)
                p_page.keyboard.press("Control+a")
                p_page.keyboard.type(email, delay=80)
            else:
                fld.evaluate(
                    "(el,v)=>{el.value=v;"
                    "el.dispatchEvent(new Event('input',{bubbles:true}));"
                    "el.dispatchEvent(new Event('change',{bubbles:true}));}", email)
            # 校验真落值 (fill 偶尔静默失败)
            try:
                if (fld.input_value(timeout=2000) or "").strip().lower() != email.lower() and how != "js":
                    continue
            except Exception:
                pass
            print(f"    [{label}] email entered via {how}", flush=True)
            return True
        except Exception as e:
            print(f"    [{label}] {how} failed: {str(e)[:60]}", flush=True)
    print(f"    [{label}] all strategies failed; proceeding (page may auto-advance)", flush=True)
    return False

def _enter_otp_resilient(p_page, code, label="otp"):
    """Type an OTP into either single-char boxes (maxlength=1) or one code
    input, without a hard .click() that hangs 30s on a hidden/disabled field.
    Order per single-input: fill() → click+type → JS set+dispatch. Best-effort."""
    code = (code or "").strip()
    if not code:
        return False
    boxes = [b for b in p_page.locator("input[maxlength='1']").all() if b.is_visible()]
    if len(boxes) >= len(code):
        for i, ch in enumerate(code):
            try:
                boxes[i].click(timeout=2500); p_page.keyboard.type(ch, delay=90)
            except Exception:
                try: boxes[i].fill(ch)
                except Exception: pass
        try: p_page.keyboard.press("Enter")
        except Exception: pass
        print(f"    [{label}] entered into {len(code)} boxes", flush=True)
        return True
    sel = ("input[autocomplete='one-time-code'], input[inputmode='numeric'], "
           "input[name='code'], input[type='tel'], input[maxlength='6']")
    cand = [c for c in p_page.locator(sel).all() if c.is_visible()]
    if not cand:
        cand = [c for c in p_page.locator("form input").all() if c.is_visible()]
    if not cand:
        print(f"    [{label}] no OTP input located", flush=True)
        return False
    fld = cand[0]
    # click-type (真键入) 优先: fill() 不触发 React onChange → device-grant OTP 虽落值
    # 但服务端从未收到提交 → deviceauth/token poll 永远 403 pending (实证 acct-96:
    # "entered via fill" 后 34x 403 pending, 而 device grant 从未 authorize)。
    # 真键入触发 onChange 才能提交。每种策略后校验 input_value 真落值。
    for how in ("click-type", "fill", "js"):
        try:
            if how == "fill":
                fld.fill(code, timeout=5000)
            elif how == "click-type":
                fld.click(timeout=5000); p_page.keyboard.press("Control+a")
                p_page.keyboard.press("Backspace"); p_page.keyboard.type(code, delay=120)
            else:
                fld.evaluate(
                    "(el,v)=>{el.value=v;"
                    "el.dispatchEvent(new Event('input',{bubbles:true}));"
                    "el.dispatchEvent(new Event('change',{bubbles:true}));}", code)
            # 校验真落值 (fill 静默失败时回退到下一策略)
            try:
                if (fld.input_value(timeout=2000) or "").strip() != code and how != "js":
                    print(f"    [{label}] {how} value mismatch, next strategy", flush=True)
                    continue
            except Exception:
                pass
            try:
                fld.press("Enter")
            except Exception:
                p_page.keyboard.press("Enter")
            print(f"    [{label}] entered via {how}", flush=True)
            return True
        except Exception as e:
            print(f"    [{label}] {how} failed: {str(e)[:50]}", flush=True)
    return False

def http_post(url, body, extra_headers=None, timeout=20):
    headers = dict(CODEX_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    # When OAUTH_PROXY is set, route these device-auth API calls through the 236 US
    # SOCKS egress too (not just the browser) — 188-direct urllib gets CF 429 under
    # load, and the US egress passes. urllib has no SOCKS support, so shell to curl.
    _proxy = os.environ.get("OAUTH_PROXY", "").strip()
    if _proxy:
        px = _proxy.replace("socks5://", "socks5h://")
        cmd = ["curl", "-s", "--max-time", str(timeout), "-x", px,
               "-w", "\n%{http_code}", "-X", "POST", "-d", data.decode(errors="replace")]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        try:
            import subprocess as _sp
            out = _sp.run(cmd, capture_output=True, text=True, timeout=timeout + 10).stdout
            b, _, code = out.rpartition("\n")
            return int(code or 0), b
        except Exception as e:
            return 0, f"curl proxy error: {e}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")

# ── SMS OTP helpers (2026-06-02: handle "Phone number required" challenge) ──
# OpenAI risk-control may demand SMS phone verification before re-issuing a
# token. PHONE_NUMBER = national digits (country select defaults US +1);
# SMS_API_URL = a virtual-number inbox endpoint returning plain text where a
# real message line carries a 6-digit code and the idle state is "暂无短信|...".
PHONE_NUMBER = os.environ.get("PHONE_NUMBER", "").strip()
SMS_API_URL  = os.environ.get("SMS_API_URL", "").strip()

def _sms_fetch():
    if not SMS_API_URL:
        return ""
    try:
        req = urllib.request.Request(SMS_API_URL, headers={"User-Agent": "Mozilla/5.0"})
        return urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  sms fetch err: {e}", flush=True)
        return ""

def _sms_codes(txt):
    """Standalone 6-digit codes from SMS text, skipping the idle boilerplate line.
    Expiry dates like 2026-06-09 23:59:59 have no 6-consecutive-digit run."""
    codes = []
    for ln in txt.splitlines():
        if "暂无短信" in ln:
            continue
        codes.extend(re.findall(r"(?<!\d)(\d{6})(?!\d)", ln))
    return codes

def poll_sms_otp(baseline, timeout=150):
    """Wait for a 6-digit code NOT in baseline (the number may be reused across
    accounts, so old codes can linger)."""
    seen = set(baseline)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in _sms_codes(_sms_fetch()):
            if c not in seen:
                return c
        time.sleep(5)
    return ""

# ── Step 1: get user_code via codex_cli_rs endpoint ─────────────────────────
# EXTERNAL_USER_CODE: 由**官方 codex CLI**(codex login --device-auth)生成的 code。
# 此时本脚本只负责"浏览器登录 + 在 /codex/device 填码授权", token 交换由官方 CLI
# 自己轮询完成 → 不走网页 consent 页, 因此不受 Codex device toggle 未开的阻挡
# (acct-112 实证: 网页 consent 必撞 "Enable device code authorization")。
# GRANT_ONLY=1 时授权完即退出, 不自己换 token。
EXTERNAL_USER_CODE = (os.environ.get("EXTERNAL_USER_CODE") or "").strip()
GRANT_ONLY = os.environ.get("GRANT_ONLY") == "1"
if EXTERNAL_USER_CODE:
    USER_CODE = EXTERNAL_USER_CODE
    DEVICE_AUTH_ID = ""
    INTERVAL = 5
    print(f"[1] 使用外部 user_code={USER_CODE} (官方 codex CLI 持有轮询) "
          f"grant_only={GRANT_ONLY}", flush=True)
else:
    print("[1] Request device code via /api/accounts/deviceauth/usercode...", flush=True)
    status, body = http_post(
        f"{AUTH_BASE}/api/accounts/deviceauth/usercode",
        {"client_id": CLIENT_ID},
    )
    print(f"  status={status} body={body[:200]}", flush=True)
    if status != 200:
        sys.exit(f"❌ Failed to get user_code: {body[:300]}")
    device_data = json.loads(body)
    DEVICE_AUTH_ID = device_data["device_auth_id"]
    USER_CODE = device_data["user_code"]
    INTERVAL = int(device_data.get("interval", "5"))
    print(f"  ✅ user_code={USER_CODE}  device_auth_id={DEVICE_AUTH_ID[:30]}...", flush=True)

# ── Step 2: browser - navigate to verify page, fill user_code ────────────────
# OTP provider switch:
#   MAIL_OTP_PROVIDER=mailcom (default)  → browser-driven www.mail.com webmail
#   MAIL_OTP_PROVIDER=imap_qq            → imaplib + imap.qq.com:993 (字段A = QQ 16-char auth code)
#   MAIL_OTP_PROVIDER=imap               → generic IMAP (set IMAP_HOST/IMAP_PORT)
MAIL_OTP_PROVIDER = os.environ.get("MAIL_OTP_PROVIDER", "mailcom").lower()

def imap_host_port():
    if MAIL_OTP_PROVIDER == "imap_qq":
        return ("imap.qq.com", 993)
    return (os.environ.get("IMAP_HOST", "imap.qq.com"), int(os.environ.get("IMAP_PORT", "993")))

def imap_fetch_otp(since_ts, max_wait=180):
    """Poll IMAP for the latest OpenAI/ChatGPT login OTP. Returns (otp, ctx) or (None, None).
    `since_ts` filters mails newer than this Unix timestamp."""
    import imaplib, email as _email
    from email.header import decode_header as _dh
    host, port = imap_host_port()
    deadline = time.time() + max_wait
    last_seen_uid = None
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(host, port, timeout=20)
            M.login(EMAIL, MAIL_PW)
            M.select("INBOX")
            typ, data = M.search(None, "FROM", "tm.openai.com", "SUBJECT", "temporary")
            ids = data[0].split()
            # newest first
            for mid in reversed(ids[-5:]):
                typ, msg_data = M.fetch(mid, "(RFC822)")
                msg = _email.message_from_bytes(msg_data[0][1])
                # parse date
                try:
                    mail_ts = _email.utils.mktime_tz(_email.utils.parsedate_tz(msg["Date"]))
                except Exception:
                    mail_ts = 0
                if mail_ts < since_ts - 30:
                    continue  # too old
                body = ""
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        b = part.get_payload(decode=True)
                        if b:
                            body = b.decode(part.get_content_charset() or "utf-8", errors="replace")
                            break
                m = re.search(r"\b(\d{6})\b", body)
                if m:
                    code = m.group(1)
                    print(f"  IMAP: got OTP {code} from mail dated {msg['Date']}", flush=True)
                    try: M.logout()
                    except: pass
                    return code, body[:200]
            try: M.logout()
            except: pass
        except Exception as e:
            print(f"  IMAP fetch err: {e}", flush=True)
        print(f"  IMAP: OTP not yet (since_ts={since_ts}), retry in 10s...", flush=True)
        time.sleep(10)
    return None, None

def mailcom_login(ctx):
    if MAIL_OTP_PROVIDER == "outlook":
        return outlook_login(ctx)
    if MAIL_OTP_PROVIDER != "mailcom":
        print(f"  [skip] mailcom_login — using OTP provider={MAIL_OTP_PROVIDER}", flush=True)
        return None  # sentinel; get_otp will route by provider
    p = ctx.new_page()
    p.goto("https://www.mail.com/", wait_until="domcontentloaded")
    # 2026-08-17 acct-210 实证 (踩坑 #46): 首页 1.5s 不够, "Log in" 链接晚出现 →
    # 后续 email/password/submit selector 全 miss (submit button not found)。
    # 可用 MAILCOM_HOME_SETTLE_SEC 覆盖 (默认 120s)。
    p.wait_for_timeout(int(os.environ.get("MAILCOM_HOME_SETTLE_SEC", "120")) * 1000)
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
    # wait login redirect to navigator.mail.com
    for _ in range(30):
        if "navigator" in p.url:
            break
        time.sleep(1)
    if "navigator" not in p.url:
        ss(p, "mailcom-fail")
        sys.exit(f"mail.com login failed url={p.url}")
    # dismiss interstitials: "Continue to Account" / upgrade prompts
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
                print(f"  mail.com: clicking '{sel}'", flush=True)
                loc.first.click()
                p.wait_for_timeout(2000)
        except Exception:
            pass
    # wait for the actual mail iframe (name='mail') to appear and have content
    # 2026-06-18: mail.com 把邮件列表迁到 Shadow DOM, innerText 返回空,
    # 改用 [class*='mail-item'] 选择器探测 row 是否到位
    for attempt in range(20):
        mail_frame = next((fr for fr in p.frames if fr.name == "mail"), None)
        if mail_frame:
            try:
                n = mail_frame.locator("[class*='mail-item']").count()
                if n > 0:
                    print(f"  mail.com: inbox loaded (mail-item rows={n})", flush=True)
                    break
            except Exception:
                pass
        print(f"  mail.com: waiting for inbox iframe... [{attempt+1}/20]", flush=True)
        time.sleep(2)
    ss(p, "mailcom-inbox")
    return p

# ── outlook.live.com provider (2026-06-22: hotmail acct) ────────────────────
# 走 login.live.com → outlook.live.com inbox; 邮件 subject + body 都明文(没 Shadow DOM)
# 用 div[role='option']/div[role='listitem'] 拿 row text, regex 6位 OTP
def _outlook_dismiss_interstitials(p, rounds=4):
    """Skip Microsoft post-login interstitials (Let's protect / Add security info /
    Stay signed in / Verify your email recovery-email prompts).

    We never want to fill the recovery-email input — it locks the flow. Click any
    "Skip for now" / "Not now" / "Cancel" / "Back" control and loop until inbox
    URL or no control found.
    """
    skip_selectors = [
        "a:has-text('Skip for now')",
        "button:has-text('Skip for now')",
        "a:has-text('Not now')",
        "button:has-text('Not now')",
        "input[value='Skip for now']",
        "input[value='Cancel']",
        "input#iCancel",
        "input#idBtn_Back",
        "button:has-text('Cancel')",
        "button:has-text('Back')",
        "[role='button']:has-text('Skip')",
        "text=Skip for now",
    ]
    for i in range(rounds):
        if "outlook.live.com" in p.url and "/mail/" in p.url:
            return True
        clicked = False
        for sel in skip_selectors:
            try:
                loc = p.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    print(f"  outlook: dismiss interstitial via {sel!r}", flush=True)
                    loc.click(timeout=5000)
                    clicked = True
                    time.sleep(4)
                    ss(p, f"outlook-skip-{i}")
                    break
            except Exception as e:
                print(f"  outlook: skip sel {sel!r} err {e}", flush=True)
        if not clicked:
            return False
    return False


def outlook_login(ctx):
    if MAIL_OTP_PROVIDER != "outlook":
        return None
    p = ctx.new_page()

    # (a) Try direct inbox — if a previous login left session cookies, skip re-auth.
    try:
        p.goto("https://outlook.live.com/mail/0/", timeout=30000)
        time.sleep(4)
        ss(p, "outlook-inbox-try")
        _outlook_dismiss_interstitials(p, rounds=3)
        if "outlook.live.com" in p.url and "/mail/" in p.url:
            print(f"  outlook: session live, direct inbox url={p.url[:80]}", flush=True)
            ss(p, "outlook-inbox")
            return p
    except Exception as e:
        print(f"  outlook: direct inbox try failed {e}", flush=True)

    entry = ("https://login.live.com/login.srf?wa=wsignin1.0&rpsnv=13&ct=" + str(int(time.time()))
             + "&rver=7.0.6738.0&wp=MBI_SSL&wreply=https%3a%2f%2foutlook.live.com%2fowa%2f%3frealm%3dhotmail.com&id=292841&aadredir=1&CBCXT=out&lw=1&fl=dob,easi2&cobrandid=90015")
    p.goto(entry, timeout=30000)
    time.sleep(3)
    ss(p, "outlook-landing")

    # If already partway through (email pre-filled + "protect account" prompt), skip it.
    if _outlook_dismiss_interstitials(p, rounds=2):
        print(f"  outlook: post-goto interstitial dismissed url={p.url[:80]}", flush=True)

    # email — only if the email field is actually present (otherwise we're on a
    # post-login interstitial and typing would corrupt recovery-email input).
    email_field = p.locator("input[type='email'], input[name='loginfmt'], input#i0116").first
    if email_field.count() > 0 and email_field.is_visible():
        email_field.click(); email_field.fill(""); email_field.type(EMAIL, delay=60)
        nb = p.get_by_role("button", name="Next", exact=True)
        if nb.count() == 0:
            nb = p.locator("input[type='submit'], input#idSIButton9, button[type='submit']")
        nb.first.click(); time.sleep(4)
    else:
        print("  outlook: email field absent — likely on interstitial or logged in", flush=True)

    _outlook_dismiss_interstitials(p, rounds=2)

    # password (may be absent if session was live or interstitial redirected to inbox)
    pwi = p.locator("input[type='password'], input#i0118, input[name='passwd']").first
    if pwi.count() == 0 or not pwi.is_visible():
        print(f"  outlook: password field absent, url={p.url[:80]}", flush=True)
        _outlook_dismiss_interstitials(p, rounds=4)
        for _ in range(20):
            if "outlook.live.com" in p.url and "/mail/" in p.url:
                break
            time.sleep(2)
        ss(p, "outlook-inbox")
        return p
    pwi.wait_for(timeout=20000); pwi.click(); pwi.type(MAIL_PW, delay=60)
    sb = p.get_by_role("button", name="Next", exact=True)
    if sb.count() == 0:
        sb = p.get_by_role("button", name="Sign in", exact=True)
    if sb.count() == 0:
        sb = p.locator("input[type='submit'], input#idSIButton9, button[type='submit']")
    sb.first.click(); time.sleep(6)
    # KMSI "Stay signed in?" — click No
    for sf in [
        lambda: p.get_by_role("button", name="No", exact=True),
        lambda: p.locator("input#idBtn_Back"),
        lambda: p.locator("button:has-text('No')"),
    ]:
        try:
            loc = sf()
            if loc.count() > 0 and loc.first.is_visible():
                print("  outlook: KMSI click No", flush=True)
                loc.first.click(); time.sleep(4)
                break
        except Exception:
            pass
    # 等 inbox 渲染
    for _ in range(20):
        if "outlook.live.com" in p.url and "/mail/" in p.url:
            break
        time.sleep(2)
    ss(p, "outlook-inbox")
    print(f"  outlook: inbox url={p.url[:80]}", flush=True)
    return p

def outlook_get_otp(mail_page, since_ts, max_wait=180):
    """outlook inbox 找最新 OpenAI OTP 邮件. subject 含 6位数字 + 必须含相对时间(今日)."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            mail_page.reload(wait_until="domcontentloaded"); time.sleep(5)
        except Exception:
            pass
        ss(mail_page, "outlook-poll")
        items = mail_page.locator("div[role='option'], div[role='listitem']").all()
        for it in items[:10]:  # 倒序最新在前
            try:
                txt = it.inner_text(timeout=2000)
            except Exception:
                continue
            tl = txt.lower()
            if ("openai" not in tl) and ("chatgpt" not in tl):
                continue
            if ("login code" not in tl) and ("verification code" not in tl) and ("temporary" not in tl):
                continue
            # 今日邮件: "1:36" 时钟 或 "now"/"min ago"
            has_recent = any(m in tl for m in ["now", "min ago", "minute", "sec", "几秒", "几分", "刚刚"])
            has_clock = bool(re.search(r"\b\d{1,2}:\d{2}\b", tl))
            if not (has_recent or has_clock):
                continue
            m = re.search(r"\b(\d{6})\b", txt)
            if m:
                code = m.group(1)
                print(f"  outlook: OTP candidate {code} from row {txt[:80]!r}", flush=True)
                # 点开邮件正文确认
                try:
                    it.click(); time.sleep(3)
                    ss(mail_page, "outlook-open")
                    body_loc = mail_page.locator("div[role='document'], div[role='region']").first
                    body = body_loc.inner_text(timeout=3000) if body_loc.count() > 0 else mail_page.content()
                    m2 = re.search(r"\b(\d{6})\b", body)
                    if m2:
                        return m2.group(1), body[:200]
                    return code, txt[:200]
                except Exception as e:
                    print(f"  outlook: open err {e}", flush=True)
                    return code, txt[:200]
        print(f"  outlook: no fresh OTP, retry 8s (elapsed {int(time.time() - (deadline - max_wait))}s)", flush=True)
        time.sleep(8)
    return None, None

def settle_inbox(mail_page, label="otp"):
    """收件箱 settle 规则(用户 2026-07-20 定的铁律): 等 1min → **刷新** → 再等 1min → 才读码。

    为什么必须刷新: 本次登录触发的验证码邮件是在我们打开收件箱**之后**才到的,
    不刷新就只能看到打开那一刻的旧列表 → 读到上一次残留的旧码(被判"代码不正确")
    或者压根看不到新邮件 → 空转重试。
    旧实现写成 time.sleep(60*2) 一口气睡完, 中间那次刷新从没执行过(2026-07-25 发现)。
    """
    secs = 0 if os.environ.get("OTP_FAST") else int(os.environ.get("OTP_SETTLE_SEC", "60"))
    if not secs:
        return
    print(f"  [{label}] settle {secs}s (让本次验证码先到)...", flush=True)
    time.sleep(secs)
    # 刷新收件箱: 优先刷 mail frame, 拿不到就整页 reload
    refreshed = False
    try:
        mf = next((fr for fr in mail_page.frames if fr.name == "mail"), None)
        if mf:
            mf.evaluate("() => document.location.reload()")
            refreshed = True
    except Exception:
        pass
    if not refreshed:
        try:
            mail_page.reload(wait_until="domcontentloaded", timeout=60000)
            refreshed = True
        except Exception as e:
            print(f"  [{label}] inbox refresh err: {str(e)[:60]}", flush=True)
    try:
        mail_page.wait_for_timeout(3000)
    except Exception:
        pass
    print(f"  [{label}] refreshed={refreshed}; settle another {secs}s...", flush=True)
    time.sleep(secs)


def get_otp(mail_page, since_ts, max_wait=180):
    """Find topmost OpenAI/ChatGPT email, click it, extract 6-digit OTP from body.

    2026-06-18: mail.com 把邮件列表 + body 都迁到 Shadow DOM,evaluate(innerText)
    返回空。改用:
      - 列表行: mf.locator("[class*='mail-item']") 枚举,text_content() 穿透 Shadow
      - 邮件正文: 实测在 frame name='detail-body-iframe' 里;直接 outerHTML 拿
        到 OTP (不能跨所有 frame 扫,否则 ad 的 siteId/mid 全是 6 位 false positive)
    `since_ts` kept for signature compat, not currently used.
    """
    if MAIL_OTP_PROVIDER == "outlook":
        return outlook_get_otp(mail_page, since_ts, max_wait=max_wait)
    if MAIL_OTP_PROVIDER != "mailcom":
        return imap_fetch_otp(since_ts, max_wait=max_wait)

    def find_body_frame():
        # mail.com 邮件正文 iframe name='detail-body-iframe'
        # (R&D mailcom-otp-extract-rnd.py 实证)
        return next(
            (
                f
                for f in mail_page.frames
                if "detail-body" in (f.name or "") or "detail-body" in (f.url or "")
            ),
            None,
        )

    def extract_otp_from_body():
        bf = find_body_frame()
        if not bf:
            return None, None
        try:
            html = bf.evaluate("() => document.documentElement.outerHTML") or ""
        except Exception:
            return None, None
        # 邮件正文里 OTP 是唯一 6 位数字。优先找 "code is" / "verification" 附近的;
        # 兜底取 outerHTML 里第一个独立 6 位 token。
        for m in re.finditer(r"\b(\d{6})\b", html):
            ctx_s = max(0, m.start() - 200)
            ctx_e = min(len(html), m.end() + 200)
            ctx = html[ctx_s:ctx_e]
            if re.search(r"code|verify|verification|login|openai|chatgpt", ctx, re.I):
                return m.group(1), ctx
        m = re.search(r"\b(\d{6})\b", html)
        if m:
            return m.group(1), html[max(0, m.start()-100):m.end()+100]
        return None, None

    deadline = time.time() + max_wait
    while time.time() < deadline:
        mf = next((fr for fr in mail_page.frames if fr.name == "mail"), None)
        if not mf:
            print("  mail frame missing, retry 5s", flush=True)
            time.sleep(5)
            continue
        try:
            rows = mf.locator("[class*='mail-item']")
            cnt = rows.count()
        except Exception as e:
            print(f"  rows count err: {e}", flush=True)
            cnt = 0
        # 登录会同时来两封 GPT 邮件: "验证码" + "新登录提醒(New sign-in)"。
        # 提醒那封没有码/排最上会误选。先按主题挑真正的验证码邮件, 排除 sign-in 提醒。
        CODE_SUBJ = re.compile(r"code|verification|登录代码|临时|temporary|验证码", re.I)
        # 只排除**确定没有验证码**的 sign-in 提醒邮件。
        # ⚠️ 不要往这里加"套餐/续订/账单"之类: 那些主题的邮件里**也是验证码邮件**
        # (2026-07-25 用户纠正)。之前误加进排除表, 等于把真验证码邮件筛掉了。
        # 读不到码的真因是"打开收件箱后没刷新"→ 看的是旧列表, 已由 settle_inbox 修掉。
        ALERT_SUBJ = re.compile(
            r"new sign-?in|new login|新登录|新的登录|sign-?in to your|security", re.I)
        def _row_texts():
            out = []
            for i in range(min(cnt, 15)):
                try:
                    t = (rows.nth(i).text_content(timeout=1500) or "").strip()
                except Exception:
                    t = ""
                out.append(t)
            return out
        _texts = _row_texts()
        # 优先级: 是 OpenAI 发件人 + 主题含 code + 不是 sign-in 提醒 → 其次任意 OpenAI 行
        order = [i for i, t in enumerate(_texts)
                 if re.search(r"openai|chatgpt|noreply", t, re.I) and CODE_SUBJ.search(t) and not ALERT_SUBJ.search(t)]
        order += [i for i, t in enumerate(_texts)
                  if re.search(r"openai|chatgpt|noreply", t, re.I) and i not in order and not ALERT_SUBJ.search(t)]
        clicked = False
        for i in order:
            t = _texts[i]
            print(f"  candidate row[{i}] (code-email): {t[:120]!r}", flush=True)
            row_el = rows.nth(i)
            opened = False
            try:
                row_el.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            for action_name, do_action in [
                ("dblclick", lambda: row_el.dblclick(timeout=4000)),
                ("subj-link-click", lambda: row_el.locator(":scope a, :scope [role='link'], :scope span").first.click(timeout=3000)),
                ("evaluate-dispatch", lambda: row_el.evaluate(
                    "el => { el.dispatchEvent(new MouseEvent('dblclick', {bubbles:true, cancelable:true, view:window})); }")),
            ]:
                try:
                    do_action()
                    time.sleep(3.5)
                    bf = find_body_frame()
                    if bf:
                        print(f"  ✓ {action_name} opened body frame", flush=True)
                        opened = True
                        break
                    print(f"  {action_name} no body frame yet", flush=True)
                except Exception as e:
                    print(f"  {action_name} err: {e}", flush=True)
            if not opened:
                continue
            clicked = True
            # 等 detail-body-iframe 出现 (mail.com 异步加载邮件正文)
            bf = None
            for _ in range(10):
                bf = find_body_frame()
                if bf:
                    break
                time.sleep(1)
            print(f"  body frame: {bf.name if bf else 'NONE'} (frames_total={len(mail_page.frames)})", flush=True)
            otp, ctx = extract_otp_from_body()
            if otp:
                return otp, ctx.strip() if ctx else ""
            # 这封点开了但正文没码(账单/提醒类) → 试**下一个**候选行, 别 break。
            # 旧版在此 break 出候选循环, 外层重试又从头挑到同一封 → 死循环
            # (acct-122 实证: 十几轮全卡在 "你的套餐将不会续订" 那封)。
            print(f"  row[{i}] opened but no OTP in body — try next candidate", flush=True)
        print(f"  OTP not yet (rows={cnt}), retry in 5s...", flush=True)
        time.sleep(5)
        try:
            mf.evaluate("() => document.location.reload()")
        except Exception:
            pass
        mail_page.wait_for_timeout(3000)
    return None, None

HEADLESS = os.environ.get("HEADLESS", "0") != "0"  # default headed (CF 2026: headless 被拒,headed via Xvfb 放行)
# Through the 236 US SOCKS proxy the double-hop slows full page loads; widen goto budget.
GOTO_MS = 120000 if os.environ.get("OAUTH_PROXY") else 45000

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=HEADLESS,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
        **({"proxy": {"server": os.environ["OAUTH_PROXY"]}} if os.environ.get("OAUTH_PROXY") else {}),
    )
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = ctx.new_page()

    # Mail OTP fetch must NOT traverse the CF proxy: mail.com is neither
    # geo- nor CF-gated, and the double-hop SOCKS latency hangs the webmail
    # inbox load. When OAUTH_PROXY is set, run the mail webmail in a separate
    # DIRECT (un-proxied) browser context; login/consent stay on the proxy.
    if os.environ.get("OAUTH_PROXY"):
        mail_browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        mail_ctx = mail_browser.new_context(
            viewport={"width": 1280, "height": 800}, locale="en-US"
        )
    else:
        mail_browser = None
        mail_ctx = ctx

    # ── PHASE 1.5: chatgpt.com login ────────────────────────────────────────
    # 2026-06-18: ChatGPT Settings → Security no longer exposes a reliable
    # Codex/device-code toggle. Do not click generic Security switches here:
    # they are MFA/session controls. The real device binding is the
    # auth.openai.com/codex/device flow below.
    # SKIP_PHASE15=1(GRANT_ONLY 默认开启): 只需在 /codex/device 填码授权, 压根不需要
    # chatgpt.com 网页会话。phase1.5 对这批 push-auth 号必卡(且 Security toggle 也开不了),
    # 白耗 3-5 分钟还可能污染 cookie。官方 CLI 持有 device_auth_id 负责换 token。
    SKIP_P15 = os.environ.get("SKIP_PHASE15") == "1" or GRANT_ONLY
    if SKIP_P15:
        print("[1.5] SKIP (GRANT_ONLY/SKIP_PHASE15): 直接走 /codex/device 授权", flush=True)
    print("[1.5] chatgpt.com login (no settings switch click)" if not SKIP_P15 else "", flush=True)
    if not SKIP_P15:

        def _submit_form(p_page):
            """触发 React form submit: 优先 click 黑色 Continue 按钮,fallback Enter, fallback requestSubmit"""
            # find 第一个 visible black/primary Continue button (排除 social with Google/Apple/phone)
            try:
                btns = p_page.evaluate("""() => {
                    return [...document.querySelectorAll('button')].filter(b => {
                        const t = (b.innerText||'').trim();
                        return /^(Continue|Sign in|Submit|Verify|Log in)$/i.test(t)
                            && !/google|apple|phone|microsoft/i.test(t)
                            && (b.type === 'submit' || b.closest('form'));
                    }).map(b => {
                        const r = b.getBoundingClientRect();
                        return {text: b.innerText.trim(), type: b.type||'', x: r.x, y: r.y, w: r.width, h: r.height};
                    });
                }""")
                for b in btns:
                    if b['w'] > 0 and b['h'] > 0:
                        p_page.mouse.click(b['x']+b['w']/2, b['y']+b['h']/2)
                        print(f"    submit click: '{b['text']}' @ ({int(b['x']+b['w']/2)},{int(b['y']+b['h']/2)})", flush=True)
                        return
            except Exception as e:
                print(f"    submit btn dump fail: {e}", flush=True)
            # fallback: Enter
            try: p_page.keyboard.press("Enter"); print("    submit fallback: Enter", flush=True); return
            except: pass
            # last: requestSubmit
            p_page.evaluate("() => { const f=document.querySelector('form'); if (f) (f.requestSubmit?f.requestSubmit():f.submit()); }")
            print("    submit fallback: form.requestSubmit", flush=True)

        def wait_chatgpt_cf(p_page, max_wait=90):
            """chatgpt.com login picker may show a Cloudflare Turnstile gate first."""
            deadline = time.time() + max_wait
            clicked = False
            while time.time() < deadline:
                try:
                    if p_page.locator("input[type='email'], input[autocomplete='username']").count() > 0:
                        return True
                    title = p_page.title()
                    body = p_page.content().lower()[:2000]
                except Exception:
                    title, body = "", ""
                cf_present = (
                    "verify you are human" in body
                    or "challenges.cloudflare" in body
                    or "turnstile" in body
                    or "just a moment" in title.lower()
                )
                if cf_present and not clicked:
                    try:
                        pos = p_page.evaluate("""() => {
                            for (const f of document.querySelectorAll('iframe')) {
                                const src = (f.src || '').toLowerCase();
                                const title = (f.title || '').toLowerCase();
                                if (src.includes('cloudflare') || src.includes('turnstile') ||
                                    title.includes('challenge') || title.includes('verify')) {
                                    const r = f.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {
                                        return {x: r.x, y: r.y, w: r.width, h: r.height};
                                    }
                                }
                            }
                            return null;
                        }""")
                        if pos:
                            cx = pos["x"] + 30
                            cy = pos["y"] + pos["h"] / 2
                        else:
                            cx, cy = 510, 450
                        p_page.mouse.move(cx - 40, cy - 25, steps=10)
                        time.sleep(0.3)
                        p_page.mouse.move(cx, cy, steps=12)
                        time.sleep(0.3)
                        p_page.mouse.click(cx, cy)
                        clicked = True
                        print(f"    clicked chatgpt.com CF @ ({int(cx)},{int(cy)})", flush=True)
                    except Exception as e:
                        print(f"    chatgpt.com CF click failed: {e}", flush=True)
                time.sleep(2)
            return p_page.locator("input[type='email'], input[autocomplete='username']").count() > 0

        def fill_login_and_otp(p_page, need_pwd=True):
            """复用 email→password→OTP 步骤"""
            wait_chatgpt_cf(p_page)
            _enter_email_resilient(p_page, EMAIL, "login")
            _submit_form(p_page)
            time.sleep(5)
            print(f"    after email submit url={p_page.url[:100]}", flush=True)
            if need_pwd:
                for _ in range(15):
                    if "password" in p_page.url.lower() or "passkey" in p_page.url.lower(): break
                    time.sleep(1)
                # passkey challenge → click through to password
                if "passkey" in p_page.url.lower() or "auth_challenge" in p_page.url.lower():
                    print(f"    passkey challenge detected, switching to password...", flush=True)
                    try:
                        alt = p_page.locator("a, button").filter(has_text=re.compile(r"password|another.*(way|method)", re.I))
                        if alt.count() > 0:
                            alt.first.click()
                            time.sleep(4)
                            print(f"    after passkey bypass url={p_page.url[:100]}", flush=True)
                    except Exception as e:
                        print(f"    passkey bypass failed: {e}", flush=True)
                try:
                    p_page.wait_for_selector("input[type='password']", timeout=15000)
                    p_page.locator("input[type='password']").first.click()
                    p_page.keyboard.type(CHATGPT_PW, delay=80)
                    ss(p_page, "p15a2-pw-filled")
                    _submit_form(p_page)
                    time.sleep(6)
                    print(f"    after pw submit url={p_page.url[:100]}", flush=True)
                except Exception as e:
                    print(f"    password step skipped: {e}", flush=True)
            # ── robust OTP: 单框/6框布局 + auto-submit 感知 + fresh-OTP 重试 ──
            # 旧版 bug: `input.first` 把 6 位塞进第一个框 / 不验证 advance / 不重试,
            # 撞 OpenAI 当前 /email-verification(6 独立框 or auto-submit)必卡。
            def _needs_otp(pg):
                try:
                    return ("email-verification" in pg.url
                            or "verification" in pg.content().lower()[:5000])
                except Exception:
                    return False

            def _otp_advanced(pg):
                try:
                    return "verification" not in pg.url
                except Exception:
                    return False

            def _type_otp(pg, code):
                """定位 OTP 输入并键入。单框直接 fill;6 框逐格 fill 并校验落位。
                走 236 代理时 keyboard.type 的 delay 会被 React 自动跳焦抢拍丢位
                (实测 6 位只落 3 位 '962'),故多框改逐格 fill + 落位校验 + 重试。"""
                loc, n = None, 0
                for sel in ("input[autocomplete='one-time-code']",
                            "input[inputmode='numeric']",
                            "input[name='code']",
                            "input[maxlength='1']",
                            "input[type='tel']"):
                    try:
                        cand = pg.locator(sel)
                        if cand.count() > 0:
                            loc, n = cand, cand.count(); break
                    except Exception:
                        pass
                if n == 0:  # 语义选择器没命中 → 兜底表单可见 input
                    try:
                        loc = pg.locator("form input:visible")
                        n = loc.count()
                    except Exception:
                        n = 0
                if n == 0:
                    return False
                print(f"    OTP inputs located: n={n}", flush=True)
                code = code.strip()
                if n == 1:
                    # 单框:必须真键入触发 React onChange (纯 fill 不触发 → Continue 不激活 →
                    # 提交无效, 实证 acct-86 OTP 820238 填了但 advanced=False)。键入后按 Enter。
                    for _ in range(3):
                        try:
                            loc.first.click()
                            loc.first.press("Control+a"); pg.keyboard.press("Backspace")
                            pg.keyboard.type(code, delay=140)   # 逐位真键入, 触发 onChange
                            time.sleep(0.5)
                            if (loc.first.input_value() or "").strip() == code:
                                try:
                                    loc.first.press("Enter")     # 单框常靠 Enter 提交
                                except Exception:
                                    pass
                                return True
                        except Exception:
                            pass
                        time.sleep(0.4)
                    return True
                # 多框(每格 1 位):逐格 fill,避免自动跳焦抢拍
                for attempt in range(3):
                    try:
                        for i in range(min(n, len(code))):
                            box = loc.nth(i)
                            box.click()
                            try: box.fill(code[i])
                            except Exception:
                                pg.keyboard.type(code[i], delay=60)
                            time.sleep(0.08)
                        # 校验:拼接每格值 == code
                        got = "".join((loc.nth(i).input_value() or "") for i in range(min(n, len(code))))
                        print(f"    OTP boxes filled got='{got}' want='{code}' (try {attempt+1})", flush=True)
                        if got == code:
                            return True
                        # 清空每格重试
                        for i in range(n):
                            try: loc.nth(i).fill("")
                            except Exception: pass
                    except Exception as e:
                        print(f"    OTP box fill err: {e}", flush=True)
                    time.sleep(0.5)
                return True

            # 等 verification 页出现(pw submit 后可能还没 redirect)
            for _ in range(15):
                if _needs_otp(p_page):
                    break
                time.sleep(1)

            # OTP-login 账号: email 提交后停在 'Enter your password' 页(底部有
            # 'Log in with a one-time code')。若还没进 OTP 页, 点该按钮切验证码登录。
            if not _needs_otp(p_page):
                for _sel in ("button:has-text('Log in with a one-time code')",
                             "a:has-text('Log in with a one-time code')",
                             "button:has-text('one-time code')",
                             "a:has-text('one-time code')",
                             "text=/log in with a one-time code|验证码登录|邮箱验证码/i"):
                    try:
                        _l = p_page.locator(_sel).first
                        if _l.count() > 0 and _l.is_visible(timeout=1500):
                            _l.click(timeout=5000)
                            print(f"    clicked OTP-mode switch {_sel!r}", flush=True)
                            time.sleep(4)
                            break
                    except Exception:
                        pass

            # ── authenticator-app 2FA(飞书表带 2FA 密钥的号): 本地算 TOTP 填入 ──
            # 必须在邮箱 OTP 分支之前: authenticator 页 body 也含 "verification",
            # _needs_otp 会误判成邮箱 OTP → 去邮箱空等取不到码。
            if TOTP_SECRET and _is_totp_page(p_page):
                print("  [totp] authenticator-app challenge detected (login)", flush=True)
                for _ta in range(3):
                    _code = totp_now()
                    print(f"  [totp] code={_code} (try {_ta+1}/3)", flush=True)
                    _type_otp(p_page, _code)
                    ss(p_page, f"p15a3-totp-filled-{_ta}")
                    _adv = False
                    for _ in range(5):
                        time.sleep(1)
                        if _otp_advanced(p_page):
                            _adv = True; break
                    if not _adv:
                        _submit_form(p_page)
                        for _ in range(18):
                            time.sleep(1)
                            if _otp_advanced(p_page):
                                _adv = True; break
                    print(f"  [totp] after fill url={p_page.url[:100]} advanced={_adv}", flush=True)
                    if _adv:
                        break
                    time.sleep(31)   # 换下一个 30s 窗口再试(同码重填必然再拒)

            if _needs_otp(p_page):
                since = int(time.time()) - 600
                otp_ok = False
                # settle 规则(用户 2026-07-20): 等 1min → 刷新 → 再等 1min → 才读码。
                # 必须在**打开收件箱之后**做, 否则没有可刷新的列表(见 settle_inbox)。
                for attempt in range(3):
                    mp = mailcom_login(mail_ctx)
                    if attempt == 0:
                        settle_inbox(mp, "otp")
                    otp, _ = get_otp(mp, since)
                    if mp is not None:
                        mp.close()
                    if not otp:
                        print(f"  ⚠ OTP fetch failed (try {attempt+1}/3)", flush=True)
                        time.sleep(5); continue
                    print(f"  ✅ OTP={otp} (try {attempt+1}/3)", flush=True)
                    if not _type_otp(p_page, otp):
                        print("  ⚠ no OTP input located on page", flush=True)
                    ss(p_page, f"p15a3-otp-filled-{attempt}")
                    # 6 位常 auto-submit;先等自动 advance,不动再 fallback 点 Continue
                    advanced = False
                    for _ in range(5):
                        time.sleep(1)
                        if _otp_advanced(p_page):
                            advanced = True; break
                    if not advanced:
                        _submit_form(p_page)
                        for _ in range(18):
                            time.sleep(1)
                            if _otp_advanced(p_page):
                                advanced = True; break
                    print(f"    after OTP url={p_page.url[:100]} advanced={advanced}", flush=True)
                    if advanced:
                        otp_ok = True; break
                    # 卡住 → 请求重发,后续只接受更新的 code
                    try:
                        rl = p_page.locator("button, a").filter(
                            has_text=re.compile(r"resend|send.*code|new code|didn.?t get", re.I))
                        if rl.count() > 0:
                            rl.first.click(); print("    clicked resend code", flush=True)
                            time.sleep(4); since = int(time.time()) - 20
                    except Exception:
                        pass
                if not otp_ok:
                    print("  ⚠ OTP flow failed after 3 tries", flush=True)

        chat_page = ctx.new_page()
        chat_page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=GOTO_MS)
        time.sleep(3)
        for _ in range(30):
            t = chat_page.title()
            if t and "moment" not in t.lower(): break
            time.sleep(2)
        ss(chat_page, "p15a-chatgpt-picker")
        fill_login_and_otp(chat_page)
        for i in range(40):
            time.sleep(1)
            u = chat_page.url
            if "chatgpt.com" in u and "/auth" not in u and "/login" not in u:
                break
            # push-auth(手机批准)会把 phase1.5 卡死在这里干等 40s → chatgpt.com 始终未登录
            # → 后面开 Codex toggle 没有 chatgpt.com 会话可用(acct-112 实证根因)。
            # 与主流程 [4.5] 同样点 'Try with email' 退回邮箱 OTP, 再让 fill_login_and_otp 收尾。
            if _is_push_auth(chat_page):
                print("  [p15-push-auth] detected — clicking 'Try with email'", flush=True)
                try:
                    if _click_try_with_email(chat_page, "p15-push-auth"):
                        ss(chat_page, "p15a4-after-try-with-email")
                        fill_login_and_otp(chat_page)
                    else:
                        ss(chat_page, "p15a4-no-try-with-email")
                except Exception as e:
                    print(f"  [p15-push-auth] err: {e}", flush=True)
        print(f"  after chatgpt.com login url={chat_page.url[:100]}", flush=True)
        # 停在 auth.openai.com/api/accounts/authorize = OAuth 重定向端点(还在跳转中),
        # 40s 循环到点就退出会误判"未登录"(acct-112 实证)。再等一会并显式 goto 回
        # chatgpt.com 落 cookie, 才能判断会话到底建立没有。
        if "chatgpt.com" not in chat_page.url or "/auth" in chat_page.url:
            try:
                for _ in range(10):
                    time.sleep(2)
                    if "chatgpt.com" in chat_page.url and "/auth" not in chat_page.url:
                        break
                chat_page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=GOTO_MS)
                time.sleep(6)
                print(f"  after settle+goto url={chat_page.url[:100]}", flush=True)
                # 落地后若是匿名页(有 login-button), 点它走 SSO: 本浏览器已在
                # auth.openai.com 认证过, 正常会静默跳回并落 cookie, 无需再取 OTP。
                for _try in range(2):
                    try:
                        if not chat_page.evaluate("""() => !!document.querySelector(
                                "[data-testid='login-button'],[data-testid='signup-button']")"""):
                            break
                        print(f"  [p15-sso] 匿名页 → 点 login-button 走 SSO (try {_try+1})", flush=True)
                        chat_page.locator("[data-testid='login-button']").first.click(timeout=5000)
                        time.sleep(10)
                        print(f"  [p15-sso] after url={chat_page.url[:100]}", flush=True)
                        # 可能又落到 push-auth / OTP, 交给已有逻辑收尾
                        if _is_push_auth(chat_page):
                            _click_try_with_email(chat_page, "p15-sso-push-auth")
                        fill_login_and_otp(chat_page)
                        time.sleep(6)
                        chat_page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=GOTO_MS)
                        time.sleep(6)
                    except Exception as e:
                        print(f"  [p15-sso] err: {str(e)[:70]}", flush=True)
                        break
            except Exception as e:
                print(f"  settle goto err: {e}", flush=True)
        ss(chat_page, "p15b-chatgpt-logged-in")
        # URL 落在 chatgpt.com/ 不代表已登录: ChatGPT 允许**匿名聊天**, 匿名页 URL 也是
        # chatgpt.com/ 且渲染出完整聊天界面 → 只看 URL 会误判成已登录, 后面开 toggle 时
        # 才发现 Security 标签压根不存在(acct-112 实证: dump 出 login-button/signup-button)。
        # 以 DOM 为准: 存在 login/signup 按钮 = 未登录。
        logged = "chatgpt.com" in chat_page.url and "/auth" not in chat_page.url
        if logged:
            try:
                anon = chat_page.evaluate("""() => !!document.querySelector(
                    "[data-testid='login-button'],[data-testid='signup-button']")""")
                if anon:
                    logged = False
                    print("  ⚠ chatgpt.com 是匿名会话(有 Log in 按钮), 判定未登录", flush=True)
            except Exception:
                pass
        if os.environ.get("BILLING_INSPECT") == "1" or os.environ.get("BILLING_RENEW") == "1":
            do_renew = os.environ.get("BILLING_RENEW") == "1"
            print(f"[BILLING] logged={logged} renew={do_renew}", flush=True)
            try:
                chat_page.goto("https://chatgpt.com/#settings/Billing",
                               wait_until="domcontentloaded", timeout=GOTO_MS)
                time.sleep(6)
                ss(chat_page, "bill-01")
            except Exception as e:
                print(f"[BILLING] goto err: {e}", flush=True)
            def _btxt():
                try: return chat_page.inner_text("body")
                except Exception: return ""
            before = _btxt()
            canceled = bool(re.search(r"will be canceled|will be cancelled", before, re.I))
            renews = bool(re.search(r"renews on|will renew", before, re.I))
            print(f"[BILLING-STATE] canceled={canceled} renews={renews}", flush=True)
            m = re.search(r"(will be cancel\w+ on [^\n]+|renews on [^\n]+|will renew[^\n]*)", before, re.I)
            if m: print(f"[BILLING-LINE] {m.group(1).strip()[:80]}", flush=True)
            plan_m = re.search(r"ChatGPT (Pro|Plus)[^\n]*", before)
            if plan_m: print(f"[BILLING-PLAN] {plan_m.group(0).strip()[:60]}", flush=True)
            pay_m = re.search(r"(Mastercard|Visa|American Express|card ending[^\n]*)", before, re.I)
            print(f"[BILLING-PAY] {pay_m.group(0) if pay_m else 'NONE'}", flush=True)
            if os.environ.get("BILLING_DUMP") == "1":
                _keep = [l.strip() for l in before.split("\n") if l.strip()]
                print("[BILLING-DUMP-BEGIN]", flush=True)
                for l in _keep[:120]:
                    print("  | " + l[:160], flush=True)
                print("[BILLING-DUMP-END]", flush=True)

            if do_renew and canceled:
                clicked = False
                for getter in (
                    lambda: chat_page.get_by_role("button", name=re.compile(r"Renew", re.I)),
                    lambda: chat_page.get_by_text(re.compile(r"Renew Pro Plan|Renew Plan|Renew", re.I)),
                ):
                    try:
                        b = getter()
                        if b.count() > 0:
                            b.first.click(); clicked = True
                            print("[BILLING] clicked Renew", flush=True); break
                    except Exception:
                        pass
                if not clicked:
                    print("[BILLING] ✗ Renew button not found", flush=True)
                else:
                    time.sleep(3); ss(chat_page, "bill-02-after-renew-click")
                    # 可能弹确认框:点其中的确认按钮
                    for _ in range(2):
                        try:
                            dlg = chat_page.get_by_role("button",
                                  name=re.compile(r"Renew|Confirm|Continue|Resubscribe|Keep", re.I))
                            if dlg.count() > 0 and dlg.first.is_visible():
                                dlg.first.click(); print("[BILLING] confirm modal clicked", flush=True)
                                time.sleep(3)
                        except Exception:
                            pass
                    time.sleep(5); ss(chat_page, "bill-03-final")
                    after = _btxt()
                    still_cancel = bool(re.search(r"will be cancel", after, re.I))
                    now_renew = bool(re.search(r"renews on|will renew", after, re.I))
                    am = re.search(r"(will be cancel\w+ on [^\n]+|renews on [^\n]+|will renew[^\n]*)", after, re.I)
                    print(f"[BILLING-RESULT] still_canceled={still_cancel} now_renews={now_renew} "
                          f"line={am.group(1).strip()[:70] if am else '?'}", flush=True)
                    if not still_cancel:
                        print("[BILLING] ✅ RENEW ENABLED", flush=True)
                    else:
                        print("[BILLING] ⚠ still shows canceled — check screenshot bill-03-final", flush=True)
            elif do_renew and not canceled:
                print("[BILLING] already auto-renewing (no cancel notice) — nothing to do", flush=True)
            sys.exit(0)
        if logged:
            print("  chatgpt.com login ok; enabling Codex device-code toggle...", flush=True)
            try:
                chat_page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=GOTO_MS)
                time.sleep(7)
                # 已在 chatgpt.com 时改 hash 不触发 SPA 路由 → settings 模态压根不打开,
                # 截图只见聊天界面, 于是"switch 一个也找不到"(acct-112 实证)。reload 强制
                # 按 hash 渲染; 仍没 switch 就再点头像→Settings 兜底。
                if chat_page.locator("button[role='switch']").count() == 0:
                    try:
                        chat_page.reload(wait_until="domcontentloaded", timeout=GOTO_MS)
                        time.sleep(8)
                        print(f"  p15c reload switches={chat_page.locator('button[role=switch]').count()}", flush=True)
                    except Exception as e:
                        print(f"  p15c reload err: {e}", flush=True)
                if chat_page.locator("button[role='switch']").count() == 0:
                    # dump 真实 DOM 再决定点什么: 盲猜选择器已浪费多轮(acct-112)。
                    try:
                        info = chat_page.evaluate("""() => ({
                            btns: [...document.querySelectorAll('button')].slice(0, 40).map(b => ({
                                t: (b.innerText||'').trim().slice(0,28),
                                tid: b.getAttribute('data-testid')||'',
                                al: b.getAttribute('aria-label')||''})),
                            body: (document.body.innerText||'').slice(0,200)
                        })""")
                        print(f"  [p15c-dump] body={info.get('body','')[:160]!r}", flush=True)
                        for x in info.get("btns", []):
                            if x.get("t") or x.get("tid") or x.get("al"):
                                print(f"  [p15c-dump] btn t={x['t']!r} tid={x['tid']!r} al={x['al']!r}", flush=True)
                    except Exception as e:
                        print(f"  [p15c-dump] err: {e}", flush=True)
                    # 打开 settings 的多种走法: 键盘快捷键 / 各种头像按钮 / 文本匹配
                    for how, act in (
                        ("goto-settings", lambda: (chat_page.goto("https://chatgpt.com/?settings=Security",
                                                                  wait_until="domcontentloaded", timeout=GOTO_MS), time.sleep(6))),
                        ("profile-testid", lambda: chat_page.locator("[data-testid='profile-button']").first.click(timeout=4000)),
                        ("accounts-menu", lambda: chat_page.locator("button[aria-label*='ccount'], button[aria-label*='enu']").first.click(timeout=4000)),
                        ("bottom-nav-img", lambda: chat_page.locator("nav button:has(img), header button:has(img)").last.click(timeout=4000)),
                    ):
                        try:
                            act(); time.sleep(3)
                            for lbl in ("Settings", "设置"):
                                it = chat_page.get_by_text(lbl, exact=True).first
                                if it.count() > 0 and it.is_visible(timeout=1200):
                                    it.click(timeout=4000); time.sleep(4); break
                            for lbl in ("Security", "安全"):
                                sc = chat_page.get_by_text(lbl, exact=True).first
                                if sc.count() > 0 and sc.is_visible(timeout=1200):
                                    sc.click(timeout=4000); time.sleep(4); break
                            n = chat_page.locator("button[role='switch']").count()
                            print(f"  p15c {how} switches={n}", flush=True)
                            if n > 0:
                                break
                        except Exception as e:
                            print(f"  p15c {how} err: {str(e)[:60]}", flush=True)
                ss(chat_page, "p15c-security")
                # scroll panel to bottom so all switches are loaded
                try:
                    chat_page.evaluate("""() => {
                        const nodes = [...document.querySelectorAll('*')].filter(el => {
                            const s = getComputedStyle(el);
                            return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 20;
                        });
                        nodes.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                        if (nodes[0]) nodes[0].scrollTop = nodes[0].scrollHeight;
                    }""")
                    time.sleep(2)
                except Exception:
                    pass
                switches = chat_page.locator("button[role='switch']")
                exact_re = re.compile(r"codex|device\s*code|device-code|device authorization|device auth|设备代码|设备授权|设备码", re.I)
                reject_re = re.compile(r"mfa|authenticator|text message|password|passkey|security key|session|多因素|身份验证|短信|密码|通行密钥|安全密钥|会话|受信任设备|活跃会话", re.I)
                target_sw = None
                for idx in range(switches.count()):
                    sw = switches.nth(idx)
                    try:
                        label = sw.evaluate("""el => {
                            const parts = [];
                            let p = el;
                            for (let i = 0; i < 5; i++) {
                                if (!p) break;
                                const text = (p.innerText || '').trim();
                                if (text) parts.push(text);
                                p = p.parentElement;
                            }
                            return parts.join('\\n---\\n');
                        }""")
                        if exact_re.search(label) and not reject_re.search(label):
                            target_sw = sw
                            print(f"  matched Codex switch idx={idx} aria={sw.get_attribute('aria-checked')}", flush=True)
                            break
                    except Exception:
                        pass
                if target_sw is None:
                    print("  ⚠ Codex toggle not found in Security panel", flush=True)
                else:
                    before = target_sw.get_attribute("aria-checked")
                    if before != "true":
                        target_sw.click(force=True)
                        time.sleep(5)
                    after = target_sw.get_attribute("aria-checked")
                    print(f"  Codex toggle: {before} → {after}", flush=True)
                    ss(chat_page, "p15d-toggle-set")
            except Exception as e:
                print(f"  ⚠ toggle step exception: {e}", flush=True)
        else:
            print("  ❌ chatgpt.com 未登录; proceeding to device flow may require full login", flush=True)
        chat_page.close()
        print("[1.5] done — proceeding to OAuth device flow", flush=True)

    # ── 2a. Navigate to verify page (会跳转到 /log-in) ──────────────────
    print("[2] Open auth.openai.com/codex/device...", flush=True)
    page.goto(f"{AUTH_BASE}/codex/device", wait_until="domcontentloaded")
    # Wait through CF Turnstile (2026: 强制要求 user click checkbox,patchright TLS 指纹不够)
    deadline = time.time() + 90
    clicked_cf = False
    dumped = False
    while time.time() < deadline:
        title = page.title()
        body_head = page.content().lower()[:1500]
        if title and "moment" not in title.lower() and "performing security" not in body_head:
            break
        # 第一次进入时 dump 所有 iframe 帮诊断
        elapsed = time.time() - (deadline - 90)
        if not dumped and elapsed > 5:
            try:
                frames_info = page.evaluate("""() => {
                    return [...document.querySelectorAll('iframe')].map(f => {
                        const r = f.getBoundingClientRect();
                        return {src: f.src||'', title: f.title||'', id: f.id||'', name: f.name||'',
                                x: r.x, y: r.y, w: r.width, h: r.height};
                    });
                }""")
                print(f"  IFRAMES ({len(frames_info)}):", flush=True)
                for fi in frames_info:
                    print(f"    src={fi['src'][:80]!r} title={fi['title']!r} id={fi['id']!r} box={int(fi['w'])}x{int(fi['h'])}", flush=True)
                dumped = True
            except Exception as e:
                print(f"  iframe dump failed: {e}", flush=True)
        # 主动 click CF Turnstile checkbox - 扫所有 iframe,找 cloudflare/challenges/turnstile 标记
        if not clicked_cf and elapsed > 8:
            try:
                pos = page.evaluate("""() => {
                    const iframes = [...document.querySelectorAll('iframe')];
                    for (const f of iframes) {
                        const src = (f.src||'').toLowerCase();
                        const title = (f.title||'').toLowerCase();
                        if (src.includes('cloudflare') || src.includes('challenges') ||
                            src.includes('turnstile') || title.includes('cloudflare') ||
                            title.includes('challenge') || title.includes('verify')) {
                            const r = f.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                return {x: r.x, y: r.y, w: r.width, h: r.height,
                                        src: f.src, title: f.title};
                            }
                        }
                    }
                    return null;
                }""")
                if pos:
                    cx = pos['x'] + 30
                    cy = pos['y'] + pos['h'] / 2
                    print(f"  found CF iframe: title={pos['title']!r} box={int(pos['w'])}x{int(pos['h'])} @ ({int(pos['x'])},{int(pos['y'])})", flush=True)
                    # 模拟真人:先 hover 再 click
                    page.mouse.move(cx - 50, cy - 30, steps=10)
                    time.sleep(0.3)
                    page.mouse.move(cx, cy, steps=15)
                    time.sleep(0.4)
                    page.mouse.click(cx, cy)
                    clicked_cf = True
                    print(f"  ✓ clicked CF Turnstile @ ({int(cx)},{int(cy)})", flush=True)
                    time.sleep(3)
                    continue
                else:
                    if not clicked_cf:
                        # 兜底:无 iframe 命中,用屏幕坐标基于截图位置点击(checkbox ~210,335)
                        print(f"  no CF iframe matched,fallback to fixed coords (210, 335)", flush=True)
                        page.mouse.move(160, 305, steps=10)
                        time.sleep(0.3)
                        page.mouse.move(210, 335, steps=15)
                        time.sleep(0.4)
                        page.mouse.click(210, 335)
                        clicked_cf = True
                        time.sleep(3)
                        continue
            except Exception as e:
                print(f"  CF click attempt failed: {e}", flush=True)
        print(f"  [{int(deadline-time.time())}s] waiting CF... title={repr(title[:30])}", flush=True)
        time.sleep(3)
    ss(page, "01-after-cf")
    print(f"  url={page.url}  title={page.title()[:40]}", flush=True)

    # ── 2b. Fill email (Welcome back 登录页) — OR skip if session 已有 (/choose-an-account) ──
    print(f"[3] Fill email: {EMAIL}", flush=True)
    # Detect /choose-an-account 页 (session 已建立, 不需要重新登)
    if "choose-an-account" in page.url or "choose" in page.url.lower():
        print("  ✓ session 已建立 (/choose-an-account 页) - click account 跳过 email/password/OTP", flush=True)
        ss(page, "02-choose-account")
        try:
            # click 第一个 account button (only one account in this ctx)
            acc_btn = page.locator(f"button:has-text('{EMAIL}'), button:has-text('analeah'), [role='button']:has-text('{EMAIL.split('@')[0]}')")
            if acc_btn.count() == 0:
                # fallback: any clickable button containing email username
                acc_btn = page.locator("button, [role='button']").filter(has_text=EMAIL.split('@')[0])
            if acc_btn.count() > 0:
                acc_btn.first.click()
                print(f"  ✓ clicked account button", flush=True)
            else:
                # last fallback: first button on page
                page.locator("button").first.click()
                print("  ⚠ fallback: clicked first button", flush=True)
        except Exception as e:
            print(f"  account click failed: {e}", flush=True)
        time.sleep(5)
        print(f"  after account click url={page.url[:100]}", flush=True)
        ss(page, "03-after-account")
    else:
        _enter_email_resilient(page, EMAIL, "device-email")
        ss(page, "02-email-filled")
        # 提交必须**校验 URL 真的离开 /log-in**: 只点一次按钮常静默失败(命中不到/
        # React 没接住), 结果停在 /log-in → 后面 password/OTP 全跳过, user_code 页
        # 永远出不来(acct-112 实证)。多策略重试, 每次校验 URL 是否推进。
        _url_before = page.url
        for _sub in range(4):
            try:
                clicked = False
                # 1) 真实鼠标点 Continue 的 box 中心(对 React 最稳)
                pos = page.evaluate("""() => {
                    const b = [...document.querySelectorAll('button')].find(b => {
                        const t = (b.innerText||'').trim();
                        return /^(Continue|继续|Next|Sign in|Log in)$/i.test(t)
                               && !/google|apple|phone|microsoft/i.test(t)
                               && b.offsetParent !== null;
                    });
                    if (!b) return null;
                    const r = b.getBoundingClientRect();
                    return {x:r.x, y:r.y, w:r.width, h:r.height};
                }""")
                if pos and pos["w"] > 0:
                    page.mouse.click(pos["x"] + pos["w"] / 2, pos["y"] + pos["h"] / 2)
                    clicked = True
                if not clicked:
                    btn = page.locator("button:has-text('Continue'), button[type='submit']")
                    if btn.count() > 0:
                        btn.first.click(timeout=4000, force=True)
                        clicked = True
                if not clicked:
                    page.keyboard.press("Enter")
                time.sleep(5)
                if page.url != _url_before or "/log-in" not in page.url:
                    print(f"  device-email submitted (try {_sub+1}) url={page.url[:80]}", flush=True)
                    break
                # URL 没动 → 再按 Enter 推一次
                page.keyboard.press("Enter")
                time.sleep(4)
                if page.url != _url_before:
                    print(f"  device-email submitted via Enter (try {_sub+1})", flush=True)
                    break
            except Exception as e:
                print(f"  device-email submit try {_sub+1} err: {str(e)[:60]}", flush=True)
                try:
                    page.keyboard.press("Enter"); time.sleep(4)
                except Exception:
                    pass
        time.sleep(3)
        ss(page, "03-after-email")
        print(f"  url={page.url}", flush=True)

    # ── 2c. Fill password (字段B) — OR skip if session 已建立 ─────────────
    # passkey challenge → click "Try another way" → password page
    if "passkey" in page.url.lower() or "auth_challenge" in page.url.lower():
        print(f"[3.5] Passkey challenge detected, clicking 'Try another way'...", flush=True)
        before_url = page.url

        def _try_another_locator():
            """Return first visible 'Try another way' locator, or None."""
            for strat in ("role-button", "role-link", "text-exact", "text-regex"):
                try:
                    if strat == "role-button":
                        loc = page.get_by_role("button", name=re.compile(r"try another way", re.I))
                    elif strat == "role-link":
                        loc = page.get_by_role("link", name=re.compile(r"try another way", re.I))
                    elif strat == "text-exact":
                        loc = page.locator("text=Try another way")
                    else:
                        loc = page.get_by_text(re.compile(r"try another way", re.I))
                    if loc.count() > 0 and loc.first.is_visible():
                        return loc.first
                except Exception as e:
                    print(f"  locate {strat} failed: {e}", flush=True)
            return None

        # 关键：普通 click 常「声称成功但页面不动」(patchright 对该文字链接不触发)。
        # 必须 click 后校验 url/DOM 真的变了，没变就升级点击方式重试——与
        # toggle 的 6-strategy cascade 同理。
        def _url_advanced():
            u = page.url.lower()
            return ("passkey" not in u) and ("auth_challenge" not in u)

        advanced = False
        for attempt in range(6):
            loc = _try_another_locator()
            if loc is None:
                print(f"  attempt {attempt}: no 'Try another way' control visible yet", flush=True)
                time.sleep(1.5)
                continue
            # 逐级升级点击方式
            how = ["normal", "force", "dispatch", "mouse", "enter"][min(attempt, 4)]
            try:
                if how == "normal":
                    loc.click(timeout=5000)
                elif how == "force":
                    loc.click(force=True, timeout=5000)
                elif how == "dispatch":
                    loc.dispatch_event("click")
                elif how == "mouse":
                    box = loc.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:  # enter
                    loc.focus()
                    page.keyboard.press("Enter")
                print(f"  clicked 'Try another way' via {how} (attempt {attempt})", flush=True)
            except Exception as e:
                print(f"  click {how} failed: {e}", flush=True)
                time.sleep(1)
                continue
            # 校验是否真的前进了
            for _ in range(8):
                time.sleep(1)
                if _url_advanced() or "password" in page.url.lower():
                    advanced = True
                    break
            if advanced:
                print(f"  ✓ passkey page advanced via {how}: url={page.url}", flush=True)
                break
            print(f"  ⚠ {how} did not advance page (still {page.url[:60]}), escalating", flush=True)

        ss(page, "03b-passkey-bypass")
        if not advanced:
            print(f"  ❌ could not get past passkey page after cascade", flush=True)
    if "password" in page.url.lower() and FORCE_OTP_LOGIN:
        print("[4] FORCE_OTP_LOGIN=1 — skip password, switch to one-time-code", flush=True)
        if not _click_one_time_code_switch(page):
            sys.exit("❌ FORCE_OTP_LOGIN set but 'one-time code' switch not found on password page")
        # 切换后可能需先点一次 Continue 触发发码 (用 Enter + 按钮兜底, 不依赖嵌套 _submit_form)
        try:
            if "email-verification" not in page.url and "verification" not in page.content().lower()[:5000]:
                page.keyboard.press("Enter")
                time.sleep(2)
                _cb = page.locator("button:has-text('Continue'), button[type='submit']")
                if _cb.count() > 0 and _cb.first.is_enabled():
                    _cb.first.click(timeout=4000)
                time.sleep(3)
        except Exception:
            pass
        ss(page, "04-otp-mode-switched")
    elif "password" in page.url.lower():
        print(f"[4] Fill password 字段B (len={len(CHATGPT_PW)})", flush=True)
        page.wait_for_selector("input[type='password']", timeout=20000)
        page.locator("input[type='password']").first.click()
        page.keyboard.type(CHATGPT_PW, delay=80)
        ss(page, "04-pw-filled")
        # 与 email 步同理: 单击一次常静默失败, URL 不动 → 后面 OTP 页永远不出现,
        # 取到的 OTP 无处可填(acct-112 实证: OTP=181990 取到但 "no OTP input")。
        # 多策略 + 校验 URL 真的离开 /log-in/password。
        _pw_url = page.url
        for _try in range(4):
            try:
                clicked = False
                pos = page.evaluate("""() => {
                    const b = [...document.querySelectorAll('button')].find(b => {
                        const t = (b.innerText||'').trim();
                        return /^(Continue|继续|Sign in|Log in|Next)$/i.test(t)
                               && !/google|apple|phone|microsoft/i.test(t)
                               && b.offsetParent !== null;
                    });
                    if (!b) return null;
                    const r = b.getBoundingClientRect();
                    return {x:r.x, y:r.y, w:r.width, h:r.height};
                }""")
                if pos and pos["w"] > 0:
                    page.mouse.click(pos["x"] + pos["w"] / 2, pos["y"] + pos["h"] / 2)
                    clicked = True
                if not clicked:
                    btn = page.locator("button:has-text('Continue'), button:has-text('Sign in'), button[type='submit']")
                    if btn.count() > 0:
                        btn.first.click(timeout=4000, force=True)
                        clicked = True
                if not clicked:
                    page.keyboard.press("Enter")
                time.sleep(5)
                if page.url != _pw_url:
                    print(f"  password submitted (try {_try+1}) url={page.url[:80]}", flush=True)
                    break
                page.keyboard.press("Enter")
                time.sleep(4)
                if page.url != _pw_url:
                    print(f"  password submitted via Enter (try {_try+1})", flush=True)
                    break
            except Exception as e:
                print(f"  password submit try {_try+1} err: {str(e)[:60]}", flush=True)
                try:
                    page.keyboard.press("Enter"); time.sleep(4)
                except Exception:
                    pass
            print(f"  pw submit try {_try+1}: clicked={clicked} url_changed="
                  f"{page.url != _pw_url}", flush=True)
        time.sleep(2)
        ss(page, "05-after-password")
        print(f"  url={page.url}", flush=True)
        # 密码提交后落 500(服务端错): 点 'Try again' 重试, 最多 3 次。
        # 500 是 OpenAI 侧偶发, 不重试就整轮白跑(acct-112 dump 实证)。
        for _r in range(3):
            try:
                body_now = (page.evaluate("() => document.body.innerText") or "")[:200]
            except Exception:
                break
            if "Server error" not in body_now and "an error occurred" not in body_now:
                break
            print(f"  ⚠ 服务端 500 → 点 'Try again' 重试 ({_r+1}/3)", flush=True)
            try:
                ta = page.locator("button:has-text('Try again'), button:has-text('重试')").first
                if ta.count() > 0:
                    ta.click(timeout=4000, force=True)
                else:
                    page.reload(wait_until="domcontentloaded", timeout=GOTO_MS)
                time.sleep(8)
                print(f"  after Try-again url={page.url[:90]}", flush=True)
            except Exception as e:
                print(f"  Try-again err: {str(e)[:60]}", flush=True)
                break
    else:
        print(f"[4] password step skipped (url={page.url[:80]})", flush=True)

    # ── 2c.5 Push-auth verification ("Approve on your iPhone") downgrade ──
    # Some accounts have ChatGPT mobile push-auth enabled — after password the
    # flow lands on /push-auth-verification/... with "Approve on your iPhone" +
    # "Try with email" fallback button. We can't approve from a headless browser,
    # so click "Try with email" to fall back to email OTP.
    if _is_push_auth(page):
        print("[4.5] push-auth detected — clicking 'Try with email' to fall back to email OTP", flush=True)
        try:
            if _click_try_with_email(page, "4.5-push-auth"):
                ss(page, "04c-after-try-with-email")
                print(f"  url after Try-with-email={page.url[:120]}", flush=True)
            else:
                ss(page, "04c-no-try-with-email-btn")
                sys.exit("❌ push-auth page but no 'Try with email' button")
        except SystemExit:
            raise
        except Exception as e:
            ss(page, "04c-try-with-email-err")
            sys.exit(f"❌ push-auth Try-with-email click failed: {e}")

    # ── 2c.6 authenticator-app 2FA(TOTP)—— 必须在邮箱 OTP 判定之前 ──────
    # 带 2FA 密钥的号(飞书表 '2FA密钥' 列)密码后落 authenticator 挑战页,
    # 该页 body 也含 "verification"/"enter the code" → 会被 2d 的 need_otp
    # 误判成邮箱 OTP,然后去 mail.com 空等(邮箱永远不会来码)。此处先接手。
    if TOTP_SECRET and _is_totp_page(page):
        print("[4.6] authenticator-app challenge detected (oauth consent flow)", flush=True)

        def _totp_advanced(pg):
            try:
                u = (pg.url or "").lower()
                return not any(k in u for k in ("authenticator", "mfa", "totp", "verification"))
            except Exception:
                return False

        _tok = False
        for _ta in range(3):
            _code = totp_now()
            print(f"  [totp] code={_code} (try {_ta+1}/3)", flush=True)
            if not _enter_otp_resilient(page, _code, "totp"):
                ss(page, "04d-no-totp-input")
                print("  ⚠ no TOTP input located on page", flush=True)
            ss(page, f"04d-totp-filled-{_ta}")
            for _ in range(5):
                time.sleep(1)
                if _totp_advanced(page):
                    _tok = True; break
            if not _tok:
                try:
                    _sb = page.locator("button:has-text('Continue'), button:has-text('Verify'), "
                                       "button[type='submit']")
                    if _sb.count() > 0 and _sb.first.is_enabled():
                        _sb.first.click()
                    else:
                        page.keyboard.press("Enter")
                except Exception:
                    page.keyboard.press("Enter")
                for _ in range(20):
                    time.sleep(1)
                    if _totp_advanced(page):
                        _tok = True; break
            print(f"  [totp] after fill url={page.url[:100]} advanced={_tok}", flush=True)
            if _tok:
                break
            time.sleep(31)   # 下一个 30s 窗口(同码重填必被拒)
        ss(page, "04d-after-totp")
        if not _tok:
            sys.exit("❌ TOTP flow failed after 3 windows (still on authenticator challenge)")

    # ── 2d. OTP if needed ────────────────────────────────────────────────
    body_text = page.content().lower()
    # NB: the /add-phone page text also contains "one-time code"/"we'll send",
    # which would falsely trigger the email-OTP detour. Exclude it explicitly —
    # /add-phone is handled by the manual-handoff block below.
    phone_challenge_url = any(p in page.url for p in ("/add-phone", "/phone-verification"))
    need_otp = (not phone_challenge_url) and any(k in body_text for k in (
        "verification code", "one-time", "verify your email", "check your email",
        "enter the code", "we sent", "enter code",
    ))
    print(f"[5] Need OTP: {need_otp}", flush=True)
    if need_otp:
        print("  Logging into mail.com 字段A...", flush=True)

        def _enter_otp(pg, code):
            """定位 OTP 输入并键入。单框直接键入;6 独立框点首格逐位键入靠组件跳焦。"""
            handle = None; n = 0
            for _sel in ("input[autocomplete='one-time-code']", "input[name='code']",
                         "input[inputmode='numeric']", "input[maxlength='1']",
                         "input[type='tel']"):
                _loc = pg.locator(_sel)
                try:
                    if _loc.count() > 0 and _loc.first.is_visible():
                        handle, n = _loc, _loc.count(); break
                except Exception:
                    pass
            if handle is None:  # 语义选择器没命中 → 兜底第一个可见 input
                vis = [i for i in pg.locator("input").all() if i.is_visible()]
                if not vis:
                    return False
                try: vis[0].evaluate("el => el.focus()")
                except Exception: pass
                pg.keyboard.type(code, delay=120)
                return True
            print(f"    OTP inputs located: n={n}", flush=True)
            code = code.strip()
            if n == 1:
                for _ in range(3):
                    try:
                        handle.first.click()
                        handle.first.press("Control+a"); pg.keyboard.press("Backspace")
                        pg.keyboard.type(code, delay=140)   # 真键入触发 onChange
                        time.sleep(0.5)
                        if (handle.first.input_value() or "").strip() == code:
                            try: handle.first.press("Enter")
                            except Exception: pass
                            return True
                    except Exception:
                        pass
                    time.sleep(0.4)
                return True
            # 多框逐格 fill + 落位校验 (代理下 keyboard.type 会丢位, 实测 6 位落 3 位)
            for attempt in range(3):
                try:
                    for i in range(min(n, len(code))):
                        b = handle.nth(i); b.click()
                        try: b.fill(code[i])
                        except Exception: pg.keyboard.type(code[i], delay=60)
                        time.sleep(0.08)
                    got = "".join((handle.nth(i).input_value() or "") for i in range(min(n, len(code))))
                    print(f"    OTP boxes got='{got}' want='{code}' (try {attempt+1})", flush=True)
                    if got == code:
                        return True
                    for i in range(n):
                        try: handle.nth(i).fill("")
                        except Exception: pass
                except Exception as e:
                    print(f"    OTP box fill err: {e}", flush=True)
                time.sleep(0.5)
            return True

        def _otp_advanced(pg):
            try:
                return "email-verification" not in pg.url and "verification" not in pg.url
            except Exception:
                return False

        # fresh-OTP 重试:单次 submit 卡住不再 sys.exit,重取新 code 再试
        since = int(time.time()) - 600
        MANUAL_OTP = os.environ.get("MANUAL_OTP", "").strip()
        otp_ok = False
        # settle 规则(用户 2026-07-20): 等 1min → 刷新 → 再等 1min → 才读码。
        # 必须在**打开收件箱之后**做(见 settle_inbox), 否则刷不到本次新到的验证码邮件。
        for attempt in range(3):
            if MANUAL_OTP:
                # 手工注入 OTP(邮箱抓取不可用时);只用一次,失败即止
                otp, ctx_snip = MANUAL_OTP, ""
                print(f"  ✅ OTP (manual): {otp}", flush=True)
            else:
                mail_page = mailcom_login(mail_ctx)
                if attempt == 0:
                    settle_inbox(mail_page, "5][otp")
                otp, ctx_snip = get_otp(mail_page, since)
                if mail_page is not None:
                    mail_page.close()
                if not otp:
                    print(f"  ⚠ OTP fetch failed (try {attempt+1}/3)", flush=True)
                    time.sleep(5); continue
                print(f"  ✅ OTP: {otp} (try {attempt+1}/3)", flush=True)
            page.bring_to_front()
            if not _enter_otp(page, otp):
                ss(page, "06-no-otp-input")
                print("  ⚠ no OTP input located on page", flush=True)
            ss(page, f"06-otp-filled-{attempt}")
            # 6 位常 auto-submit;先等自动 advance,不动再 fallback 点 Continue
            advanced = False
            for _ in range(5):
                time.sleep(1)
                if _otp_advanced(page):
                    advanced = True; break
            if not advanced:
                sb = page.locator("button:has-text('Continue'), button:has-text('Verify'), "
                                  "button[type='submit']")
                try:
                    if sb.count() > 0 and sb.first.is_enabled():
                        sb.first.click()
                    else:
                        page.keyboard.press("Enter")
                except Exception:
                    page.keyboard.press("Enter")
                for _ in range(20):
                    time.sleep(1)
                    if _otp_advanced(page):
                        advanced = True; break
            print(f"    after OTP url={page.url[:100]} advanced={advanced}", flush=True)
            # 停用/删除账号:OTP 已被验证但账号 deactivated → re-OAuth 救不了,立即退出勿重试
            try:
                _pc = page.content().lower()
            except Exception:
                _pc = ""
            if ("account_deactivated" in _pc or "deleted or deactivated" in _pc
                    or "account_deleted" in _pc):
                ss(page, "07-account-deactivated")
                print(f"  url={page.url}", flush=True)
                sys.exit("❌ ACCOUNT_DEACTIVATED — 需新 email+新 Pro 订阅,re-OAuth 无法恢复")
            if advanced:
                otp_ok = True; break
            # 手工 OTP 只有一次(不能凭空再生),填了没过就止,避免空转
            if MANUAL_OTP:
                print("  ⚠ manual OTP didn't advance — stopping (code stale/invalid?)", flush=True)
                break
            # 卡住 → 请求重发,后续只接受更新 code
            try:
                rl = page.locator("button, a").filter(
                    has_text=re.compile(r"resend|send.*code|new code|didn.?t get", re.I))
                if rl.count() > 0:
                    rl.first.click(); print("    clicked resend code", flush=True)
                    time.sleep(4); since = int(time.time()) - 20
            except Exception:
                pass
        ss(page, "07-after-otp")
        print(f"  url={page.url}", flush=True)
        if not otp_ok:
            sys.exit("❌ OTP flow failed after 3 tries (still on /email-verification)")

    # ── 2d-phone. "Phone number required" risk-control challenge ─────────────
    # OpenAI sometimes forces phone binding (/add-phone) before re-issuing a
    # token. If PHONE_NUMBER + SMS_API_URL are set, fill the phone, then poll
    # the virtual-number SMS inbox for the 6-digit code. If they're not set,
    # screenshot and hand off to a human.
    pc = page.content().lower()
    if any(p in page.url for p in ("/add-phone", "/phone-verification")) or ("phone number" in pc and any(k in pc for k in (
            "add your phone", "phone number required", "we'll send", "we will send",
            "verify it", "one-time code"))):
        print("[5a] 'Phone number required' page detected", flush=True)
        ss(page, "5a0-phone-required")
        print(f"  url={page.url}", flush=True)
        if not PHONE_NUMBER or not SMS_API_URL:
            print("  ⏸ no PHONE_NUMBER/SMS_API_URL — MANUAL handoff", flush=True)
            sys.exit("⏸ MANUAL_PHONE_REQUIRED")
        sms_baseline = _sms_codes(_sms_fetch())
        print(f"  sms baseline codes: {sms_baseline}", flush=True)
        # phone field is the tel input (country-code <Select> defaults to US +1)
        phone_in = None
        tel = page.locator("input[type='tel']")
        if tel.count() > 0:
            phone_in = tel.first
        else:
            vis = [i for i in page.locator("input").all() if i.is_visible()]
            phone_in = vis[-1] if vis else None
        if phone_in is None:
            ss(page, "5a1-no-phone-input")
            sys.exit("❌ no phone input found on phone-required page")
        # Focus via JS (no pointer click → not blocked by react-aria overlay)
        try:
            phone_in.evaluate("el => el.focus()")
        except Exception:
            pass
        page.keyboard.type(PHONE_NUMBER, delay=80)
        ss(page, "5a2-phone-filled")
        cb = page.get_by_role("button", name="Continue", exact=True)
        if cb.count() > 0 and cb.first.is_enabled():
            cb.first.click()
        else:
            page.keyboard.press("Enter")
        time.sleep(6)
        ss(page, "5a3-after-phone")
        print(f"  url={page.url}", flush=True)
        pc2 = page.content().lower()
        if "not valid" in pc2 or "invalid" in pc2:
            ss(page, "5a3b-phone-invalid")
            sys.exit(f"❌ phone rejected as invalid: {PHONE_NUMBER}")
        print("  polling SMS api for OTP...", flush=True)
        sms_otp = poll_sms_otp(sms_baseline, timeout=150)
        if not sms_otp:
            ss(page, "5a4-sms-timeout")
            sys.exit("❌ no new SMS OTP within 150s")
        print(f"  ✅ SMS OTP: {sms_otp}", flush=True)
        otp_in2 = None
        for _sel in ("input[autocomplete='one-time-code']", "input[name='code']",
                     "input[inputmode='numeric']", "input[type='tel']"):
            _loc = page.locator(_sel)
            if _loc.count() > 0 and _loc.first.is_visible():
                otp_in2 = _loc.first
                break
        if otp_in2 is None:
            _vis = [i for i in page.locator("input").all() if i.is_visible()]
            otp_in2 = _vis[0] if _vis else None
        try:
            otp_in2.evaluate("el => el.focus()")
        except Exception:
            pass
        page.keyboard.type(sms_otp, delay=80)
        ss(page, "5a5-sms-filled")
        cb2 = page.get_by_role("button", name="Continue", exact=True)
        if cb2.count() > 0 and cb2.first.is_enabled():
            cb2.first.click()
        else:
            page.keyboard.press("Enter")
        for _ in range(30):
            if "add-phone" not in page.url.lower() and "phone" not in page.url.lower():
                break
            time.sleep(1)
        ss(page, "5a6-after-sms")
        print(f"  url={page.url}", flush=True)

    # ── 2d2. OAuth consent page: "Sign in to Codex with ChatGPT" → Continue ──
    if "/consent" in page.url or "codex/consent" in page.url:
        print("[5b] OAuth consent page — clicking Continue...", flush=True)
        # consent button is "Continue" (dark button)
        consent_btn = page.locator("button:has-text('Continue'), button:has-text('Allow'), button:has-text('Authorize')")

        # Continue 是异步变可点的(React 渲染完才 enable)。立刻判 is_enabled() 往往为 False,
        # 旧逻辑直接 sys.exit("not toggle-related") → 明明 toggle 已开、TOTP 已过, 仍整轮报废
        # (acct-127/129 实证 2026-07-25: consent 正文已无 toggle 提示, 纯粹是判太早)。
        # 先轮询等它变 enabled(~30s), 真等不到才走下面的 disabled 分支。
        for _cw in range(20):
            try:
                if consent_btn.count() > 0 and consent_btn.first.is_enabled():
                    break
            except Exception:
                pass
            time.sleep(1.5)

        if consent_btn.count() > 0 and not consent_btn.first.is_enabled():
            ss(page, "07a-consent-disabled")
            txt = ""
            try:
                txt = page.evaluate("() => document.body.innerText")[:3000]
                print(f"  consent body text:\n{txt}", flush=True)
            except Exception:
                pass
            # Continue disabled 且提示 "Enable device code authorization for Codex"
            # = 账号 Codex device-code toggle 没开。此刻**当前会话已登录**, 直接就地去
            # Security 开 toggle 再回 consent 重试 —— 零额外 OTP。
            # (旧逻辑在此 sys.exit, 导致永远走不到下方 [7e] 的同款修复, 而两步法另起
            #  浏览器从零登录要多付 1 次 mail.com 取码, 三段串联把成功率压到 ~20%。
            #  acct-112 实证: 10 次重试全废。)
            if re.search(r"enable device code authorization|device code authorization for codex", txt, re.I):
                # GRANT_ONLY: 走 /codex/device 时密码提交后会被直接送到 consent 页
                # (跳过填码步)。此处**不要**再去开 toggle: 浏览器已完成登录, device
                # grant 由持有 device_auth_id 的官方 codex CLI 负责; 它自己的 consent
                # 流程不受网页 toggle 限制。继续留在网页只会白撞 toggle 墙。
                if GRANT_ONLY:
                    print("  [5b] GRANT_ONLY: 登录已完成, consent 交给官方 codex CLI 处理", flush=True)
                    ss(page, "07z-grant-only-login-done")
                    browser.close()
                    sys.exit(0)
                print("  [5b-toggle] consent blocked by Codex toggle off — enabling in-session via Security", flush=True)
                if _enable_codex_toggle_inline(page):
                    consent_btn = page.locator("button:has-text('Continue'), button:has-text('Allow'), button:has-text('Authorize')")
                else:
                    sys.exit("❌ consent disabled and in-session toggle enable failed")
            else:
                # 非 toggle 原因的 disabled: 多为渲染/风控抖动。仍尝试强点一次再校验,
                # 不要直接判死这一轮(grinder 会换出口重试, 但白等一整轮很贵)。
                print("  [5b] Continue 仍 disabled(非 toggle 原因) — 尝试强点", flush=True)
                try:
                    consent_btn.first.click(timeout=5000, force=True)
                    time.sleep(6)
                    print(f"  [5b] after force-click url={page.url[:100]}", flush=True)
                except Exception as e:
                    print(f"  [5b] force-click err: {str(e)[:80]}", flush=True)
                if "/consent" in page.url:
                    ss(page, "07a-consent-still-disabled")
                    sys.exit("❌ consent Continue disabled (not toggle-related); "
                             "not clicking Security/MFA switches")

        # 2026-08-20 acct-241 实证: 上面 [5b] disabled 分支 force-click 成功后 URL 已离开
        # /consent(跳到 deviceauth/callback → 渲染 9 位 user_code 输入页, 见 [2e] 注释),
        # 但此处旧逻辑无条件 consent_btn.first.click() 又硬点一次仍 disabled 的 Continue →
        # 默认 30s timeout 抛**未捕获**异常 → 脚本在到达 [2e] 填 user_code 前崩溃(auth.json 空)。
        # 改为: 仅当 Continue 真 enabled 才点; disabled(已 force-click 跳走)则直接落 [2e]。
        try:
            if consent_btn.count() > 0 and consent_btn.first.is_enabled():
                consent_btn.first.click()
            elif consent_btn.count() == 0:
                page.keyboard.press("Enter")
            # else: Continue 仍在但 disabled → 不硬点(会 30s timeout 崩), 交给 [2e] 填 user_code
        except Exception as e:
            print(f"  [5b] final consent click skipped: {str(e)[:80]}", flush=True)
        # wait to leave consent
        for _ in range(30):
            if "/consent" not in page.url:
                break
            time.sleep(1)
        ss(page, "07b-after-consent")
        print(f"  url={page.url}", flush=True)

    # ── 2e. 输入 user_code (9 方框页面 — Use your device code to grant access) ───
    # 注意:URL 可能是 deviceauth/callback?code=... 但页面渲染的是 user_code 输入页
    # 必须输入 user_code 才能让 OpenAI 把当前 OAuth flow 绑到我们的 device_auth_id
    print(f"[6] Filling USER_CODE: {USER_CODE}", flush=True)
    # 找 9 个 1-char input boxes (or single input)
    user_code_clean = USER_CODE.replace("-", "")  # 9XBE-AG4JT → 9XBEAG4JT
    # 等页面渲染好(可能从 consent 跳过来)
    time.sleep(3)
    # session 复用时会停在 /choose-an-account (点账号后不一定跳到 9-格页);
    # 循环: 若在 choose-account 就点账号; 等到出现 >=1 个可见 input 再继续。
    def _click_account_if_present():
        for sel in ("button:has-text('@')", "[data-testid*='account']",
                    "button:has-text('Continue')", "div[role='button']:has-text('@')"):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1200):
                    loc.click(timeout=4000); return True
            except Exception:
                pass
        return False
    def _find_code_inputs():
        """Return (kind, inputs) for an ACTUAL device-code field only.
        kind='boxes' → 6-9 single-char boxes; 'single' → one code input."""
        boxes = [b for b in page.locator("input[maxlength='1']").all() if b.is_visible()]
        if len(boxes) >= 6:
            return ("boxes", boxes)
        code_sel = ("input[autocomplete='one-time-code'], input[name*='code' i], "
                    "input[placeholder*='code' i], input[id*='code' i], "
                    "input[inputmode='numeric']")
        cin = [c for c in page.locator(code_sel).all() if c.is_visible()]
        if cin:
            return ("single", cin)
        return (None, [])

    inputs = []
    kind = None
    for _wait in range(20):  # up to ~40s
        # A login/password page can appear here on session-reuse paths. Typing
        # the user_code into its EMAIL field corrupts it (bud@mail.com →
        # bud@maiii2f-y3ktpl.com). Detect + complete login instead of blind-typing.
        try:
            body_head = page.content()[:3000]
        except Exception:
            body_head = ""
        if page.locator("input[type='password']").count() > 0 or re.search(r"enter your password", body_head, re.I):
            print("  ⚠ login/password page at user_code step — completing login, NOT typing code here", flush=True)
            try:
                pw = page.locator("input[type='password']").first
                pw.fill(CHATGPT_PW, timeout=5000)
                b = page.locator("button:has-text('Continue'), button[type='submit']")
                if b.count() > 0 and b.first.is_enabled(timeout=2000):
                    b.first.click(timeout=5000)
                else:
                    page.keyboard.press("Enter")
                time.sleep(5)
            except Exception as e:
                print(f"    pw fill failed: {str(e)[:60]}", flush=True)
            continue
        kind, inputs = _find_code_inputs()
        if kind:
            break
        if "choose-an-account" in page.url or "choose-account" in page.url:
            _click_account_if_present()
        time.sleep(2)
    ss(page, "08-user-code-page")
    print(f"  code inputs: kind={kind} n={len(inputs)}", flush=True)
    if kind is None:
        # dump 真实 DOM 而不是猜选择器(记忆: 不 dump 瞎试选择器是浪费时间)
        try:
            _d = page.evaluate("""() => ({
                url: location.href.slice(0,120),
                body: (document.body.innerText||'').slice(0,400),
                inputs: [...document.querySelectorAll('input')].slice(0,15).map(i=>({
                    t:i.type||'', n:i.name||'', id:i.id||'',
                    ml:i.maxLength, ph:i.placeholder||'',
                    al:i.getAttribute('aria-label')||'',
                    vis: i.offsetParent !== null})),
                btns: [...document.querySelectorAll('button')].slice(0,15).map(b=>({
                    t:(b.innerText||'').trim().slice(0,26),
                    tid:b.getAttribute('data-testid')||''}))
            })""")
            print(f"  [code-dump] url={_d.get('url')}", flush=True)
            print(f"  [code-dump] body={_d.get('body','')[:300]!r}", flush=True)
            for _i in _d.get("inputs", []):
                print(f"  [code-dump] input {_i}", flush=True)
            for _b in _d.get("btns", []):
                if _b.get("t") or _b.get("tid"):
                    print(f"  [code-dump] btn {_b}", flush=True)
        except Exception as _e:
            print(f"  [code-dump] err: {_e}", flush=True)
    if kind == "boxes":
        # per-cell fill (proxy latency drops chars with bulk keyboard.type)
        for i, ch in enumerate(user_code_clean[:len(inputs)]):
            try:
                inputs[i].click(timeout=3000)
                page.keyboard.type(ch, delay=60)
            except Exception:
                inputs[i].fill(ch)
    elif kind == "single":
        inputs[0].click()
        page.keyboard.press("Control+a")
        page.keyboard.type(USER_CODE, delay=80)  # try with dash
    else:
        ss(page, "09-no-code-input")
        sys.exit("❌ no device-code input field found (likely stuck on a login page — see 08-user-code-page.png)")
    ss(page, "09-code-filled")
    time.sleep(2)
    # Click the Continue button (NOT Cancel) — use get_by_role to be safe
    try:
        cont_btn = page.get_by_role("button", name="Continue", exact=True)
        if cont_btn.count() > 0 and cont_btn.first.is_enabled():
            cont_btn.first.click()
            print("  clicked Continue (by role)", flush=True)
        else:
            raise Exception("Continue not enabled or not found")
    except Exception as e:
        print(f"  get_by_role failed: {e}, fallback to keyboard Enter", flush=True)
        page.keyboard.press("Enter")
    time.sleep(6)
    ss(page, "10-after-authorize")
    print(f"  url={page.url}", flush=True)

    # ── 2f. Wait for completion ──────────────────────────────────────────
    print("[7] Wait for completion...", flush=True)
    # 有些账号在填完 user_code + Continue 后, 又弹一个 email-verification (设备授权
    # 二次邮箱验证码)。若卡在这里, 取码 (settle→多frame→真键入+Enter) 再继续。
    if "email-verification" in page.url or "verification" in page.content().lower()[:5000]:
        print("  [7] device-grant email-verification detected — fetching OTP...", flush=True)
        try:
            # settle 规则(用户 2026-07-20): 等 1min → 刷新 → 再等 1min → 才读码。
            # 必须在**打开收件箱之后**做(见 settle_inbox)。
            since7 = int(time.time()) - 600
            for a7 in range(3):
                mp7 = mailcom_login(mail_ctx)
                if a7 == 0:
                    settle_inbox(mp7, "7")
                otp7, _ = get_otp(mp7, since7)
                if mp7 is not None:
                    mp7.close()
                if not otp7:
                    print(f"  [7] device OTP fetch failed ({a7+1}/3)", flush=True)
                    time.sleep(5); continue
                print(f"  [7] device OTP={otp7} ({a7+1}/3)", flush=True)
                _enter_otp_resilient(page, otp7, "device-otp")
                time.sleep(6)
                if "email-verification" not in page.url:
                    print(f"  [7] device OTP accepted, url={page.url[:80]}", flush=True)
                    break
                # 卡住 → resend + 取新码
                try:
                    rl = page.locator("button, a").filter(has_text=re.compile(r"resend|重新发送|didn.?t get", re.I))
                    if rl.count() > 0:
                        rl.first.click(); since7 = int(time.time()) - 20; time.sleep(4)
                except Exception:
                    pass
        except Exception as e:
            print(f"  [7] device-verification handling err: {e}", flush=True)
    # 设备 OTP 通过后, OpenAI 常把用户带回 9-格 "Use your device code to grant access"
    # 页 (URL 含 /consent 但其实是 device-code grant 页, 9 格是空的)。必须重填 user_code
    # + Continue, 否则 [8] Poll auth code 永远 403 (实证 acct-87)。9 格逐格键入防丢位。
    for _grant_try in range(3):
        _boxes = [i for i in page.locator("input").all() if i.is_visible()]
        if len(_boxes) < 9:
            break  # 不是 9 格 grant 页, 无需重填
        print(f"[7c] re-fill 9-box device-code grant page (try {_grant_try+1})", flush=True)
        ucode = USER_CODE.replace("-", "")
        try:
            for _i in range(min(9, len(ucode))):
                b = _boxes[_i]; b.click()
                try: b.fill(ucode[_i])
                except Exception: page.keyboard.type(ucode[_i], delay=60)
                time.sleep(0.1)
            time.sleep(1.5)
            cont = page.get_by_role("button", name="Continue", exact=True)
            if cont.count() > 0 and cont.first.is_enabled():
                cont.first.click(); print("  [7c] clicked Continue", flush=True)
            else:
                page.keyboard.press("Enter")
            time.sleep(6)
            print(f"  [7c] after grant url={page.url[:90]}", flush=True)
            ss(page, "07c-after-grant")
            if "device" not in page.url.lower() and "/consent" not in page.url:
                break
        except Exception as e:
            print(f"  [7c] grant re-fill err: {e}", flush=True)
    # [7d] 若落在 OAuth consent 页 (有 Continue 授权按钮, 非 9 格), 必须点 Continue
    # 否则 [8] Poll auth code 永远 403 (acct-87 实证: 9 格不出现但停在 /consent)。
    for _consent_try in range(3):
        if "/consent" not in page.url and "codex/consent" not in page.url:
            break
        cbtn = page.locator("button:has-text('Continue'), button:has-text('Allow'), button:has-text('Authorize')")
        if cbtn.count() == 0:
            break
        print(f"[7d] post-OTP consent Continue (try {_consent_try+1})", flush=True)
        try:
            # 多策略点击: force click → JS dispatch → focus+Enter → scroll+click
            try:
                cbtn.first.click(force=True, timeout=4000)
            except Exception:
                pass
            time.sleep(2)
            if "/consent" in page.url:
                cbtn.first.evaluate("el => el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}))")
            time.sleep(2)
            if "/consent" in page.url:
                try:
                    cbtn.first.scroll_into_view_if_needed(timeout=2000); cbtn.first.click(timeout=4000)
                except Exception:
                    page.keyboard.press("Enter")
            time.sleep(3)
            print(f"  [7d] after consent url={page.url[:90]}", flush=True)
            ss(page, "07d-after-consent")
            if "/consent" not in page.url:
                break
            # 点击无效: dump 所有 button + checkbox + body 头部用于诊断真实 consent 页结构
            try:
                info = page.evaluate("""() => {
                  const btns = [...document.querySelectorAll('button')].map(b => ({
                    text: (b.innerText||'').trim().slice(0,40), disabled: b.disabled,
                    aria_disabled: b.getAttribute('aria-disabled'), visible: b.offsetParent !== null
                  })).filter(b => b.visible);
                  const cbs = [...document.querySelectorAll('input[type=checkbox], [role=checkbox]')].map(c => ({
                    label: (c.getAttribute('aria-label')||c.innerText||'').slice(0,60),
                    checked: c.checked || c.getAttribute('aria-checked')
                  }));
                  const body = (document.body.innerText||'').slice(0,800);
                  return {btns, cbs, body};
                }""")
                print(f"  [7d-dump] buttons={info.get('btns')}", flush=True)
                print(f"  [7d-dump] checkboxes={info.get('cbs')}", flush=True)
                print(f"  [7d-dump] body={info.get('body')[:400]!r}", flush=True)
                # 若有未勾选的 consent checkbox, 勾上再点 Continue
                if info.get('cbs'):
                    for sel in ("input[type=checkbox]", "[role=checkbox]"):
                        try:
                            c = page.locator(sel).first
                            if c.count() > 0 and c.is_visible(timeout=1000):
                                c.check(timeout=2000); time.sleep(1)
                                cbtn.first.click(force=True, timeout=3000); time.sleep(2)
                                break
                        except Exception:
                            pass
            except Exception as e:
                print(f"  [7d-dump] err: {e}", flush=True)
            # consent "Continue disabled" 因 Codex device-code toggle 未开 (skill #23):
            # 页面提示 "Enable device code authorization for Codex in ChatGPT Security Settings"。
            # 直接去 chatgpt.com/#settings/Security 开 toggle, 再回 consent 重试。
            _body_txt = info.get("body", "") if info else ""
            if "enable device code authorization" in _body_txt.lower() or "device code authorization for codex" in _body_txt.lower():
                print("  [7e] consent blocked by Codex toggle off — enabling via Security settings", flush=True)
                try:
                    consent_url = page.url
                    page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=GOTO_MS)
                    # Security 页是 React SPA, 走美国代理渲染慢; 等 switch 真出现再找
                    # (实证 acct-93: sleep 4 后 switch labels=[] 空, 页面还没渲染)。
                    sw = None
                    for _w in range(20):  # up to ~40s
                        time.sleep(2)
                        try:
                            page.mouse.wheel(0, 600)  # 滚动触发懒加载
                        except Exception:
                            pass
                        sw = page.locator("button[role='switch']")
                        if sw.count() > 0:
                            print(f"  [7e] security page: {sw.count()} switches after {(_w+1)*2}s", flush=True)
                            break
                    ss(page, "07e1-security-for-toggle")
                    # 找 Codex/device-code 开关 (aria-checked=false 的, 排除 mfa/passkey)
                    toggled = False
                    for i in range(min(sw.count(), 12)):
                        try:
                            lbl = (sw.nth(i).evaluate("el => (el.getAttribute('aria-label')||'') + ' ' + (el.closest('div')?.parentElement?.innerText||el.parentElement?.innerText||'')")
                                   or "")
                            if not re.search(r"codex|device\s*code|device-code|device authorization|设备代码|设备授权", lbl, re.I):
                                continue
                            if re.search(r"mfa|authenticator|passkey|session|2fa", lbl, re.I):
                                continue
                            chk = sw.nth(i).get_attribute("aria-checked") or ""
                            if chk == "true":
                                print(f"  [7e] toggle idx={i} already on", flush=True); toggled = True; break
                            sw.nth(i).click(force=True, timeout=4000); time.sleep(2)
                            chk2 = sw.nth(i).get_attribute("aria-checked") or ""
                            print(f"  [7e] toggle idx={i} {chk}→{chk2}", flush=True)
                            ss(page, f"07e2-toggle-{i}")
                            toggled = True; break
                        except Exception:
                            continue
                    if not toggled:
                        # fallback: dump 所有 switch label + 邻文本用于诊断, 并试点唯一非 mfa/passkey switch
                        try:
                            dump = page.evaluate("""() => [...document.querySelectorAll('button[role=switch]')].map(b => ({
                                aria: b.getAttribute('aria-label')||'', checked: b.getAttribute('aria-checked'),
                                near: (b.closest('div')?.parentElement?.innerText||'').slice(0,80)
                            }))""")
                            print(f"  [7e] no codex toggle matched; switches={dump[:12]}", flush=True)
                            # 兜底: 邻文本含 codex/device 的 switch 点开
                            for i in range(min(sw.count(), 12)):
                                near = (dump[i].get("near","") if i < len(dump) else "")
                                if re.search(r"codex|device", near, re.I) and dump[i].get("checked") != "true":
                                    sw.nth(i).click(force=True, timeout=4000); time.sleep(2)
                                    print(f"  [7e] fallback clicked switch idx={i} near={near[:40]!r}", flush=True)
                                    toggled = True; break
                        except Exception:
                            pass
                    page.goto(consent_url, wait_until="domcontentloaded", timeout=GOTO_MS)
                    time.sleep(3)
                    ss(page, "07e3-back-to-consent")
                    # 回到 consent 再点 Continue (现在应 enabled)
                    cbtn2 = page.locator("button:has-text('Continue'), button:has-text('Authorize'), button:has-text('Allow')")
                    if cbtn2.count() > 0:
                        try:
                            cbtn2.first.click(force=True, timeout=4000); time.sleep(3)
                            page.keyboard.press("Enter")
                        except Exception:
                            pass
                    time.sleep(3)
                    print(f"  [7e] after re-consent url={page.url[:90]}", flush=True)
                except Exception as e:
                    print(f"  [7e] toggle-enable err: {e}", flush=True)
        except Exception as e:
            print(f"  [7d] consent err: {e}", flush=True)
        time.sleep(2)
    # 注意: consent 后 URL 会变成 deviceauth/callback?code=ac_XXX。这个 callback 的
    # ac_ code 是浏览器 PKCE 绑定的, **不能**用它换 token (实证 acct-96: 用它 [9] 返
    # token_exchange_user_error 400)。真正可换的 authorization_code 由 [8] 的
    # deviceauth/token poll 返回 (实证 acct-93/94: 同一 callback URL, 走 [8] poll → 200)。
    # 关键: 落到 callback 页后**绝不能立刻 close** — 该页 JS 需时间 POST 完成 device grant
    # 绑定, 过早关闭 → grant 一直 pending → [8] poll 永远 403 (实证 acct-96)。所以这里
    # 停在页面等待完成文案 / 或耗尽 loop 给 callback JS 充足时间, 只等不捕获 callback code。
    _seen_callback = False
    for _w in range(30):
        body_lower = page.content().lower()
        if any(t in body_lower for t in ("may now return", "device authorized", "you can close",
                                          "signed in to codex", "successful", "all done", "successfully signed in")):
            print(f"  ✅ Browser shows success: url={page.url}", flush=True)
            break
        if "callback" in page.url and "code=" in page.url:
            if not _seen_callback:
                print(f"  ⏳ on deviceauth/callback — waiting for grant-completion JS: {page.url[:80]}", flush=True)
                _seen_callback = True
            # 落到 callback 后再多等几轮让页面 JS 完成 device grant, 不 break
        time.sleep(2)
    ss(page, "11-final")
    browser.close()

# GRANT_ONLY: 授权已在浏览器里完成, token 交换交给持有该 device_auth_id 的
# 官方 codex CLI(它一直在轮询), 本脚本到此收工。
if GRANT_ONLY:
    print("✅ GRANT_ONLY 完成: 已在 /codex/device 授权, 由官方 codex CLI 换取 token", flush=True)
    sys.exit(0)

# ── Step 3: poll for authorization_code ────────────────────────────────
print("[8] Poll /api/accounts/deviceauth/token for auth code...", flush=True)
auth_code = None
code_challenge = None
code_verifier = None
for attempt in range(60):
    if auth_code:
        break
    status, body = http_post(
        f"{AUTH_BASE}/api/accounts/deviceauth/token",
        {"device_auth_id": DEVICE_AUTH_ID, "user_code": USER_CODE},
    )
    if status == 200:
        d = json.loads(body)
        if "authorization_code" in d:
            auth_code = d["authorization_code"]
            code_challenge = d.get("code_challenge")
            code_verifier = d.get("code_verifier")
            print(f"  ✅ Got authorization_code", flush=True)
            break
    print(f"  attempt {attempt+1}: status={status} body={body[:100]}", flush=True)
    time.sleep(INTERVAL)

if not auth_code:
    sys.exit("❌ Failed to get authorization_code")

# ── Step 4: exchange code for tokens ───────────────────────────────────
print("[9] Exchange auth code → tokens at /oauth/token...", flush=True)
_form_parts = [
    "grant_type=authorization_code",
    f"code={urllib.parse.quote(auth_code)}",
    f"redirect_uri={urllib.parse.quote(f'{AUTH_BASE}/deviceauth/callback')}",
    f"client_id={CLIENT_ID}",
]
if code_verifier:  # device callback flow 无 code_verifier 时省略该参数 (发空串会 400)
    _form_parts.append(f"code_verifier={urllib.parse.quote(code_verifier)}")
form_body = "&".join(_form_parts)
status, body = http_post(
    f"{AUTH_BASE}/oauth/token",
    form_body,
    extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
)
print(f"  status={status} body={body[:300]}", flush=True)
if status != 200:
    sys.exit(f"❌ Token exchange failed: {body[:500]}")

tok = json.loads(body)
access_token  = tok["access_token"]
refresh_token = tok.get("refresh_token", "")
id_token      = tok.get("id_token", access_token)

# decode JWT
try:
    parts = access_token.split(".")
    pl = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(pl))
    exp = claims.get("exp", int(time.time()) + 3600)
    auth_claims = claims.get("https://api.openai.com/auth", {})
    account_id = auth_claims.get("chatgpt_account_id", "")
    plan_type  = auth_claims.get("chatgpt_plan_type", "?")
except Exception as e:
    print(f"  JWT decode error: {e}", flush=True)
    exp, account_id, plan_type = int(time.time()) + 3600, "", "?"

out = {
    "access_token":  access_token,
    "refresh_token": refresh_token,
    "id_token":      id_token,
    "expires_at":    exp,
    "account_id":    account_id,
}
with open(AUTH_OUT, "w") as f:
    json.dump(out, f, indent=2)

import datetime
print(f"\n✅ auth.json → {AUTH_OUT}", flush=True)
print(f"   account_id : {account_id}", flush=True)
print(f"   plan_type  : {plan_type}", flush=True)
print(f"   expires_at : {exp}  ({datetime.datetime.fromtimestamp(exp)})", flush=True)
print(f"   token_len  : {len(access_token)}", flush=True)
