#!/usr/bin/env python3
"""mail.com 账号 probe — 登录 webmail，截图，探查 IMAP 设置入口。

仅做诊断，不发邮件、不改设置。截图落到 /work/screenshots/。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

EMAIL = os.environ["MAIL_USER"]
LOGIN_PW = os.environ["MAIL_LOGIN_PW"]
SHOTS = Path("/work/screenshots")
SHOTS.mkdir(exist_ok=True)


def shoot(page, name: str) -> None:
    p = SHOTS / f"mail-{name}.png"
    page.screenshot(path=str(p), full_page=True)
    print(f"  shot: {p}")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="en-US")
        page = ctx.new_page()
        try:
            print("step 1: open www.mail.com")
            page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            shoot(page, "01-landing")
            print(f"  url={page.url}  title={page.title()}")

            print("step 2: click top-right 'Log in' to reveal inline form")
            for sel in ["a:has-text('Log in')", "button:has-text('Log in')",
                        "[aria-label*='Log in' i]", "text=Log in"]:
                if page.locator(sel).count() > 0:
                    print(f"  click selector: {sel}")
                    page.locator(sel).first.click()
                    break
            time.sleep(2)
            shoot(page, "02-form-expanded")

            print("step 3: dump visible inputs")
            for inp in page.query_selector_all("input"):
                visible = inp.is_visible()
                if not visible:
                    continue
                attrs = inp.evaluate("el => ({type: el.type, name: el.name, id: el.id, placeholder: el.placeholder, autocomplete: el.autocomplete})")
                print(f"  input(visible): {attrs}")

            print("step 4: fill email + login pw + submit")
            # mail.com inline form: 'Email address' placeholder, 'Password' placeholder
            email_filled = False
            for sel in ["input[placeholder='Email address']", "input[type='email']",
                        "input[name='username']", "input[autocomplete='username']"]:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    print(f"  email field selector: {sel}")
                    loc.first.fill(EMAIL)
                    email_filled = True
                    break
            pw_filled = False
            for sel in ["input[placeholder='Password']", "input[type='password']"]:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    print(f"  password field selector: {sel}")
                    loc.first.fill(LOGIN_PW)
                    pw_filled = True
                    break
            if not (email_filled and pw_filled):
                print(f"  ABORT: email_filled={email_filled} pw_filled={pw_filled}")
                shoot(page, "03-fill-failed")
                return
            shoot(page, "03-creds-filled")

            # Submit via 'Log in' button (Enter on inline form sometimes triggers ad)
            for sel in ["button:has-text('Log in')", "input[type='submit']",
                        "button[type='submit']"]:
                loc = page.locator(sel)
                # Pick the visible one inside the inline form (not the toggle in header)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        rect = loc.nth(i).bounding_box()
                        if rect and rect["y"] > 50:  # skip the header toggle (y small)
                            print(f"  click submit: {sel}#{i} at y={rect['y']}")
                            loc.nth(i).click()
                            break
                else:
                    continue
                break
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception as e:
                print(f"  wait_for_load_state: {e}")
            time.sleep(5)

            shoot(page, "04-after-login")
            print(f"  url after login={page.url}  title={page.title()}")

            print("step 5: navigate to Settings → POP3/IMAP")
            try:
                # Inline frames are common on mail.com webmail; collect candidates
                for frame in page.frames:
                    print(f"  frame: name={frame.name!r} url={frame.url[:120]}")
                # Try direct URL hit (mail.com webmail has stable settings URL)
                page.goto("https://navigator-lxa.mail.com/settings/", wait_until="domcontentloaded", timeout=15000)
                time.sleep(3)
                shoot(page, "05-settings")
                print(f"  url={page.url}")
                body = page.content()
                for kw in ("POP3", "IMAP", "External", "Mailbox access", "External access",
                           "Mail collector", "App passwords"):
                    if kw.lower() in body.lower():
                        print(f"  settings contains: {kw}")
            except Exception as e:
                print(f"  settings nav error: {e}")
                shoot(page, "05-settings-error")

            body = page.content().lower()
            for kw in ("inbox", "compose", "captcha", "verify", "incorrect", "wrong", "invalid",
                       "settings", "imap", "pop"):
                if kw in body:
                    print(f"  body contains: {kw!r}")

            print("step 3: search for settings / IMAP entry")
            for sel in ["text=Settings", "a[href*='settings']", "a[href*='imap']",
                        "[aria-label*='Settings' i]", "text=IMAP"]:
                try:
                    el = page.locator(sel).first
                    if el and el.count() > 0:
                        print(f"  found: {sel}")
                except Exception:
                    pass

        finally:
            time.sleep(1)
            browser.close()


if __name__ == "__main__":
    main()
