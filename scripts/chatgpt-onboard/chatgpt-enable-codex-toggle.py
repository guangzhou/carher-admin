#!/usr/bin/env python3
"""Enable ChatGPT's Codex device-code authorization toggle.

Runs inside the same patchright/Xvfb container used by the ChatGPT onboarding
flow. Credentials are read from files/env by the caller; this script never
prints password values.
"""

import os
import re
import sys
import time
from pathlib import Path

from patchright.sync_api import TimeoutError as PlaywrightTimeoutError
from patchright.sync_api import sync_playwright


EMAIL = os.environ["CHATGPT_EMAIL"]
PASSWORD = Path(os.environ["CHATGPT_PW_FILE"]).read_text().strip()
MAIL_PASSWORD = Path(os.environ.get("MAIL_PW_FILE", os.environ["CHATGPT_PW_FILE"])).read_text().strip()
SCREENSHOT_DIR = Path(os.environ.get("SCREENSHOT_DIR", "/work/screenshots"))
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
ACTION = os.environ.get("ACTION", "enable-codex-toggle")
# Through the 236 US SOCKS proxy (OAUTH_PROXY set) the double-hop makes full page
# loads slow; give goto() a longer budget so chatgpt.com doesn't time out at 45s.
GOTO_MS = 120000 if os.environ.get("OAUTH_PROXY") else 45000
OTP_RE = re.compile(r"\b(\d{6})\b")
SENDER_HINTS_RE = re.compile(r"openai|chatgpt|noreply", re.I)
# 2026-08-19 acct-233 probe3 实证根因: mail.com "poseidon" webmail 把收件箱列表
# 画在 Shadow DOM web components 里。document.body.innerText **不穿透 shadowRoot**
# → 老 scraper 读到的永远只是 portal chrome (空/导航条) → 判"inbox still loading" x45
# → 盲点 y=180 (广告/表头行) → 永远读不到码。修复: 用下面这个递归下降进
# shadowRoot 的 deep_text() 取文本; OTP 行用 Playwright locator (穿透 open shadow)
# 按视觉 y 排序点最新一封, 排除广告行; 正文码从 detail-body-iframe 里 deep_text 读。
DEEP_TEXT_JS = r"""
() => {
  function deep(node, acc){
    if(!node) return;
    if(node.nodeType===3){ acc.push(node.textContent); return; }
    if(node.shadowRoot) deep(node.shadowRoot, acc);
    const kids = node.childNodes||[];
    for(const c of kids) deep(c, acc);
  }
  const acc=[];
  deep(document.body, acc);
  return acc.join(' ').replace(/\s+/g,' ').trim();
}
"""
# 收件箱列表里标记"真 ChatGPT/OpenAI 登录码邮件"的主题 (中英)。
OTP_SUBJ_RE = re.compile(r"(登录代码|登入代码|临时.*代码|login code|verification code|code.*ChatGPT|ChatGPT.*代码)", re.I)
# 广告行 (mail.com 列表顶部常插广告, 老实现盲点 y=180 就是点到它)。
AD_RE = re.compile(r"(anzeige|mail\.com games|play for free|sponsored|advertisement|book of buffalo|qantas)", re.I)


def deep_text(target):
    """Shadow-piercing innerText: descends into shadowRoot (innerText does not)."""
    try:
        return target.evaluate(DEEP_TEXT_JS) or ""
    except Exception:
        return ""
# 2026-07-25: 带 authenticator-app 2FA 的号(飞书表 '2FA密钥' 列)。密码提交后落
# authenticator 挑战页, 邮箱永远不会来码 → 必须本地算 TOTP。镜像无 pyotp, 自己算。
TOTP_SECRET = (os.environ.get("TOTP_SECRET") or "").strip().replace(" ", "").upper()
# 2026-08-15: 卖号商给的 GPT 密码搞不定(实测停在 /log-in/password 报
# "Incorrect email address or password")的号, 强制走"使用一次性验证码登录":
# 即使密码框出现也不填密码, 点 'Log in with a one-time code' 切邮箱 OTP 模式。
# 不设此 env 时默认行为不变(先试密码), 只对指定号生效。
FORCE_OTP_LOGIN = os.environ.get("FORCE_OTP_LOGIN") == "1"


