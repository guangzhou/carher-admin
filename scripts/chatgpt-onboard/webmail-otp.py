#!/usr/bin/env python3
"""mail.com webmail-only 流程探针：登 webmail → 进收件箱 → 找 OpenAI 邮件
→ 取 6 位验证码。完全绕开 IMAP（mail.com 默认关）。

环境变量：
  MAIL_USER       — nattheocommingdant@mail.com
  MAIL_LOGIN_PW   — 从文件读，路径见 MAIL_LOGIN_PW_FILE（避免 shell 转义）
  MAIL_LOGIN_PW_FILE — /run/login_pw.txt （在容器里 mount 进来）
  OTP_FROM_HINT   — openai (默认)
  SINCE_TS        — 只看这个 timestamp 之后的邮件，秒数（默认 0）
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

EMAIL = os.environ["MAIL_USER"]
PW_FILE = os.environ.get("MAIL_LOGIN_PW_FILE", "/run/login_pw.txt")
LOGIN_PW = Path(PW_FILE).read_text().rstrip("\n\r")
OTP_FROM_HINT = os.environ.get("OTP_FROM_HINT", "openai").lower()
SINCE_TS = float(os.environ.get("SINCE_TS", "0"))

OTP_RE = re.compile(r"\b(\d{6})\b")
SHOTS = Path("/work/screenshots")
SHOTS.mkdir(exist_ok=True)


def shoot(target, name: str) -> None:
    p = SHOTS / f"webmail-{name}.png"
    target.screenshot(path=str(p), full_page=True)
    print(f"  shot: {p}")


def login(page) -> None:
    print(f"login: pw len={len(LOGIN_PW)}")
    page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    page.locator("a:has-text('Log in')").first.click()
    time.sleep(2)
    page.locator("input[placeholder='Email address']").first.fill(EMAIL)
    page.locator("input[placeholder='Password']").first.fill(LOGIN_PW)
    shoot(page, "01-creds-filled")

    # find the *form* submit button (skip header toggle)
    btns = page.locator("button:has-text('Log in')")
    for i in range(btns.count()):
        if btns.nth(i).is_visible():
            box = btns.nth(i).bounding_box()
            if box and box["y"] > 50:
                btns.nth(i).click()
                break
    page.wait_for_load_state("domcontentloaded", timeout=20000)
    time.sleep(8)
    shoot(page, "02-after-login")
    print(f"  url={page.url}")
    if "navigator" not in page.url:
        sys.exit("login failed — url did not move to navigator-*.mail.com")


def open_inbox_iframe(page):
    """mail.com webmail wraps inbox in <iframe>. Locate the inbox frame."""
    print("locating inbox frame")
    deadline = time.time() + 20
    while time.time() < deadline:
        for fr in page.frames:
            n = (fr.name or "") + " " + fr.url
            if "mail" in n.lower() and ("inbox" in n.lower() or "navigator" in n.lower() or "list" in n.lower()):
                print(f"  candidate frame: name={fr.name!r} url={fr.url[:120]}")
        # the data list is typically in a frame named like "list" or "inbox"
        for fr in page.frames:
            if fr.name and fr.name.lower() in ("list", "inbox", "mail-list", "navigator"):
                return fr
        time.sleep(2)
    return None


def find_openai_message(page) -> str | None:
    """In the inbox view, find a row whose sender contains OPT_FROM_HINT, click it,
    extract OTP from rendered body. Returns 6-digit code or None."""
    print("scanning inbox for sender hint:", OTP_FROM_HINT)
    body = page.content()
    # mail.com renders in main frame OR nested iframe; try main first
    if OTP_FROM_HINT in body.lower():
        print("  main page contains hint")
        loc = page.locator(f"text=/{OTP_FROM_HINT}/i").first
        if loc.count() > 0:
            loc.click()
            time.sleep(3)
            shoot(page, "03-message-open")
            content = page.content()
            m = OTP_RE.search(content)
            if m:
                return m.group(1)
    # try iframes
    for fr in page.frames:
        try:
            fbody = fr.content()
        except Exception:
            continue
        if OTP_FROM_HINT in fbody.lower():
            print(f"  frame {fr.name!r} contains hint")
            loc = fr.locator(f"text=/{OTP_FROM_HINT}/i").first
            if loc.count() > 0:
                loc.click()
                time.sleep(3)
                shoot(page, "03-message-open")
                # OTP may be in a different frame after open
                for fr2 in page.frames:
                    try:
                        fb2 = fr2.content()
                    except Exception:
                        continue
                    m = OTP_RE.search(fb2)
                    if m:
                        return m.group(1)
    return None


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="en-US")
        page = ctx.new_page()
        try:
            login(page)
            print("\nDUMP: top-level frames after login")
            for fr in page.frames:
                print(f"  frame name={fr.name!r} url={fr.url[:140]}")
            print("\nDUMP: visible page sections (first 1KB of body text)")
            try:
                txt = page.evaluate("() => document.body.innerText")
                print(txt[:1000])
            except Exception as e:
                print(f"  text dump failed: {e}")
            shoot(page, "10-dump-body")

            otp = find_openai_message(page)
            print(f"\nOTP found: {otp}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
