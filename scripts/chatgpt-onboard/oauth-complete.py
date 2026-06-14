#!/usr/bin/env python3
"""完整 OAuth 自动化：传入 user_code，playwright 完成浏览器侧授权。

流程：
1. open https://auth.openai.com/codex/device
2. 填 user_code
3. 跳转登录页 → 填 email
4. 填 password
5. 若弹 OTP 页 → 切 mail.com webmail tab → 抽最新 OpenAI 邮件 OTP → 填回
6. 等 'You may now return to your terminal' 出现
完事 litellm 容器侧自己 poll 拿 access_token、写 auth.json。

输入：
  USER_CODE          OAuth device code（如 7O0J-7OYG7）
  OPENAI_EMAIL_FILE  /run/openai_email.txt
  OPENAI_PW_FILE     /run/openai_pw.txt
  MAIL_USER          webmail 账号
  MAIL_LOGIN_PW_FILE webmail 登录密码文件
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

USER_CODE = os.environ["USER_CODE"]
OPENAI_EMAIL = Path(os.environ["OPENAI_EMAIL_FILE"]).read_text().strip()
OPENAI_PW = Path(os.environ["OPENAI_PW_FILE"]).read_text().rstrip("\n\r")
MAIL_USER = os.environ["MAIL_USER"]
MAIL_LOGIN_PW = Path(os.environ["MAIL_LOGIN_PW_FILE"]).read_text().rstrip("\n\r")

OTP_RE = re.compile(r"\b(\d{6})\b")
SHOTS = Path("/work/screenshots")
SHOTS.mkdir(exist_ok=True)
START_TS = time.time()


def shoot(target, name: str) -> None:
    p = SHOTS / f"oauth-{name}.png"
    target.screenshot(path=str(p), full_page=True)
    print(f"  shot: {p}", flush=True)


def fill_user_code(page) -> None:
    print(f"step 1: open device page + fill code {USER_CODE}", flush=True)
    page.goto("https://auth.openai.com/codex/device", wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    shoot(page, "01-device-page")
    inputs = page.locator("input").all()
    print(f"  device page inputs: {len(inputs)}")
    for inp in inputs:
        if inp.is_visible():
            inp.fill(USER_CODE)
            break
    shoot(page, "02-code-filled")
    page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    time.sleep(3)


def fill_email_password(page) -> None:
    print("step 2: email + password", flush=True)
    shoot(page, "03-after-code")
    print(f"  url={page.url}")

    # email page
    sel = "input[type='email'], input[name='email'], input[autocomplete='username']"
    page.wait_for_selector(sel, timeout=15000)
    page.fill(sel, OPENAI_EMAIL)
    shoot(page, "04-email-filled")
    btn = page.locator("button:has-text('Continue'), button[type='submit']").first
    if btn.count() > 0:
        btn.click()
    else:
        page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    time.sleep(4)

    # password page
    shoot(page, "05-after-email")
    print(f"  url={page.url}")
    page.wait_for_selector("input[type='password']", timeout=15000)
    page.fill("input[type='password']", OPENAI_PW)
    shoot(page, "06-pw-filled")
    btn = page.locator("button:has-text('Continue'), button:has-text('Sign in'), button[type='submit']").first
    if btn.count() > 0:
        btn.click()
    else:
        page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=20000)
    time.sleep(5)


def maybe_otp(page, ctx_browser) -> None:
    """If OTP screen appears, fetch latest OpenAI mail OTP via webmail tab."""
    shoot(page, "07-after-pw")
    print(f"  url={page.url}", flush=True)
    body = page.content().lower()
    needs_otp = any(t in body for t in ("verify", "verification", "code", "one-time", "check your email"))
    if not needs_otp:
        print("  no OTP screen detected")
        return
    print("step 3: OTP screen detected, opening mail.com tab", flush=True)

    mail_page = ctx_browser.new_page()
    mail_page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    mail_page.locator("a:has-text('Log in')").first.click()
    time.sleep(2)
    mail_page.locator("input[placeholder='Email address']").first.fill(MAIL_USER)
    mail_page.locator("input[placeholder='Password']").first.fill(MAIL_LOGIN_PW)
    btns = mail_page.locator("button:has-text('Log in')")
    for i in range(btns.count()):
        if btns.nth(i).is_visible():
            box = btns.nth(i).bounding_box()
            if box and box["y"] > 50:
                btns.nth(i).click()
                break
    mail_page.wait_for_load_state("domcontentloaded", timeout=20000)
    time.sleep(10)
    shoot(mail_page, "08-webmail-inbox")

    # extract OTP from inbox: poll up to 120s for new mail since START_TS
    otp = None
    for poll in range(24):  # 24 * 5s = 120s
        mail_frame = None
        for fr in mail_page.frames:
            if fr.name == "mail":
                mail_frame = fr
                break
        if mail_frame is None:
            time.sleep(5)
            continue
        try:
            text = mail_frame.evaluate("() => document.body.innerText")
        except Exception:
            time.sleep(5)
            continue
        # candidates: 6-digit codes near 'code' / 'OpenAI' / '代码'
        for m in OTP_RE.finditer(text):
            d = m.group(1)
            ctx = text[max(0, m.start() - 80): m.end() + 80].lower()
            if any(k in ctx for k in ("code", "openai", "verify", "代码", "登录代码", "verification")):
                otp = d
                print(f"  OTP candidate {d} ctx={ctx[:120]!r}")
                break
        if otp:
            break
        # try refresh
        print(f"  poll {poll+1}/24 — no OTP yet, refreshing", flush=True)
        try:
            mail_frame.evaluate("() => location.reload()")
        except Exception:
            mail_page.reload()
        time.sleep(5)

    if not otp:
        shoot(mail_page, "09-no-otp")
        sys.exit("ERROR: no OTP within 120s")
    print(f"  ✅ OTP: {otp}", flush=True)

    # fill OTP back into OAuth tab
    page.bring_to_front()
    inputs = [i for i in page.locator("input").all() if i.is_visible()]
    print(f"  OAuth tab visible inputs: {len(inputs)}")
    if len(inputs) >= 6:
        for i, ch in enumerate(otp):
            inputs[i].fill(ch)
    elif inputs:
        inputs[0].fill(otp)
    shoot(page, "10-otp-filled")
    page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    time.sleep(5)


def wait_completion(page) -> None:
    print("step 4: waiting for OAuth completion screen", flush=True)
    deadline = time.time() + 90
    while time.time() < deadline:
        body = page.content().lower()
        if any(t in body for t in ("may now return", "device authorized", "you can close",
                                    "you've signed in", "successful")):
            shoot(page, "11-success")
            print("  ✅ OAuth complete — litellm container will write auth.json", flush=True)
            return
        time.sleep(3)
    shoot(page, "11-timeout")
    sys.exit("ERROR: completion screen not seen within 90s")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(locale="en-US")
            page = ctx.new_page()
            fill_user_code(page)
            fill_email_password(page)
            maybe_otp(page, ctx)
            wait_completion(page)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
