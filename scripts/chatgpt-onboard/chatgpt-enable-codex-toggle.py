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
OTP_RE = re.compile(r"\b(\d{6})\b")
SENDER_HINTS_RE = re.compile(r"openai|chatgpt|noreply", re.I)


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
    try:
        return target.evaluate("() => document.body.innerText")
    except Exception:
        return ""


def login_mailcom(page):
    page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(2)
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
    sender_re = re.compile(r"(openai|chatgpt|noreply@tm\.openai|noreply@)", re.I)
    for attempt in range(1, 46):
        frame = find_mail_frame(page)
        targets = [page] + ([frame] if frame is not None else [])
        for target in targets:
            if sender_re.search(visible_text(target)):
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
    texts = []
    for frame in page.frames:
        try:
            # Avoid stale codes in the inbox list after opening a message.
            if mail_frame is not page and frame.name == "mail":
                continue
            texts.append(frame.evaluate("() => document.body.innerText"))
        except Exception:
            pass
    try:
        texts.append(mail_frame.evaluate("() => document.body.innerText"))
    except Exception:
        pass
    for text in texts:
        if not SENDER_HINTS_RE.search(text) and "code" not in text.lower() and "验证码" not in text:
            continue
        match = OTP_RE.search(text)
        if match:
            return match.group(1)
    return None


def dump_mail_text(page, name, limit=20000):
    parts = []
    try:
        parts.append(("page", page.evaluate("() => document.body.innerText")))
    except Exception:
        pass
    for idx, frame in enumerate(page.frames):
        try:
            parts.append((f"frame:{idx}:{frame.name}", frame.evaluate("() => document.body.innerText")))
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
        text = page.evaluate("() => document.body.innerText")
        out.write_text(text[:limit], encoding="utf-8")
        print(f"  dump: {out}", flush=True)
    except Exception as exc:
        print(f"  dump failed: {exc}", flush=True)


def click_latest_otp_message(target):
    patterns = [
        r"Your temporary ChatGPT login code",
        r"临时 ChatGPT 登录代码",
        r"OpenAI.*code",
        r"ChatGPT.*code",
        r"noreply@tm\.openai\.com",
    ]
    for pattern in patterns:
        try:
            loc = target.get_by_text(re.compile(pattern, re.I)).first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=5000)
                return True
        except Exception:
            pass
    # Fallback for the new mail.com split-row layout: click around the first
    # visible OpenAI/ChatGPT sender/subject in the message list.
    for selector in ("text=ChatGPT", "text=OpenAI", "text=noreply@tm.openai.com"):
        try:
            loc = target.locator(selector).first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=5000)
                return True
        except Exception:
            pass
    try:
        handles = target.locator("text=/ChatGPT|OpenAI|noreply@tm\\.openai\\.com/i")
        for idx in range(min(handles.count(), 8)):
            loc = handles.nth(idx)
            if not loc.is_visible(timeout=500):
                continue
            box = loc.bounding_box()
            if not box:
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
    browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    page = browser.new_page()
    try:
        login_mailcom(page)
        for attempt in range(1, 25):  # ~ 3min: 24 * (reload+sleep ≈ 8s)
            if attempt > 1:
                print(f"  mail.com poll {attempt}/24 (avoiding prev_otp={prev_otp})", flush=True)
                page.reload(wait_until="domcontentloaded", timeout=30000)
                time.sleep(8)
            frame = find_mail_frame(page) or page
            try:
                text = frame.evaluate("() => document.body.innerText")
            except Exception:
                text = ""
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
            if click_latest_otp_message(frame):
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
    page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=45000)
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

    for _ in range(10):
        if page.locator("input[type='password']").count() > 0:
            break
        if "passkey" in page.url.lower() or "auth_challenge" in page.url.lower():
            click_password_fallback(page)
        time.sleep(1)

    if page.locator("input[type='password']").count() == 0:
        if page_needs_otp(page):
            otp_loop(page, pw)
        else:
            raise RuntimeError("password field did not appear")
    else:
        if not type_first_visible(page.locator("input[type='password']"), page, PASSWORD):
            raise RuntimeError("visible password field not found")
        time.sleep(1)
        shot(page, "02-password-filled")
        submit(page)
        time.sleep(8)
        print(f"  after password url={page.url[:120]}", flush=True)
        shot(page, "02c-after-password-submit")

        if page_needs_otp(page):
            otp_loop(page, pw)

    deadline = time.time() + 60
    while time.time() < deadline:
        if "chatgpt.com" in page.url and "/auth" not in page.url and "/login" not in page.url:
            print("  login ok", flush=True)
            shot(page, "03-logged-in")
            return
        if page_needs_otp(page):
            otp_loop(page, pw)
        time.sleep(2)
    raise TimeoutError(f"login did not finish; url={page.url}")


