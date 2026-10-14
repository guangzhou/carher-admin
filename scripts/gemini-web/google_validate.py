"""Open the Antigravity VALIDATION_REQUIRED validation_url in a logged-in browser and walk through it.

Runs on 188 (patchright, Xvfb). Reads /acct/<name>.json and /acct/<name>.validation_url.
Logs every page; stops (does not guess) on phone / SMS / ID verification steps.
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
from google_login import totp, visible_text  # noqa: E402

NAME = sys.argv[1]
ACCT = json.load(open(f"{ACCT_DIR}/{NAME}.json"))
VURL = open(f"{ACCT_DIR}/{NAME}.validation_url").read().strip()
OUT = f"{OUT_DIR}/{NAME}"
step = 0
HARD_STOP = ("phone number", "Enter a phone", "text message", "Verify it's you with your phone", "ID", "selfie video")


def shot(page, label):
    global step
    step += 1
    page.screenshot(path=f"{OUT}.validate-{step:02d}-{label}.png")
    print(f"[{step:02d}] {label}: {page.url[:110]} :: {visible_text(page, 220)}", flush=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 860})
        page = ctx.new_page()
        page.goto(VURL, wait_until="domcontentloaded")
        shot(page, "start")
        totp_done = False
        last_url, same = "", 0
        deadline = time.time() + 180
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            url = page.url
            same = same + 1 if url == last_url else 0
            last_url = url
            text = visible_text(page, 800)
            if "identifier" in url and page.locator('#identifierId, input[type="email"]:visible').count():
                page.locator('#identifierId, input[type="email"]:visible').first.fill(ACCT["email"])
                page.keyboard.press("Enter"); shot(page, "email"); continue
            if page.locator('input[type="password"]:visible').count():
                page.locator('input[type="password"]:visible').first.fill(ACCT["password"])
                page.keyboard.press("Enter"); shot(page, "password"); continue
            if page.locator('input[name="totpPin"]').count():
                if totp_done:
                    shot(page, "totp-again"); print("STOP totp loop", flush=True); return 3
                if 30 - int(time.time()) % 30 < 4:
                    page.wait_for_timeout(5000)
                page.locator('input[name="totpPin"]').first.fill(totp(ACCT["totp_secret"]))
                page.keyboard.press("Enter"); totp_done = True; shot(page, "totp"); continue
            if any(h.lower() in text.lower() for h in ("phone number", "text message", "verification code to")):
                shot(page, "needs-phone"); print("STOP needs phone/SMS", flush=True); return 4
            clicked = False
            for label in ("Continue", "Next", "Verify", "Sign in", "Confirm", "Done", "I agree", "Accept"):
                btn = page.get_by_role("button", name=label)
                if btn.count() and btn.first.is_enabled():
                    shot(page, "click-" + label.lower().replace(" ", "-"))
                    btn.first.click(); clicked = True; break
            if clicked:
                continue
            if same >= 3:
                shot(page, "settled")
                print("SETTLED:", text[:500], flush=True)
                return 0
        shot(page, "timeout")
        return 6


sys.exit(main())
