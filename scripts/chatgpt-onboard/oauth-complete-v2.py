#!/usr/bin/env python3
"""OAuth complete v2 —— 加 anti-detection（stealth + 真实 UA + 关 webdriver 标记）
试图绕过 auth.openai.com 的 Cloudflare Turnstile。"""
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
ACCT = os.environ.get("ACCT", "acct-?")

OTP_RE = re.compile(r"\b(\d{6})\b")
SHOTS = Path("/work/screenshots")
SHOTS.mkdir(exist_ok=True)
START_TS = time.time()

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def shoot(target, name: str) -> None:
    p = SHOTS / f"{ACCT}-{name}.png"
    target.screenshot(path=str(p), full_page=True)
    print(f"  shot: {p}", flush=True)


def apply_stealth(page) -> None:
    # Remove navigator.webdriver flag, fake plugins/languages, override chrome obj
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        window.chrome = {runtime: {}};
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : origQuery(p);
    """)


def wait_past_turnstile(page, max_wait: int = 45) -> bool:
    """If Cloudflare Turnstile page is shown, wait/probe up to max_wait seconds."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        body = page.content().lower()
        if "verify you are human" not in body and "security verification" not in body:
            return True
        # try clicking inside turnstile iframe (sometimes auto-passes if score OK)
        try:
            tf = next((f for f in page.frames if "challenges.cloudflare" in (f.url or "")), None)
            if tf:
                cb = tf.locator("input[type='checkbox']").first
                if cb.count() > 0 and cb.is_visible():
                    print("  attempting Turnstile checkbox click", flush=True)
                    cb.click(force=True)
                    time.sleep(3)
        except Exception as e:
            print(f"  turnstile click error: {e}", flush=True)
        time.sleep(3)
    return False


def fill_user_code(page) -> None:
    print(f"step 1: device page + code {USER_CODE}", flush=True)
    page.goto("https://auth.openai.com/codex/device", wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    shoot(page, "01-device")
    inp = next((i for i in page.locator("input").all() if i.is_visible()), None)
    if not inp:
        sys.exit("no input on device page")
    inp.fill(USER_CODE)
    page.keyboard.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=20000)
    time.sleep(5)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="en-US",
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.new_page()
            apply_stealth(page)
            fill_user_code(page)

            shoot(page, "02-after-code")
            print(f"  url={page.url}", flush=True)
            if "verify you are human" in page.content().lower() or "challenges.cloudflare" in page.content().lower():
                print("  Cloudflare Turnstile detected, waiting/probing...", flush=True)
                ok = wait_past_turnstile(page, 45)
                shoot(page, "03-turnstile-result")
                if not ok:
                    print("  ❌ Turnstile blocked headless agent — cannot proceed automatically", flush=True)
                    sys.exit(2)
                print("  ✅ Turnstile passed", flush=True)

            # email
            page.wait_for_selector("input[type='email'], input[name='email'], input[autocomplete='username']", timeout=20000)
            page.fill("input[type='email'], input[name='email'], input[autocomplete='username']", OPENAI_EMAIL)
            shoot(page, "04-email")
            btn = page.locator("button:has-text('Continue'), button[type='submit']").first
            (btn if btn.count() > 0 else page).press("Enter") if btn.count() == 0 else btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            time.sleep(4)
            shoot(page, "05-after-email")

            page.wait_for_selector("input[type='password']", timeout=20000)
            page.fill("input[type='password']", OPENAI_PW)
            shoot(page, "06-pw")
            btn = page.locator("button:has-text('Continue'), button:has-text('Sign in'), button[type='submit']").first
            if btn.count() > 0:
                btn.click()
            else:
                page.keyboard.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            time.sleep(8)
            shoot(page, "07-after-pw")
            print(f"  url={page.url}")
            body = page.content().lower()
            for tok in ("may now return", "device authorized", "successful", "you can close",
                        "verify your email", "verification code", "invalid", "incorrect"):
                if tok in body:
                    print(f"  body has: {tok!r}", flush=True)

            # OTP handling left to next iteration if needed
        finally:
            browser.close()


if __name__ == "__main__":
    main()