def enable_toggle(page):
    print("[2] open security settings", flush=True)
    page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=45000)
    time.sleep(7)
    shot(page, "04-security")
    dump_page_text(page, "04-security")
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
        shot(page, "04b-security-bottom")
        dump_page_text(page, "04b-security-bottom")
    except Exception as exc:
        print(f"  security scroll probe failed: {exc}", flush=True)

    switches = page.locator("button[role='switch']")
    target = None
    exact_re = re.compile(
        r"codex|device\s*code|device-code|device authorization|device auth|设备代码|设备授权|设备码",
        re.I,
    )
    reject_re = re.compile(
        r"mfa|authenticator|text message|password|passkey|security key|session|"
        r"多因素|身份验证|短信|密码|通行密钥|安全密钥|会话|受信任设备|活跃会话",
        re.I,
    )
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
            print(
                f"  switch {idx}: aria={sw.get_attribute('aria-checked')} label={compact[:220]!r}",
                flush=True,
            )
            if exact_re.search(label) and not reject_re.search(label):
                target = sw
                print(f"  matched Codex/device-code switch {idx}", flush=True)
                break
        except Exception as exc:
            print(f"  switch {idx} inspect failed: {exc}", flush=True)

    if target is None:
        print("RESULT=TOGGLE_NOT_FOUND", flush=True)
        sys.exit(30)

    before = target.get_attribute("aria-checked")
    print(f"  before aria-checked={before}", flush=True)
    after = before

    def _refresh_checked():
        try:
            return target.get_attribute("aria-checked")
        except Exception:
            return None

    if before != "true":
        attempts = [
            ("click force", lambda: target.click(force=True, timeout=5000)),
            ("scroll+click", lambda: (target.scroll_into_view_if_needed(timeout=3000), target.click(timeout=5000))[-1]),
            ("dispatch", lambda: target.dispatch_event("click")),
            ("mouse box center", lambda: (
                target.bounding_box() and page.mouse.click(
                    target.bounding_box()["x"] + target.bounding_box()["width"] / 2,
                    target.bounding_box()["y"] + target.bounding_box()["height"] / 2,
                )
            )),
            ("focus+space", lambda: (target.focus(), page.keyboard.press("Space"))),
            ("parent label click", lambda: target.evaluate(
                "el => { const p = el.closest('label,div[role=\"button\"],button'); (p||el).click(); }"
            )),
        ]
        for name, fn in attempts:
            try:
                fn()
            except Exception as exc:
                print(f"  toggle attempt '{name}' raised: {exc}", flush=True)
            time.sleep(2)
            after = _refresh_checked()
            print(f"  after '{name}' aria-checked={after}", flush=True)
            if after == "true":
                break
        else:
            time.sleep(3)
            after = _refresh_checked()

    print(f"  final aria-checked={after}", flush=True)
    shot(page, "05-after-toggle")

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
    page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=45000)
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
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
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
    page.goto("https://chatgpt.com/#settings/Apps", wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)
    shot(page, "06-settings-apps")
    dump_page_text(page, "06-settings-apps")
    page.goto("https://chatgpt.com/#settings/Account", wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)
    shot(page, "07-settings-account")
    dump_page_text(page, "07-settings-account")
    print("RESULT=PROBED", flush=True)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
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
