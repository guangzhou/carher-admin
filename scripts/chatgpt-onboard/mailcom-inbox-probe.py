#!/usr/bin/env python3
# mailcom-inbox-probe.py — mail.com-only login + inbox render probe.
#
# Purpose: safely diagnose why the onboarding scraper stalls on mail.com
# ("inbox still loading" x45, sender keyword never visible, no OTP read) WITHOUT
# touching ChatGPT — so it triggers NO real OTP send and cannot abuse-throttle
# the account (2026-08-18 acct-231/232/233 all stalled the same way).
#
# It logs into mail.com, then patiently dumps the inbox across several rounds
# (NO aggressive reload) to answer: does the message list eventually render with
# enough settle time? what do the rows look like? is a portal->inbox nav needed?
#
# Every click action waits >=PRECLICK_SEC (default 60s) first, per operator rule
# 2026-08-18 ("你点击每个动作前至少停留1分钟").
#
# Env: MAIL_USER (or CHATGPT_EMAIL), MAIL_PW_FILE (default /run/mail_pw),
#      PRECLICK_SEC (default 60), MAILCOM_HOME_SETTLE_SEC (default 120),
#      ROUNDS (default 5). Screenshots -> /work/probe.
import os
import re
import time

from patchright.sync_api import sync_playwright

EMAIL = os.environ.get("MAIL_USER") or os.environ.get("CHATGPT_EMAIL") or ""
MAIL_PASSWORD = open(os.environ.get("MAIL_PW_FILE", "/run/mail_pw")).read().strip()
OUT = "/work/probe"
os.makedirs(OUT, exist_ok=True)
PRE = int(os.environ.get("PRECLICK_SEC", "60"))
HOME = int(os.environ.get("MAILCOM_HOME_SETTLE_SEC", "120"))
ROUNDS = int(os.environ.get("ROUNDS", "5"))

SENDER_RE = re.compile(r"(openai|chatgpt|登录代码|verification|验证码|临时)", re.I)
# text that only shows once the webmail list chrome is actually present
LIST_MARKERS = ("收件箱", "Inbox", "Posteingang", "撰写", "Compose", "New email", "新邮件")


def log(m):
    print(m, flush=True)


def shot(page, name):
    try:
        page.screenshot(path=f"{OUT}/{name}.png")
    except Exception as e:
        log(f"  shot {name} err={e}")


def dump(page, tag):
    try:
        url = page.url
    except Exception:
        url = "?"
    log(f"[{tag}] url={url}")
    sender_hit = False
    list_hit = False
    for i, fr in enumerate(page.frames):
        try:
            t = fr.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            t = ""
        if SENDER_RE.search(t):
            sender_hit = True
        if any(k in t for k in LIST_MARKERS):
            list_hit = True
        # only print frames with mail-ish content (skip pure ad-bidding json)
        if any(k in t for k in LIST_MARKERS) or SENDER_RE.search(t) or "Photos & Files" in t:
            lines = [x.strip() for x in t.split("\n") if x.strip()][:30]
            log(f"  frame[{i}] name={fr.name!r} url={(fr.url or '')[:70]}")
            log("     " + " | ".join(lines)[:700])
    log(f"  SENDER_KEYWORD_PRESENT={sender_hit}  LIST_CHROME_PRESENT={list_hit}")
    shot(page, tag)


def fill_first(page, sels, val, nm):
    for s in sels:
        try:
            loc = page.locator(s).first
            if loc.count() and loc.is_visible(timeout=2000):
                loc.fill(val)
                log(f"  {nm} filled via {s}")
                return True
        except Exception:
            pass
    log(f"  {nm} NOT filled")
    return False


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.set_default_timeout(30000)

        log(f"== goto www.mail.com (email={EMAIL}) ==")
        page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(HOME)

        log(f"== wait {PRE}s before 'Log in' click ==")
        time.sleep(PRE)
        try:
            page.locator("a:has-text('Log in')").first.click(timeout=10000)
        except Exception as e:
            log(f"  login-link click err={e}")
        time.sleep(3)

        fill_first(page, ["input[placeholder='Email address']", "#login-email", "input[name='username']"], EMAIL, "email")
        fill_first(page, ["input[placeholder='Password']", "#login-password", "input[type='password']"], MAIL_PASSWORD, "pw")

        log(f"== wait {PRE}s before submit click ==")
        time.sleep(PRE)
        clicked = False
        btns = page.locator("button:has-text('Log in'), button[type='submit']")
        for i in range(btns.count()):
            b = btns.nth(i)
            try:
                if b.is_visible():
                    box = b.bounding_box()
                    if not box or box["y"] > 50:
                        b.click(timeout=5000)
                        clicked = True
                        break
            except Exception:
                pass
        log(f"  submit clicked={clicked}")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        time.sleep(15)
        dump(page, "00-post-login")

        # patience loop: wait PRE per round, dump, see WHEN the list renders. No reload.
        for r in range(1, ROUNDS + 1):
            log(f"== patience round {r}/{ROUNDS}: sleep {PRE}s (no reload) ==")
            time.sleep(PRE)
            dump(page, f"round-{r}")

        # one portal->inbox nav attempt (only if list chrome still absent), pre-waited
        log(f"== wait {PRE}s before Email-nav click attempt ==")
        time.sleep(PRE)
        for sel in ("a[href*='mailintern']", "a[href*='/mail']", "a[href$='/mail']",
                    "button:has-text('Email')", "a:has-text('Email')", "*[title='Email']"):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    loc.click(timeout=5000)
                    log(f"  clicked Email nav via {sel!r}")
                    break
            except Exception:
                pass
        try:
            page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        log(f"== wait {PRE}s after Email-nav click, then dump ==")
        time.sleep(PRE)
        dump(page, "after-email-click")

        log("== PROBE DONE ==")
        browser.close()


if __name__ == "__main__":
    main()