def totp_now(secret=None, t=None):
    """RFC6238 TOTP-SHA1, 30s 窗口, 6 位。secret = base32(padding 可省)。"""
    import base64, hmac, hashlib, struct
    s = secret or TOTP_SECRET
    if not s:
        return None
    s = s.replace(" ", "").upper()
    s += "=" * ((8 - len(s) % 8) % 8)
    key = base64.b32decode(s, casefold=True)
    ctr = int((t if t is not None else time.time()) // 30)
    mac = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def page_needs_push_auth(page):
    """当前页是"手机批准"(push-auth)挑战?
    URL 判定不够: 实测 acct-122 密码提交后 URL 仍停在 /log-in/password, 只有正文变成
    "在你的 SM-T835 上批准 / 我们已向你的设备发送通知"。此时若按邮箱 OTP 走, 取到码却
    找不到输入框 → RESULT=ERROR detail=OTP input not found。故必须按正文判定。"""
    try:
        if "push-auth" in (page.url or "").lower():
            return True
        body = (page.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        return False
    return any(k in body for k in (
        "approve on your", "we sent a notification", "open the chatgpt app",
        "上批准", "向你的设备发送通知", "打开 chatgpt 应用", "重新发送提示",
    ))


def click_try_with_email(page):
    """点 push-auth 页的 'Try with email' 退回邮箱 OTP。按钮文案会本地化
    (中文 '试试电子邮件'), 只匹配英文会漏 → 干等超时。"""
    pat = re.compile(r"try with email|use email|试试电子邮件|使用电子邮件|改用电子邮件|电子邮件", re.I)
    for attempt in range(6):
        for how in ("role-button", "role-link", "text", "any"):
            try:
                if how == "role-button":
                    loc = page.get_by_role("button", name=pat)
                elif how == "role-link":
                    loc = page.get_by_role("link", name=pat)
                elif how == "text":
                    loc = page.get_by_text(pat)
                else:
                    loc = page.locator("button, a, [role='button']").filter(has_text=pat)
                if loc.count() > 0 and loc.first.is_visible():
                    try:
                        loc.first.click(timeout=4000)
                    except Exception:
                        loc.first.click(timeout=4000, force=True)
                    time.sleep(4)
                    if not page_needs_push_auth(page):
                        print(f"  [push-auth] fell back to email OTP via {how}", flush=True)
                        return True
            except Exception:
                pass
        time.sleep(1.5)
    shot(page, "02d-no-try-with-email-btn")
    print("  [push-auth] 'Try with email' not clickable", flush=True)
    return False


def page_needs_totp(page):
    """当前页是 authenticator-app 挑战(而非邮箱 OTP)?"""
    try:
        url = (page.url or "").lower()
        if any(k in url for k in ("authenticator", "/mfa", "totp")):
            return True
        body = (page.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        return False
    return any(k in body for k in ("authenticator app", "authentication app", "验证器应用",
                                   "身份验证器", "two-factor authentication code",
                                   "code from your authenticator", "enter the code from your"))


def totp_loop(page, max_windows=3):
    """算 TOTP 填入并提交; 失败则等下一个 30s 窗口重试(同码重填必被拒)。"""
    for i in range(max_windows):
        code = totp_now()
        if not code:
            print("  [totp] no TOTP_SECRET set — cannot answer authenticator challenge", flush=True)
            return False
        print(f"  [totp] code={code} (window {i+1}/{max_windows})", flush=True)
        try:
            submit_otp(page, code)
        except Exception as exc:
            print(f"  [totp] submit err: {exc}", flush=True)
        shot(page, f"02t-after-totp-{i}")
        if not page_needs_totp(page):
            print(f"  [totp] advanced url={page.url[:110]}", flush=True)
            return True
        time.sleep(31)
    print("  [totp] failed after all windows", flush=True)
    return False


def shot(page, name):
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        print(f"  shot: {path}", flush=True)
    except Exception as exc:
        print(f"  shot failed: {exc}", flush=True)


def submit(page):
    try:
        btns = page.evaluate("""() => {
            return [...document.querySelectorAll('button')].filter(b => {
                const t = (b.innerText || '').trim();
                return /^(Continue|Sign in|Submit|Verify|Log in|继续|登录|提交|验证)$/i.test(t)
                    && !/google|apple|phone|microsoft|电话|手机号/i.test(t)
                    && !b.disabled;
            }).map(b => { const r = b.getBoundingClientRect();
                return {text: b.innerText.trim(), x: r.x, y: r.y, w: r.width, h: r.height}; });
        }""")
        for btn in btns:
            if btn["w"] > 0 and btn["h"] > 0:
                page.mouse.click(btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
                print(f"    submit click: {btn['text']!r}", flush=True)
                return
    except Exception as exc:
        print(f"    submit coordinate click failed: {exc}", flush=True)
    buttons = page.locator("button")
    wanted = re.compile(r"^(Continue|Sign in|Submit|Log in|继续|登录|提交|验证)$", re.I)
    skip = re.compile(r"Google|Apple|phone|电话|手机号", re.I)
    for idx in range(buttons.count()):
        btn = buttons.nth(idx)
        try:
            text = (btn.inner_text(timeout=1000) or "").strip()
            if wanted.fullmatch(text) and not skip.search(text) and btn.is_visible() and btn.is_enabled():
                btn.click()
                return
        except Exception:
            pass
    submit_buttons = page.locator("button[type='submit']")
    for idx in range(submit_buttons.count()):
        btn = submit_buttons.nth(idx)
        try:
            if btn.is_visible() and btn.is_enabled():
                btn.click()
                return
        except Exception:
            pass
    page.keyboard.press("Enter")


def click_password_fallback(page):
    fallback = page.locator("a, button").filter(
        has_text=re.compile(r"password|another.*(way|method)|try another", re.I)
    )
    for idx in range(fallback.count()):
        item = fallback.nth(idx)
        try:
            if item.is_visible():
                item.click()
                time.sleep(2)
                return True
        except Exception:
            pass
    return False


def fill_first_visible(locator, value):
    for idx in range(locator.count()):
        item = locator.nth(idx)
        try:
            if item.is_visible():
                item.fill(value)
                return True
        except Exception:
            pass
    return False


def type_first_visible(locator, page, value):
    for idx in range(locator.count()):
        item = locator.nth(idx)
        try:
            if item.is_visible():
                item.click()
                if sys.platform == "darwin":
                    page.keyboard.press("Meta+A")
                else:
                    page.keyboard.press("Control+A")
                page.keyboard.type(value, delay=50)
                return True
        except Exception:
            pass
    return False


def click_otp_mode_switch(page):
    """OTP-login 账号在 email 提交后进 'Enter your password' 页, 但底部有
    'Log in with a one-time code' 按钮 → 点它切到邮箱验证码登录 (这些卖号
    默认验证码登录, 没设密码)。中英文案都覆盖。"""
    sels = [
        "button:has-text('Log in with a one-time code')",
        "a:has-text('Log in with a one-time code')",
        "button:has-text('one-time code')",
        "a:has-text('one-time code')",
        "button:has-text('验证码登录')",
        "text=/log in with a one-time code|使用一次性代码|验证码登录|邮箱验证码/i",
    ]
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                loc.click(timeout=5000)
                print(f"  clicked OTP-mode switch via {sel!r}", flush=True)
                time.sleep(4)
                return True
        except Exception:
            pass
    return False


def page_needs_otp(page):
    try:
        text = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        text = ""
    return any(
        needle in text
        for needle in (
            "verification code",
            "check your email",
            "verify your email",
            "one-time code",
            "验证码",
            "代码",
        )
    )


def find_mail_frame(page):
    deadline = time.time() + 25
    while time.time() < deadline:
        for frame in page.frames:
            if frame.name == "mail":
                return frame
        time.sleep(2)
    return None


def visible_text(target):
    # shadow-piercing: mail.com list lives in shadowRoot, plain innerText is blind.
    return deep_text(target)


def login_mailcom(page):
    page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=45000)
    # 2026-08-17 acct-210 实证: 首页 2s 不够, mail.com 首页含大量广告脚本/A-B UI,
    # 头 60-120s 内 "Log in" 链接可能延迟出现 → 后面找不到 email/password/submit button,
    # 报 "mail.com email input not found" 或 "submit button not found" (踩坑 #46)。
    # 可用 MAILCOM_HOME_SETTLE_SEC 覆盖 (默认 120s)。
    time.sleep(int(os.environ.get("MAILCOM_HOME_SETTLE_SEC", "120")))
    try:
        page.locator("a:has-text('Log in')").first.click(timeout=10000)
    except Exception:
        pass
    time.sleep(2)
    if not fill_first_visible(page.locator("input[placeholder='Email address'], #login-email, input[name='username']"), EMAIL):
        raise RuntimeError("mail.com email input not found")
    if not fill_first_visible(page.locator("input[placeholder='Password'], #login-password, input[type='password']"), MAIL_PASSWORD):
        raise RuntimeError("mail.com password input not found")
    buttons = page.locator("button:has-text('Log in'), button[type='submit']")
    clicked = False
    for idx in range(buttons.count()):
        btn = buttons.nth(idx)
        try:
            if btn.is_visible():
                box = btn.bounding_box()
                if not box or box["y"] > 50:
                    btn.click(timeout=5000)
                    clicked = True
                    break
        except Exception:
            pass
    if not clicked:
        raise RuntimeError("mail.com submit button not found")
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    time.sleep(10)
    shot(page, "mailcom-inbox")
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    if "invalid email address / password combination" in body:
        raise RuntimeError("mail.com invalid credentials")
    sender_re = re.compile(r"(openai|chatgpt|noreply@tm\.openai|noreply@|登录代码|临时)", re.I)
    def _any_frame_has_sender():
        # mail.com list may render in the top page OR any nested frame (name varies:
        # not always "mail"). Scan every frame's SHADOW-PIERCING text (deep_text),
        # not plain innerText — the poseidon list is inside shadowRoot (2026-08-19).
        try:
            if sender_re.search(deep_text(page)):
                return True
        except Exception:
            pass
        for fr in page.frames:
            try:
                if sender_re.search(deep_text(fr)):
                    return True
            except Exception:
                pass
        return False
    # 2026-08-18: mail.com now lands on a portal hub after login (top nav:
    # "Email / Photos & Files / Services / Upgrade") instead of the inbox. The
    # webmail app only opens after clicking the "Email" entry. Without this,
    # every account stalls on the portal ("inbox still loading" x45, sender
    # keyword never visible, no OTP) — confirmed acct-231/232 (both showed the
    # portal navigator/init page + ad frames in mailcom-message-opened.txt,
    # ChatGPT had sent the code, but no inbox ever rendered). An empty frame
    # named "mail" exists even on the portal, so find_mail_frame() can't tell
    # portal from inbox; gate on the portal-only "Photos & Files" nav instead.
    def _portal_hub_showing():
        try:
            t = page.evaluate("() => document.body.innerText") or ""
        except Exception:
            t = ""
        return ("Photos & Files" in t) or ("navigator/init" in (page.url or ""))
    for _ptry in range(4):
        if _any_frame_has_sender() or not _portal_hub_showing():
            break
        clicked_email = False
        for sel in ("a[href*='mailintern']", "a[href*='/mail/']",
                    "a[href$='/mail']", "a[data-portal='mail']",
                    "a:has-text('Email')", "button:has-text('Email')"):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1200):
                    loc.click(timeout=5000)
                    clicked_email = True
                    print(f"  mail.com portal->inbox: clicked Email via {sel!r}", flush=True)
                    break
            except Exception:
                pass
        if not clicked_email:
            print("  mail.com portal hub shown but no 'Email' entry matched", flush=True)
            break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        time.sleep(8)
        shot(page, "mailcom-inbox")
    for attempt in range(1, 46):
        if _any_frame_has_sender():
            print(f"  mail.com inbox loaded attempt={attempt}", flush=True)
            shot(page, "mailcom-inbox")
            return
        if attempt % 10 == 0:
            print(f"  mail.com inbox still loading; reload attempt={attempt}", flush=True)
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
        time.sleep(2)
    if "navigator" not in page.url and find_mail_frame(page) is None:
        raise RuntimeError(f"mail.com login did not reach inbox; url={page.url}")
    print("  mail.com inbox sender keyword not visible yet; proceeding", flush=True)


def extract_otp_from_open_mail(mail_frame, page):
    # 2026-08-19: opened message body renders in a nested 'detail-body-iframe'
    # inside the poseidon shadow tree. Read every frame with deep_text (shadow-
    # piercing) — plain innerText returned "" for that iframe (probe3 proof).
    texts = []
    for frame in page.frames:
        try:
            # Avoid stale codes in the inbox list after opening a message.
            if mail_frame is not page and frame.name == "mail":
                continue
            texts.append(deep_text(frame))
        except Exception:
            pass
    try:
        texts.append(deep_text(mail_frame))
    except Exception:
        pass
    for text in texts:
        if not SENDER_HINTS_RE.search(text) and "code" not in text.lower() and "验证码" not in text and "验证码" not in text:
            # also accept the ChatGPT body phrasing ("输入此临时验证码以继续")
            if "临时验证码" not in text and "登录代码" not in text:
                continue
        match = OTP_RE.search(text)
        if match:
            return match.group(1)
    return None


def dump_mail_text(page, name, limit=20000):
    parts = []
    try:
        parts.append(("page", deep_text(page)))
    except Exception:
        pass
    for idx, frame in enumerate(page.frames):
        try:
            parts.append((f"frame:{idx}:{frame.name}", deep_text(frame)))
        except Exception:
            pass
    out = SCREENSHOT_DIR / f"{name}.txt"
    try:
        out.write_text("\n\n".join(f"===== {label} =====\n{text[:limit]}" for label, text in parts), encoding="utf-8")
        print(f"  dump: {out}", flush=True)
    except Exception as exc:
        print(f"  dump failed: {exc}", flush=True)


def dump_page_text(page, name, limit=20000):
    out = SCREENSHOT_DIR / f"{name}.txt"
    try:
        text = deep_text(page)
        out.write_text(text[:limit], encoding="utf-8")
        print(f"  dump: {out}", flush=True)
    except Exception as exc:
        print(f"  dump failed: {exc}", flush=True)


def click_latest_otp_message(target):
    """Click the visually TOPMOST ChatGPT/OpenAI OTP email in mail.com list.

    2026-08-19 acct-233 probe3 实证重写: Playwright/patchright 的 get_by_text 定位器
    **穿透 open shadow root**, 所以能命中 poseidon 收件箱里的行 (deep_text 之外的第二
    条穿透路径)。老实现的坑有二: (1) 模式过宽 (OpenAI.*code / ChatGPT.*code) 会匹配到
    一个大容器/表头, 其 bounding_box y≈180(表头), 于是"点最顶"点到广告/表头而非真行;
    (2) 不排除广告行。修复: 只按 OTP_SUBJ_RE(登录代码/临时代码/login code…) 精确匹配
    邮件主题文本, 枚举全部匹配 → 排除广告行(AD_RE) → 按视觉 y 排序 → 点 y 最小(最新
    一封在最顶)那条。probe3 实测: 4 个匹配 y=[351,806,936,1001] 全 vis 且非广告,
    点 k=0(y=351)成功打开, 正文 detail-body-iframe 读到 380341。
    """
    # scroll list container to top, best-effort (mail.com SPA 常保留 scroll 位置)
    try:
        target.evaluate("() => { try{window.scrollTo(0,0);}catch(e){} document.querySelectorAll('[class*=\"scroll\"],[class*=\"list\"],[class*=\"mail-list\"]').forEach(el=>{try{el.scrollTop=0;}catch(e){}}); }")
    except Exception:
        pass
    candidates = []  # (y, index, text)
    try:
        loc_group = target.get_by_text(OTP_SUBJ_RE)
        count = loc_group.count()
        for i in range(min(count, 25)):
            loc = loc_group.nth(i)
            try:
                if not loc.is_visible(timeout=500):
                    continue
                txt = (loc.inner_text(timeout=1000) or "").strip().replace("\n", " ")[:60]
                if AD_RE.search(txt):
                    continue
                box = loc.bounding_box()
                if not box:
                    continue
                candidates.append((box["y"], i, txt))
            except Exception:
                continue
    except Exception:
        pass
    candidates.sort(key=lambda t: t[0])
    for y, i, txt in candidates:
        try:
            print(f"  click topmost otp row at y={y:.0f} txt={txt!r}", flush=True)
            target.get_by_text(OTP_SUBJ_RE).nth(i).click(timeout=5000)
            return True
        except Exception:
            continue
    # Fallback: 老的宽匹配兜底 (OTP_SUBJ_RE 一个都没命中时才走, 排除广告)
    for pattern in (r"临时 ChatGPT 登录代码", r"Your temporary ChatGPT login code",
                    r"OpenAI.*code", r"ChatGPT.*code", r"noreply@tm\.openai\.com"):
        try:
            loc_group = target.get_by_text(re.compile(pattern, re.I))
            for i in range(min(loc_group.count(), 8)):
                loc = loc_group.nth(i)
                if not loc.is_visible(timeout=500):
                    continue
                try:
                    txt = (loc.inner_text(timeout=800) or "")
                    if AD_RE.search(txt):
                        continue
                except Exception:
                    pass
                if not loc.bounding_box():
                    continue
                loc.click(timeout=3000)
                return True
        except Exception:
            pass
    return False


def fetch_mailcom_otp(pw, request_ts, prev_otp=None):
    # request_ts: 期望邮件到达时间下界 (epoch); prev_otp: 上次拿到的 OTP, 用于检测 stale 邮件并跳过
    # mail.com 邮件正文不含时间戳, 用 prev_otp 跳过同值是最可靠的"新邮件"判据
    print(f"[otp] fetching code from mail.com (prev_otp={prev_otp or 'none'})", flush=True)
    # 时序策略 (用户 2026-07-20 要求): 登录邮箱后先等 OTP 邮件落地, 取"最新一封"前
    # 先 settle 1min → refresh → 再等 1min → 才读码。避免抓到上一次登录残留的旧码
    # (mail.com 收件箱堆积多封 OTP 时旧码会被 ChatGPT 判"代码不正确")。
    # 可用 OTP_SETTLE_SEC 覆盖 (默认 60s);OTP_FAST=1 时跳过 (调试用)。
    settle = 0 if os.environ.get("OTP_FAST") else int(os.environ.get("OTP_SETTLE_SEC", "60"))
    browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    page = browser.new_page()
    try:
        login_mailcom(page)
        if settle:
            print(f"  [otp] settle {settle}s before first read (let this login's OTP land)...", flush=True)
            time.sleep(settle)
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            print(f"  [otp] refreshed inbox; settle another {settle}s...", flush=True)
            time.sleep(settle)
        for attempt in range(1, 25):  # ~ 3min: 24 * (reload+sleep ≈ 8s)
            if attempt > 1:
                print(f"  mail.com poll {attempt}/24 (avoiding prev_otp={prev_otp})", flush=True)
                page.reload(wait_until="domcontentloaded", timeout=30000)
                time.sleep(8)
            # mail.com 列表可能在顶层 page 或任意 frame(名字不一定叫 "mail"),
            # 收集所有 frame + 顶层 page 的 innerText 拼一起再匹配, 否则 OTP 邮件
            # 在 iframe 里时读不到 → 一直 "still loading"(实证 acct-86 卡这)。
            texts = []
            try:
                texts.append(deep_text(page))
            except Exception:
                pass
            for _fr in page.frames:
                try:
                    texts.append(deep_text(_fr))
                except Exception:
                    pass
            text = "\n".join(texts)
            frame = find_mail_frame(page) or page
            # New mail.com UI renders the inbox in the top page, and the list
            # often contains the OTP subject before the message is opened.
            candidate = None
            if SENDER_HINTS_RE.search(text):
                for match in OTP_RE.finditer(text):
                    start = max(0, match.start() - 180)
                    end = min(len(text), match.end() + 180)
                    ctx = text[start:end]
                    if SENDER_HINTS_RE.search(ctx) and re.search(r"code|login|verification|验证码|代码|登录", ctx, re.I):
                        if prev_otp and match.group(1) == prev_otp:
                            continue  # stale, skip
                        candidate = match.group(1)
                        print(f"  OTP candidate from inbox text: {candidate}", flush=True)
                        break
            if candidate:
                return candidate
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            # 在所有 frame(+顶层 page)里试着点开最新 OpenAI 邮件, 不只 find_mail_frame
            clicked = False
            for _tgt in [page] + list(page.frames):
                try:
                    if click_latest_otp_message(_tgt):
                        clicked = True
                        frame = _tgt
                        break
                except Exception:
                    pass
            if clicked:
                time.sleep(5)
                shot(page, "mailcom-message-opened")
                dump_mail_text(page, "mailcom-message-opened")
                code = extract_otp_from_open_mail(frame, page)
                if code and code != prev_otp:
                    print(f"  OTP found in opened mail: {code}", flush=True)
                    return code
                if code and code == prev_otp:
                    print(f"  opened mail OTP={code} == prev_otp, stale; reloading", flush=True)
            for line in lines:
                if not SENDER_HINTS_RE.search(line):
                    continue
                match = OTP_RE.search(line)
                if match and match.group(1) != prev_otp:
                    print(f"  OTP found in mail list line: {match.group(1)}", flush=True)
                    return match.group(1)
                try:
                    frame.get_by_text(line, exact=False).first.click(timeout=5000)
                    time.sleep(5)
                    shot(page, "mailcom-message-opened")
                    dump_mail_text(page, "mailcom-message-opened")
                    code = extract_otp_from_open_mail(frame, page)
                    if code and code != prev_otp:
                        print(f"  OTP found in opened mail (line click): {code}", flush=True)
                        return code
                except Exception:
                    pass
        raise RuntimeError(f"no fresh OpenAI/ChatGPT OTP mail (still seeing prev_otp={prev_otp})")
    finally:
        browser.close()


def submit_otp(page, code):
    print(f"[otp] submitting verification code {code}", flush=True)
    for locator in (
        page.locator("input[name='code'], input[autocomplete='one-time-code'], input[inputmode='numeric']"),
        page.locator("input[type='text']"),
    ):
        if fill_first_visible(locator, code):
            submit(page)
            time.sleep(8)
            shot(page, "02b-after-otp")
            return
    # 没有 OTP 输入框的常见真因: 其实停在 push-auth("手机批准")页, 不是邮箱 OTP 页。
    # 直接 raise 会把整轮判死; 先退回邮箱 OTP 再重试一次输入(acct-122 实证)。
    if page_needs_push_auth(page):
        print("  [otp] no input — actually on push-auth; falling back to email", flush=True)
        if click_try_with_email(page):
            for locator in (
                page.locator("input[name='code'], input[autocomplete='one-time-code'], input[inputmode='numeric']"),
                page.locator("input[type='text']"),
            ):
                if fill_first_visible(locator, code):
                    submit(page)
                    time.sleep(8)
                    shot(page, "02b-after-otp")
                    return
    raise RuntimeError("OTP input not found")


def otp_failed(page):
    # 检测 ChatGPT 验证码错误反馈 (中英 OpenAI 文案).
    # 严控匹配面: 只匹配 OpenAI 明确反馈"这次提交的码错了", 避免误判:
    #   - "expired" 不单独算 (主页/其他页都可能出现)
    #   - "代码" 不单独算 (OTP 输入页常态文本)
    #   - "didn't work" 太泛 — 砍掉
    # 同时还要求页面仍处于 email-verification URL 或 OTP 输入表单, 不然就是已离开 OTP 页 = 成功
    try:
        body = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        return False  # 兜底: 读不到 body 就当没失败, 走 old success 路径
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    error_phrases = (
        "代码不正确",
        "验证码不正确",
        "incorrect code",
        "invalid code",
        "code is invalid",
        "wrong code",
        "代码已过期",
        "code has expired",
        "code expired",
    )
    has_err = any(p in body for p in error_phrases)
    if not has_err:
        return False
    # 二次确认仍在 OTP 表单页 (URL 含 verification 或页面仍有 OTP 输入框)
    still_on_otp = (
        "email-verification" in url
        or "verify" in url
        or "challenge" in url
    )
    try:
        if not still_on_otp:
            still_on_otp = page.locator(
                "input[name='code'], input[autocomplete='one-time-code'], input[inputmode='numeric']"
            ).count() > 0
    except Exception:
        pass
    return still_on_otp


def click_resend_email(page):
    # 点击 "重新发送电子邮件" / "Resend email" 让 OpenAI 重新发 OTP
    selectors = [
        "text=/重新发送电子邮件/i",
        "text=/重新发送/i",
        "text=/resend email/i",
        "text=/resend/i",
        "button:has-text('Resend')",
        "button:has-text('重新发送')",
        "a:has-text('Resend')",
        "a:has-text('重新发送')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click(timeout=5000)
                print(f"  ✓ clicked resend via {sel}", flush=True)
                shot(page, "02d-resend-clicked")
                time.sleep(3)
                return True
        except Exception:
            continue
    print("  ⚠ resend link not found", flush=True)
    return False


def otp_loop(page, pw, max_attempts=3):
    # 拉 OTP → 提交 → 失败则 resend → 拉新 OTP, 至多 max_attempts 次.
    # ⚠ 兼容性保证: 第一次 attempt 跟旧 (fetch_mailcom_otp + submit_otp) 路径完全一致 —
    #    prev_otp=None ⇒ fetch_mailcom_otp 不做任何 stale 过滤, 行为与旧一字不差;
    #    只有当 otp_failed() 明确判定本次 OTP 被 ChatGPT 拒绝时, 才走 resend 二轮.
    prev_otp = None
    for attempt in range(1, max_attempts + 1):
        print(f"[otp-loop] attempt {attempt}/{max_attempts} (prev_otp={prev_otp or 'none'})", flush=True)
        if attempt > 1:
            # 只有 retry 才点 resend, 第一次走旧路径
            try:
                click_resend_email(page)
            except Exception as exc:
                print(f"  resend click raised (continuing): {exc}", flush=True)
            time.sleep(5)  # 让 OpenAI 发邮件 + mail.com 拉新
        request_ts = time.time()
        try:
            code = fetch_mailcom_otp(pw, request_ts, prev_otp=prev_otp)
        except TypeError:
            # 防御: 万一 fetch_mailcom_otp 被改回旧签名 (pw, request_ts), 用旧调用
            code = fetch_mailcom_otp(pw, request_ts)
        submit_otp(page, code)
        # 给 ChatGPT 处理 OTP 一点时间再判定 (submit_otp 内已 sleep 8s)
        try:
            failed = otp_failed(page)
        except Exception as exc:
            print(f"  otp_failed probe raised, assuming success: {exc}", flush=True)
            return  # 探测出错按旧行为: submit 完即视作成功
        if not failed:
            return  # 旧路径同款 "success / 继续主流程"
        print(f"  ✗ OTP {code} rejected by ChatGPT; will resend + retry", flush=True)
        prev_otp = code
    raise RuntimeError(f"OTP loop exhausted after {max_attempts} attempts; last={prev_otp}")


def wait_for_login_form(page):
    deadline = time.time() + 90
    clicked_cf = False
    while time.time() < deadline:
        if page.locator("input[type='email'], input[autocomplete='username']").count() > 0:
            return
        body = ""
        title = ""
        try:
            body = page.content().lower()[:3000]
            title = page.title().lower()
        except Exception:
            pass
        if not clicked_cf and (
            "turnstile" in body
            or "challenges.cloudflare" in body
            or "verify you are human" in body
            or "just a moment" in title
        ):
            try:
                page.mouse.move(450, 420, steps=8)
                page.mouse.click(450, 420)
                clicked_cf = True
                print("  clicked possible Cloudflare challenge area", flush=True)
            except Exception as exc:
                print(f"  Cloudflare click failed: {exc}", flush=True)
        time.sleep(2)
    raise TimeoutError("login form did not appear")


def login(page, pw):
    print(f"[1] login chatgpt.com as {EMAIL}", flush=True)
    page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=GOTO_MS)
    wait_for_login_form(page)
    shot(page, "01-login")

    email_submitted = False
    for attempt in range(1, 4):
        if fill_first_visible(page.locator("input[type='email'], input[autocomplete='username']"), EMAIL):
            submit(page)
            email_submitted = True
        elif page.locator("input[type='password']").count() > 0 or page_needs_otp(page):
            break
        else:
            # Some ChatGPT auth variants keep the email in the URL and hide the
            # field while still requiring a second Continue click.
            submit(page)
        time.sleep(5)
        print(f"  after email attempt {attempt} url={page.url[:120]}", flush=True)
        shot(page, f"01b-after-email-{attempt}")
        if page.locator("input[type='password']").count() > 0 or page_needs_otp(page):
            break
    if not email_submitted and page.locator("input[type='password']").count() == 0 and not page_needs_otp(page):
        shot(page, "01c-no-email-or-password")
        raise RuntimeError("visible email field not found")

    for _ in range(30):
        if page.locator("input[type='password']").count() > 0:
            break
        if "passkey" in page.url.lower() or "auth_challenge" in page.url.lower():
            click_password_fallback(page)
        time.sleep(2)

    if page.locator("input[type='password']").count() == 0:
        if TOTP_SECRET and page_needs_totp(page):
            totp_loop(page)
        elif page_needs_otp(page):
            otp_loop(page, pw)
        elif click_otp_mode_switch(page):
            # OTP-login account: switched password page → one-time-code mode
            time.sleep(2)
            otp_loop(page, pw)
        else:
            raise RuntimeError("password field did not appear")
    elif FORCE_OTP_LOGIN:
        # GPT 密码搞不定的号: 密码框虽出现, 但不填密码, 直接切一次性验证码登录。
        print("  FORCE_OTP_LOGIN=1 — skip password, switch to one-time-code", flush=True)
        if not click_otp_mode_switch(page):
            raise RuntimeError("FORCE_OTP_LOGIN set but 'one-time code' switch not found")
        time.sleep(2)
        # 切换后可能仍需先点一次 Continue 触发发码, 或直接进 OTP 页
        if not page_needs_otp(page):
            submit(page)
            time.sleep(3)
        otp_loop(page, pw)
    else:
        if not type_first_visible(page.locator("input[type='password']"), page, PASSWORD):
            raise RuntimeError("visible password field not found")
        time.sleep(1)
        shot(page, "02-password-filled")
        submit(page)
        time.sleep(8)
        print(f"  after password url={page.url[:120]}", flush=True)
        shot(page, "02c-after-password-submit")

        # 密码提交后页面是异步渲染的: 立刻判定会全部落空(URL 还是 /log-in/password、
        # body 还没换成 push-auth 文案)→ 直落邮箱 OTP 分支 → 取到码却没有输入框
        # (acct-122 两轮实证 RESULT=ERROR detail=OTP input not found)。先轮询等挑战页定型。
        for _ in range(20):
            if page_needs_push_auth(page) or (TOTP_SECRET and page_needs_totp(page)) \
                    or page_needs_otp(page) or page.locator("input[type='password']").count() == 0:
                break
            time.sleep(1.5)
        print(f"  challenge settled url={page.url[:100]} push={page_needs_push_auth(page)}", flush=True)

        # push-auth: 部分号密码提交后落 /push-auth-verification (手机批准),
        # headless 无法批准 → 点 'Try with email' fallback 到邮箱 OTP。
        # (与主 OAuth 脚本 [4.5] 同逻辑; 缺此步会在 push-auth 页干等超时 = acct-112 实证)
        if page_needs_push_auth(page):
            print("  push-auth detected — clicking 'Try with email' → email OTP", flush=True)
            click_try_with_email(page)
            shot(page, "02d-after-try-with-email")
            print(f"  url after Try-with-email={page.url[:120]}", flush=True)

        # TOTP 必须先判: authenticator 页 body 也含 "verification"/"enter the code",
        # page_needs_otp 会误判成邮箱 OTP → 去 mail.com 空等(邮箱不会来码)。
        if TOTP_SECRET and page_needs_totp(page):
            totp_loop(page)
        elif page_needs_otp(page):
            otp_loop(page, pw)

    deadline = time.time() + 60
    while time.time() < deadline:
        if "chatgpt.com" in page.url and "/auth" not in page.url and "/login" not in page.url:
            print("  login ok", flush=True)
            shot(page, "03-logged-in")
            return
        if TOTP_SECRET and page_needs_totp(page):
            totp_loop(page)
        elif page_needs_otp(page):
            otp_loop(page, pw)
        time.sleep(2)
    raise TimeoutError(f"login did not finish; url={page.url}")


SWITCH_SETTLE_MAX = int(os.environ.get("SWITCH_SETTLE_MAX", "45"))
SWITCH_SETTLE_QUIET = float(os.environ.get("SWITCH_SETTLE_QUIET", "3"))

TOGGLE_EXACT_RE = re.compile(
    r"codex|device\s*code|device-code|device authorization|device auth|设备代码|设备授权|设备码",
    re.I,
)
TOGGLE_REJECT_RE = re.compile(
    r"mfa|authenticator|text message|password|passkey|security key|session|"
    r"多因素|身份验证|短信|密码|通行密钥|安全密钥|会话|受信任设备|活跃会话",
    re.I,
)


def wait_switches_settled(page, max_wait=None, quiet=None):
    """等 settings 面板渲染稳定：button[role='switch'] 数量连续 quiet 秒不变才算完。

    为什么必须等：面板是 React 异步渲染的，原来固定 sleep 7 之后常常只渲染了一半
    （2026-08-04 acct-132/134 实证：此时只枚举到 3 个开关，Codex 那个落在 index 2；
    渲染完的对照组 acct-133 是 index 5）。在半渲染的 DOM 上取到的句柄随后会被重渲染
    替换掉 —— 6 种点击全部无效、aria-checked 永远读回 false，看着像"号被 gate 了"。
    见 memory feedback_codex_toggle_enable_failed_switch_index_means_partial_render。
    """
    max_wait = SWITCH_SETTLE_MAX if max_wait is None else max_wait
    quiet = SWITCH_SETTLE_QUIET if quiet is None else quiet
    sw = page.locator("button[role='switch']")
    prev, stable_since, t0 = -1, None, time.time()
    while time.time() - t0 < max_wait:
        try:
            cur = sw.count()
        except Exception:
            cur = -1
        if cur > 0 and cur == prev:
            if stable_since is None:
                stable_since = time.time()
            if time.time() - stable_since >= quiet:
                print(f"  switches settled: {cur} (waited {int(time.time() - t0)}s)", flush=True)
                return cur
        else:
            prev, stable_since = cur, None
        time.sleep(1.0)
    print(f"  ⚠ switches 未在 {max_wait}s 内稳定 (last={prev}) — 继续但可能是半渲染", flush=True)
    return prev


def find_codex_switch(page):
    """在当前 DOM 里重新定位 Codex device-code 开关，返回 (locator, idx)。

    ⚠ 每一轮都必须重新枚举拿新句柄，不能复用上一轮的 —— 上一轮那个可能已经 detached。
    """
    switches = page.locator("button[role='switch']")
    try:
        total = switches.count()
    except Exception:
        total = 0
    for idx in range(total):
        sw = switches.nth(idx)
        try:
            label = sw.evaluate(
                """el => {
                    const parts = [];
                    let p = el;
                    for (let i = 0; i < 5; i++) {
                        if (!p) break;
                        const text = (p.innerText || '').trim();
                        if (text) parts.push(text);
                        p = p.parentElement;
                    }
                    return parts.join('\\n---parent---\\n');
                }"""
            )
            compact = " ".join(label.split())
            print(
                f"  switch {idx}/{total}: aria={sw.get_attribute('aria-checked')} label={compact[:220]!r}",
                flush=True,
            )
            if TOGGLE_EXACT_RE.search(label) and not TOGGLE_REJECT_RE.search(label):
                print(f"  matched Codex/device-code switch {idx} (共 {total} 个开关)", flush=True)
                return sw, idx
        except Exception as exc:
            print(f"  switch {idx} inspect failed: {exc}", flush=True)
    return None, -1


def _dialog_open(page):
    """只识别**真正挡路的确认小弹窗**(如"要开启锁定模式吗?"),返回 (locator, title)。

    ⚠️ ChatGPT 的**设置主面板本身就是 div[role='dialog']** —— 早期版本把它也当成"挡路弹窗"
    去点关闭/Escape,结果把设置面板整个关掉,switch 定位随即全部超时(aria=None)。
    (2026-08-18 acct-230 patched run 实证:switch 5 本已 aria=true,却因面板被关而无法确认。)
    所以这里必须**排除设置主面板**(靠导航项文本识别),只认短小的确认框。"""
    NAV_MARKERS = ("快捷键", "受信任联系人", "家长控制", "账户安全与登录")
    for sel in ("div[role='alertdialog']", "div[role='dialog']"):
        try:
            loc = page.locator(sel)
            n = loc.count()
        except Exception:
            n = 0
        for i in range(n):
            d = loc.nth(i)
            try:
                if not d.is_visible(timeout=300):
                    continue
                txt = " ".join((d.inner_text(timeout=400) or "").split())
            except Exception:
                continue
            if any(m in txt for m in NAV_MARKERS):
                continue                     # 这是设置主面板,绝不动
            # 真正的确认框:短 + 带取消/关闭语义(锁定模式确认框正是这形态)
            if len(txt) < 700 and any(k in txt for k in ("取消", "Cancel", "以后再说", "暂不", "关闭")):
                return d, txt[:120]
    return None, ""


def _dismiss_stray_confirm(page, tag=""):
    """点掉挡路的确认弹窗。**绝不点"开启/启用/确认/Enable/Confirm"** —— 尤其
    "开启锁定模式"(锁定模式切断与外部网站/服务/工具的连接,确认了这号就废了 API 用途)。
    只点取消/以后再说/关闭/Cancel;都没有就按 Escape。返回是否清掉了一个弹窗。

    2026-08-18 acct-230(Pro 号)实证:Pro 的"账户安全与登录"页更高,坐标/祖先型点法
    (mouse box center / parent label click)会误触邻近控件弹出锁定模式确认框,其半透明
    背板随后挡死对 Codex 开关的所有后续点击 → 6 种点法全部 aria-checked=false 假象。"""
    dlg, title = _dialog_open(page)
    if dlg is None:
        return False
    print(f"  [modal-guard{tag}] 挡路弹窗: {title!r} → 只点取消/关闭(绝不确认)", flush=True)
    for txt in ("取消", "以后再说", "暂不", "关闭", "Cancel", "Not now", "Later", "Close"):
        try:
            btn = dlg.locator(f"button:has-text('{txt}')")
            if btn.count() > 0 and btn.first.is_visible(timeout=400):
                btn.first.click(timeout=3000)
                time.sleep(1.5)
                return True
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        time.sleep(1.0)
    except Exception:
        pass
    return True


def flip_switch(page, target):
    """对已定位的开关轮流试点法，返回最终 aria-checked。

    每次点完**立刻清掉误触弹窗**再读 aria —— 否则某个坐标型点法误触锁定模式确认框后,
    其背板会挡死后面所有点法(见 _dismiss_stray_confirm 注释, acct-230 Pro 实证)。"""
    def _checked():
        try:
            return target.get_attribute("aria-checked")
        except Exception:
            return None

    _dismiss_stray_confirm(page, "-pre")   # 进来先清场:上一步可能留了弹窗挡着

    before = _checked()
    dis = None
    try:
        dis = target.get_attribute("disabled") or target.get_attribute("aria-disabled")
    except Exception:
        pass
    print(f"  before aria-checked={before} disabled={dis}", flush=True)
    if before == "true":
        return before

    attempts = [
        # ⚠️ 只用**元素定向**点法,精确命中 switch 5。
        # 坐标/祖先型(mouse box center / parent label click)在 Pro 更高的安全页会误触
        # 邻近的**锁定模式**开关(switch 2)弹确认框 —— acct-230 实证,已删除,绝不再加。
        ("scroll+real click", lambda: (target.scroll_into_view_if_needed(timeout=3000), target.click(timeout=5000))[-1]),
        ("click force", lambda: target.click(force=True, timeout=5000)),
        ("dispatch", lambda: target.dispatch_event("click")),
        ("focus+space", lambda: (target.focus(), page.keyboard.press("Space"))),
    ]
    after = before
    for name, fn in attempts:
        try:
            fn()
        except Exception as exc:
            print(f"  toggle attempt '{name}' raised: {exc}", flush=True)
        time.sleep(1.5)
        dlg, title = _dialog_open(page)      # 点完立刻查误触弹窗
        if dlg is not None:
            print(f"  after '{name}': 触发弹窗 {title!r}(本次点法误触) → 取消", flush=True)
            _dismiss_stray_confirm(page, f"-{name}")
        after = _checked()
        print(f"  after '{name}' aria-checked={after}", flush=True)
        if after == "true":
            return after
    time.sleep(2)
    _dismiss_stray_confirm(page, "-post")
    return _checked()


def open_security_settings(page, tag):
    page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=GOTO_MS)
    time.sleep(7)
    shot(page, f"04-security{tag}")
    dump_page_text(page, f"04-security{tag}")
    try:
        page.evaluate(
            """() => {
                const nodes = [...document.querySelectorAll('*')].filter(el => {
                    const s = getComputedStyle(el);
                    return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 20;
                });
                nodes.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                if (nodes[0]) nodes[0].scrollTop = nodes[0].scrollHeight;
            }"""
        )
        time.sleep(2)
        shot(page, f"04b-security-bottom{tag}")
        dump_page_text(page, f"04b-security-bottom{tag}")
    except Exception as exc:
        print(f"  security scroll probe failed: {exc}", flush=True)


def enable_toggle(page):
    """两轮：每轮都重新打开面板 → 等开关数稳定 → 重新定位 → 点。

    第二轮存在的意义：半渲染 DOM 上的句柄会被 React 替换成新节点，旧句柄点了不翻。
    重新 goto + 重新枚举拿到的是新节点，本机实测（acct-132/134）第二次即 ENABLED。
    以前没有第二轮，只能靠人肉重跑整个 Job（登录 + TOTP 全部重来 ~2min）。
    """
    print("[2] open security settings", flush=True)
    after = None
    for round_no in (1, 2):
        tag = "" if round_no == 1 else f"-r{round_no}"
        if round_no > 1:
            print(f"  === toggle 第 {round_no} 轮：重开面板 + 重新定位（上一轮句柄疑似失效）", flush=True)
        open_security_settings(page, tag)
        wait_switches_settled(page)
        target, _idx = find_codex_switch(page)
        if target is None:
            if round_no == 1:
                continue
            print("RESULT=TOGGLE_NOT_FOUND", flush=True)
            sys.exit(30)
        after = flip_switch(page, target)
        print(f"  final aria-checked={after} (round {round_no})", flush=True)
        shot(page, f"05-after-toggle{tag}")
        if after == "true":
            print("RESULT=ENABLED", flush=True)
            return

    print("RESULT=ENABLE_FAILED", flush=True)
    sys.exit(31)


def click_settings_close_if_visible(page):
    for sel in ["button[aria-label='Close']", "button:has-text('关闭')", "button:has-text('Close')"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=500):
                loc.first.click()
                time.sleep(2)
                return True
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        time.sleep(1)
    except Exception:
        pass
    return False


def disable_mfa(page):
    print("[2] disable accidentally enabled MFA switch if present", flush=True)
    page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=GOTO_MS)
    time.sleep(7)
    shot(page, "04-security-before-mfa-disable")
    switches = page.locator("button[role='switch']")
    target = None
    for idx in range(switches.count()):
        sw = switches.nth(idx)
        try:
            label = sw.evaluate(
                """el => {
                    const parts = [];
                    let p = el;
                    for (let i = 0; i < 5; i++) {
                        if (!p) break;
                        const text = (p.innerText || '').trim();
                        if (text) parts.push(text);
                        p = p.parentElement;
                    }
                    return parts.join('\\n---parent---\\n');
                }"""
            )
            compact = " ".join(label.split())
            print(f"  switch {idx}: aria={sw.get_attribute('aria-checked')} label={compact[:180]!r}", flush=True)
            if re.search(r"authenticator app|验证器应用|身份验证", label, re.I):
                target = sw
                break
        except Exception as exc:
            print(f"  switch {idx} inspect failed: {exc}", flush=True)
    if target is None:
        print("RESULT=MFA_SWITCH_NOT_FOUND", flush=True)
        return
    before = target.get_attribute("aria-checked")
    print(f"  mfa before aria-checked={before}", flush=True)
    if before == "true":
        target.click(force=True)
        time.sleep(5)
        for pattern in (r"turn off|disable|confirm|continue|关闭|停用|确认|继续"):
            try:
                btn = page.locator("button").filter(has_text=re.compile(pattern, re.I)).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    time.sleep(4)
                    break
            except Exception:
                pass
    after = target.get_attribute("aria-checked")
    print(f"  mfa after aria-checked={after}", flush=True)
    shot(page, "05-after-mfa-disable")
    print("RESULT=MFA_DISABLED" if after != "true" else "RESULT=MFA_STILL_ENABLED", flush=True)


