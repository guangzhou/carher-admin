"""快速只查「我们存的密码还对不对」：填邮箱 + 密码，读页面判词就退出。不碰 TOTP、不登完。

在 188 playwright 镜像里跑。读 /acct/<name>.json，每号一行结果到 stdout：
  OK pw-accepted <name> [<days>]        密码对（进到 TOTP / 下一步）
  STOP pw-changed <name> days=<n>       页面写 "Your password was changed N days ago"
  STOP pw-wrong <name>                  其它密码错
  STOP captcha <name>                   输密码前被人机拦
  STOP other <name> <页面前 120 字>
不打印密码。只读，不写任何远端状态。
"""
import json
import os
import re
import sys
import time

from patchright.sync_api import TimeoutError as PWTimeout
from patchright.sync_api import sync_playwright

ACCT_DIR = os.environ.get("GW_ACCT_DIR", "/acct")
CHANGED_RE = re.compile(r"password was changed (\d+) (day|days|hour|hours|minute|minutes) ago", re.I)
WRONG_RE = re.compile(r"Wrong password|Couldn.t sign you in|password was changed", re.I)


def text(page, n=600):
    try:
        return re.sub(r"\s+", " ", page.inner_text("body")[:n])
    except Exception:
        return ""


def check(p, name):
    acct = json.load(open(f"{ACCT_DIR}/{name}.json"))
    browser = p.chromium.launch(headless=False, args=["--no-sandbox"])
    try:
        ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 860})
        page = ctx.new_page()
        page.goto("https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp&hl=en",
                  wait_until="domcontentloaded")
        box = page.locator('#identifierId, input[type="email"], input[name="identifier"]').first
        box.wait_for(state="visible", timeout=45000)
        box.fill(acct["email"])
        page.keyboard.press("Enter")
        try:
            page.wait_for_selector('input[type="password"]:visible', timeout=25000)
        except PWTimeout:
            t = text(page)
            if "not a robot" in t.lower() or "recaptcha" in page.url.lower() or "Verify it" in t:
                return f"STOP captcha {name}"
            return f"STOP other {name} {t[:120]}"
        page.fill('input[type="password"]:visible', acct["password"])
        page.keyboard.press("Enter")
        deadline = time.time() + 45
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            t = text(page)
            url = page.url
            if m := CHANGED_RE.search(t):
                return f"STOP pw-changed {name} changed={m.group(1)}{m.group(2)[0]}"
            if WRONG_RE.search(t):
                return f"STOP pw-wrong {name} {t[:90]}"
            # 密码过了：到了 TOTP / 可选提示页 / 直接 gemini
            if page.locator('input[name="totpPin"]').count() or url.startswith("https://gemini.google.com/") \
                    or "challenge/selection" in url or "challenge/ipp" in url or "myaccount.google.com" in url \
                    or "speedbump" in url:
                return f"OK pw-accepted {name}"
            if "challenge/pwd" not in url and "signin/v2/challenge/pwd" not in url and \
                    not page.locator('input[type="password"]:visible').count():
                return f"OK pw-accepted {name} (url={url.split('?')[0][-50:]})"
        return f"STOP other {name} timeout {text(page)[:100]}"
    finally:
        try:
            browser.close()
        except Exception:
            pass


def main():
    names = sys.argv[1:]
    with sync_playwright() as p:
        for n in names:
            try:
                print(check(p, n), flush=True)
            except Exception as e:
                print(f"STOP other {n} crash {type(e).__name__}: {str(e)[:100]}", flush=True)


main()
