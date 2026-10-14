"""Automate the Antigravity OAuth consent click for cli-proxy-api: login (password + TOTP), open the
auth URL, click through consent, capture the http://localhost:54545/callback?code=... URL.

Runs on 188 inside mcr.microsoft.com/playwright/python with patchright, same as google_login.py.
Reads /acct/<name>.json and /acct/<name>.authurl; writes /out/<name>.callback (the full callback URL).
Never prints password, TOTP secret, or the code.
"""
import json
import os
import sys
import time

from patchright.sync_api import sync_playwright

ACCT_DIR = os.environ.get("GW_ACCT_DIR", "/acct")
OUT_DIR = os.environ.get("GW_OUT_DIR", "/out")
WORK_DIR = os.environ.get("GW_WORK_DIR", "/work")
sys.path.insert(0, WORK_DIR)
from google_login import totp, visible_text  # noqa: E402  (reuse helpers)

NAME = sys.argv[1]
ACCT = json.load(open(f"{ACCT_DIR}/{NAME}.json"))
AUTH_URL = open(f"{ACCT_DIR}/{NAME}.authurl").read().strip()
OUT = f"{OUT_DIR}/{NAME}"
step = 0


def shot(page, label):
    global step
    step += 1
    page.screenshot(path=f"{OUT}.oauth-{step:02d}-{label}.png")
    print(f"[{step:02d}] {label}: {page.url[:110]} :: {visible_text(page, 150)}", flush=True)


def main():
    callback = {"url": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 860})

        def on_request(req):
            if req.url.startswith("http://localhost:54545/callback"):
                callback["url"] = req.url
        ctx.on("request", on_request)
        page = ctx.new_page()
        page.goto(AUTH_URL, wait_until="domcontentloaded")
        shot(page, "start")

        totp_done = False
        st = {}
        deadline = time.time() + 180
        while time.time() < deadline and not callback["url"]:
            page.wait_for_timeout(2500)
            url = page.url
            if callback["url"] or url.startswith("http://localhost:54545"):
                callback["url"] = callback["url"] or url
                break
            email_box = page.locator('#identifierId, input[type="email"]:visible')
            if "signin/identifier" in url and email_box.count():
                email_box.first.fill(ACCT["email"]); page.keyboard.press("Enter"); shot(page, "email"); continue
            pw = page.locator('input[type="password"]:visible')
            if "challenge/pwd" in url and pw.count():
                # also covers a slow submit that leaves us on the password page (10-14 rosenbergereric115)
                pw_tries = st.get("pw", 0) + 1; st["pw"] = pw_tries
                if pw_tries > 3:
                    shot(page, "password-stuck"); print("STOP password page stuck:", visible_text(page, 300), flush=True); return 8
                pw.first.fill(ACCT["password"]); page.keyboard.press("Enter"); shot(page, "password"); page.wait_for_timeout(3000); continue
            if "/challenge/totp" in url.split("?")[0] and page.locator('input[name="totpPin"]').count():
                if totp_done:
                    shot(page, "totp-rejected"); return 3
                if 30 - int(time.time()) % 30 < 4:
                    page.wait_for_timeout(5000)
                page.locator('input[name="totpPin"], input[type="tel"]').first.fill(totp(ACCT["totp_secret"]))
                page.keyboard.press("Enter"); totp_done = True; shot(page, "totp"); continue
            # account chooser: pick our account
            chooser = page.locator(f'[data-identifier="{ACCT["email"].lower()}"], [data-email="{ACCT["email"].lower()}"]')
            if "accountchooser" in url and chooser.count():
                chooser.first.click(); shot(page, "chooser"); continue
            # consent / "Google hasn't verified this app" / "Sign in to ..." screens
            clicked = False
            labels = ("Continue", "Allow", "Select all", "Advanced")
            if "/signin/oauth/" in url:  # firstparty nativeapp "Make sure you downloaded this app" page
                labels = ("Sign in",) + labels
            for label in labels:
                btn = page.get_by_role("button", name=label)
                if btn.count() and btn.first.is_enabled():
                    shot(page, "consent-" + label.lower().replace(" ", "-"))
                    btn.first.click(); clicked = True; break
            if clicked:
                continue
            boxes = page.locator('input[type="checkbox"]:not(:checked)')
            if boxes.count():
                for i in range(boxes.count()):
                    boxes.nth(i).check()
                shot(page, "consent-checkboxes"); continue
            if "myaccount.google.com" in url:
                for label in ("Not now", "Skip", "Cancel"):
                    btn = page.get_by_role("button", name=label)
                    if btn.count():
                        btn.first.click(); break
                continue
        if not callback["url"]:
            shot(page, "timeout")
            print("STOP no callback:", visible_text(page, 500), flush=True)
            return 6
        if "code=" not in callback["url"]:
            print("STOP callback without code (error?)", callback["url"].split("?")[1][:120], flush=True)
            return 7
        open(f"{OUT}.callback", "w").write(callback["url"])
        print("OK callback captured (code not printed)", flush=True)
        browser.close()
        return 0


sys.exit(main())