def probe_codex(page):
    print("[2] probe Codex/settings surfaces", flush=True)
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=GOTO_MS)
    time.sleep(6)
    shot(page, "04-app-home")
    dump_page_text(page, "04-app-home")
    for sel in [
        "a:has-text('Codex')",
        "button:has-text('Codex')",
        "[aria-label*='Codex']",
        "[data-testid*='codex' i]",
    ]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=1500):
                print(f"  clicking Codex entry via {sel}", flush=True)
                loc.first.click()
                time.sleep(8)
                shot(page, "05-codex-entry")
                dump_page_text(page, "05-codex-entry")
                break
        except Exception as exc:
            print(f"  codex entry selector failed {sel}: {exc}", flush=True)
    page.goto("https://chatgpt.com/#settings/Apps", wait_until="domcontentloaded", timeout=GOTO_MS)
    time.sleep(5)
    shot(page, "06-settings-apps")
    dump_page_text(page, "06-settings-apps")
    page.goto("https://chatgpt.com/#settings/Account", wait_until="domcontentloaded", timeout=GOTO_MS)
    time.sleep(5)
    shot(page, "07-settings-account")
    dump_page_text(page, "07-settings-account")
    print("RESULT=PROBED", flush=True)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"], **({"proxy": {"server": os.environ["OAUTH_PROXY"]}} if os.environ.get("OAUTH_PROXY") else {}))
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        try:
            login(page, pw)
            if ACTION == "disable-mfa":
                disable_mfa(page)
            elif ACTION == "probe":
                probe_codex(page)
            elif ACTION == "enable-codex-toggle":
                enable_toggle(page)
            else:
                raise RuntimeError(f"unknown ACTION={ACTION}")
        except PlaywrightTimeoutError as exc:
            shot(page, "99-timeout")
            dump_page_text(page, "99-timeout")
            print(f"RESULT=TIMEOUT detail={exc}", flush=True)
            sys.exit(40)
        except Exception as exc:
            shot(page, "99-error")
            dump_page_text(page, "99-error")
            print(f"RESULT=ERROR detail={exc}", flush=True)
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
