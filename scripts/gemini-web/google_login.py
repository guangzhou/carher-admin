"""Log a Google account into gemini.google.com with password + TOTP and dump the web cookies.

Runs inside mcr.microsoft.com/playwright/python on 198. Reads /acct/<name>.json
({"email","password","totp_secret"}), writes /out/<name>.cookies.json with
__Secure-1PSID / __Secure-1PSIDTS, plus screenshots on every step for debugging.
Never prints the password, TOTP secret or cookie values.
"""
import base64
import hashlib
import hmac
import json
import struct
import sys
import time
from urllib.parse import urlparse

from patchright.sync_api import TimeoutError as PWTimeout
from patchright.sync_api import sync_playwright

NAME = sys.argv[1] if __name__ == "__main__" else None
ACCT = json.load(open(f"/acct/{NAME}.json")) if NAME else None
OUT = f"/out/{NAME}"
step_no = 0


def totp(secret: str) -> str:
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(time.time()) // 30)
    h = hmac.new(key, counter, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def shot(page, label):
    global step_no
    step_no += 1
    page.screenshot(path=f"{OUT}.{step_no:02d}-{label}.png")
    print(f"[{step_no:02d}] {label}: {page.url[:120]} :: {visible_text(page, 160)}", flush=True)


def visible_text(page, limit=400):
    try:
        return page.inner_text("body")[:limit].replace("\n", " | ")
    except Exception:
        return ""


def main():
    with sync_playwright() as p:
        # Same setup as the 188 ChatGPT toggle launcher: patchright, headed under Xvfb, no UA override.
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 860})
        page = ctx.new_page()
        page.goto("https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp&hl=en",
                  wait_until="domcontentloaded")
        shot(page, "start")
        email_box = page.locator('#identifierId, input[type="email"], input[name="identifier"]').first
        try:
            email_box.wait_for(state="visible", timeout=45000)
        except PWTimeout:
            print("STOP no email field:", visible_text(page, 600), flush=True)
            return 1
        email_box.fill(ACCT["email"])
        page.keyboard.press("Enter")
        try:
            page.wait_for_selector('input[type="password"]:visible', timeout=25000)
        except PWTimeout:
            shot(page, "no-password-field")
            print("STOP after email:", visible_text(page), flush=True)
            return 2
        shot(page, "password-page")
        page.fill('input[type="password"]:visible', ACCT["password"])
        page.keyboard.press("Enter")

        deadline = time.time() + 120
        totp_done = False
        while time.time() < deadline:
            page.wait_for_timeout(2500)
            url = page.url
            if url.startswith("https://gemini.google.com/"):
                break
            host = urlparse(url).netloc
            if host == "myaccount.google.com" or "speedbump" in url or "gds.google.com" in url:
                # Optional post-login prompts (selfie, recovery info, passkey...): skip, never enroll anything.
                shot(page, "optional-prompt")
                for label in ("Not now", "Skip", "Remind me later", "Cancel", "Done", "I understand"):
                    btn = page.get_by_role("button", name=label)
                    if btn.count():
                        btn.first.click()
                        break
                else:
                    link = page.get_by_text("Not now", exact=True)
                    if link.count():
                        link.first.click()
                    else:
                        print("STOP unknown optional prompt:", visible_text(page), flush=True)
                        return 4
                continue
            if "challenge/totp" in url or page.locator('input[name="totpPin"]').count():
                if totp_done:
                    shot(page, "totp-rejected")
                    print("STOP totp rejected:", visible_text(page), flush=True)
                    return 3
                # Avoid the last seconds of a 30s window so the code is still valid on arrival.
                if 30 - int(time.time()) % 30 < 4:
                    page.wait_for_timeout(5000)
                box = page.locator('input[name="totpPin"], input[type="tel"]').first
                box.fill(totp(ACCT["totp_secret"]))
                shot(page, "totp-filled")
                page.keyboard.press("Enter")
                totp_done = True
                continue
            if "challenge/selection" in url or "challenge/pwd" not in url and "selection" in url:
                opt = page.locator('[data-challengetype="6"], text=/Google Authenticator|Authenticator app/i').first
                if opt.count():
                    shot(page, "challenge-selection")
                    opt.click()
                    continue
            if "speedbump" in url or "gds.google.com" in url or "signin/v2/challenge/iap" in url:
                shot(page, "interstitial")
                for label in ("Not now", "I understand", "Skip", "Continue", "Confirm", "Done"):
                    btn = page.get_by_role("button", name=label)
                    if btn.count():
                        btn.first.click()
                        break
                else:
                    print("STOP unknown interstitial:", visible_text(page), flush=True)
                    return 4
                continue
            if "challenge" in url or "rejected" in url or "deniedsigninrejected" in url:
                shot(page, "unexpected-challenge")
                print("STOP unexpected challenge:", visible_text(page), flush=True)
                return 5
        else:
            shot(page, "timeout")
            print("STOP timeout:", visible_text(page), flush=True)
            return 6

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(6000)
        shot(page, "gemini")
        cookies = {c["name"]: c["value"] for c in ctx.cookies("https://gemini.google.com")
                   if c["name"] in ("__Secure-1PSID", "__Secure-1PSIDTS")}
        if len(cookies) != 2:
            print("STOP cookies missing:", sorted(cookies), flush=True)
            return 7
        json.dump({"email": ACCT["email"], "secure_1psid": cookies["__Secure-1PSID"],
                   "secure_1psidts": cookies["__Secure-1PSIDTS"], "ts": int(time.time())},
                  open(f"{OUT}.cookies.json", "w"))
        print("OK cookies saved (values not printed)", flush=True)
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
