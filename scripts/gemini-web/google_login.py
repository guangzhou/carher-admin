"""Log a Google account into gemini.google.com with password + TOTP and dump the web cookies.

Runs on 188 inside mcr.microsoft.com/playwright/python (patchright, headed under Xvfb).
Reads /acct/<name>.json ({"email","password","totp_secret"}), writes /out/<name>.cookies.json with
__Secure-1PSID / __Secure-1PSIDTS, plus a screenshot per step. Never prints password, TOTP secret or cookies.

Every exit path prints exactly one line starting with OK or STOP (the batch runner greps for them).
Pages seen in the wild and how they are handled (2026-10-14, 23 accounts):
  * TOTP page whose URL `continue=` contains gemini.google.com  -> "done" is judged by host prefix only
  * optional prompts (Add a selfie / Protect your account access with a selfie / recovery / passkey),
    served from myaccount.google.com OR from an accounts URL still containing challenge/totp
    -> recognised by page text, "Not now"/"Skip"
  * password page still showing after Enter (slow submit) -> re-submit up to 2x; "Wrong password" -> STOP
  * navigation while reading the page ("Execution context was destroyed") -> treated as transient
"""
import base64
import hashlib
import hmac
import json
import re
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
OPTIONAL_PROMPT_RE = re.compile(
    r"Add a selfie|with a selfie|selfie video|Add a recovery|recovery (phone|email)|Create a passkey|passkey|"
    r"Stay signed in|Make sure you can always sign in|Confirm your recovery", re.I)
SKIP_LABELS = ("Not now", "Skip", "Remind me later", "Cancel", "Done", "I understand", "Continue", "Confirm")


def totp(secret: str) -> str:
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(time.time()) // 30)
    h = hmac.new(key, counter, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def visible_text(page, limit=400):
    try:
        return page.inner_text("body")[:limit].replace("\n", " | ")
    except Exception:
        return ""


def shot(page, label):
    global step_no
    step_no += 1
    try:
        page.screenshot(path=f"{OUT}.{step_no:02d}-{label}.png")
    except Exception:
        pass
    print(f"[{step_no:02d}] {label}: {page.url[:120]} :: {visible_text(page, 160)}", flush=True)


def click_first(page, labels):
    for label in labels:
        btn = page.get_by_role("button", name=label)
        if btn.count():
            try:
                btn.first.click(timeout=3000)  # default 30s: an unclickable "Not now" would stall every poll
                return label
            except Exception:
                continue
    for label in labels:
        t = page.get_by_text(label, exact=True)
        if t.count():
            try:
                t.first.click(timeout=3000)
                return label
            except Exception:
                continue
    return None


def step(page, st):
    """One pass of the post-password state machine: returns "done", an int exit code, or None to keep polling."""
    url = page.url
    if url.startswith("https://gemini.google.com/"):
        return "done"
    path = url.split("?")[0]
    host = urlparse(url).netloc
    text = visible_text(page, 600)
    has_totp_box = page.locator('input[name="totpPin"]').count() > 0
    has_pw_box = page.locator('input[type="password"]:visible').count() > 0

    if (OPTIONAL_PROMPT_RE.search(text) and not has_totp_box) or host == "myaccount.google.com" \
            or "speedbump" in url or "gds.google.com" in url:
        # Optional post-login prompts: skip, never enroll anything.
        shot(page, "optional-prompt")
        st["prompts"] += 1
        clicked = click_first(page, SKIP_LABELS)
        if not clicked and st["prompts"] < 2:
            return None
        if st["prompts"] >= 2:
            # 10-14: "Protect your account access with a selfie" sometimes ignores Not now (same page 5x),
            # while a fresh login of the same account goes straight through. The session is already
            # signed in at this point, so just leave the prompt and open Gemini directly.
            page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            if page.url.startswith("https://gemini.google.com/"):
                print("    (optional prompt bypassed by direct navigation)", flush=True)
                return "done"
        if st["prompts"] > 6:
            print("STOP optional prompt loop:", text[:300], flush=True)
            return 4
        return None

    if has_totp_box:
        if st["totp_done"] and st["totp_tries"] >= 2:
            shot(page, "totp-rejected")
            print("STOP totp rejected:", text, flush=True)
            return 3
        if 30 - int(time.time()) % 30 < 4:  # don't send a code about to expire
            page.wait_for_timeout(5000)
        page.locator('input[name="totpPin"]').first.fill(totp(ACCT["totp_secret"]))
        shot(page, "totp-filled")
        page.keyboard.press("Enter")
        st["totp_done"] = True
        st["totp_tries"] += 1
        page.wait_for_timeout(3000)
        return None

    if "/challenge/pwd" in path and has_pw_box:
        if re.search(r"Wrong password|password was changed|Couldn.t sign you in", text, re.I):
            shot(page, "wrong-password")
            print("STOP wrong password:", text[:300], flush=True)
            return 8
        st["pw_retries"] += 1
        if st["pw_retries"] <= 2:
            page.fill('input[type="password"]:visible', ACCT["password"])
            page.keyboard.press("Enter")
            return None

    if "challenge/selection" in url:
        opt = page.locator('[data-challengetype="6"]').first
        if not opt.count():
            opt = page.get_by_text(re.compile(r"Google Authenticator|Authenticator app", re.I)).first
        if opt.count():
            shot(page, "challenge-selection")
            opt.click()
            return None

    if "challenge" in url or "rejected" in url or "deniedsigninrejected" in url:
        st["unknown"] += 1
        if st["unknown"] >= 3:  # give slow pages a few polls before declaring it unknown
            shot(page, "unexpected-challenge")
            print("STOP unexpected challenge:", text, flush=True)
            return 5
    return None


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 860})
        page = ctx.new_page()
        try:
            page.goto("https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp&hl=en",
                      wait_until="domcontentloaded")
            shot(page, "start")
            email_box = page.locator('#identifierId, input[type="email"], input[name="identifier"]').first
            email_box.wait_for(state="visible", timeout=45000)
            email_box.fill(ACCT["email"])
            page.keyboard.press("Enter")
            page.wait_for_selector('input[type="password"]:visible', timeout=30000)
            shot(page, "password-page")
            page.fill('input[type="password"]:visible', ACCT["password"])
            page.keyboard.press("Enter")
        except PWTimeout:
            shot(page, "pre-password")
            print("STOP before password step:", visible_text(page, 400), flush=True)
            return 2

        st = {"totp_done": False, "totp_tries": 0, "pw_retries": 0, "prompts": 0, "unknown": 0}
        deadline = time.time() + 150
        while time.time() < deadline:
            page.wait_for_timeout(2500)
            try:
                rc = step(page, st)
            except Exception as e:  # page navigated while we were reading it
                print(f"    (transient {type(e).__name__})", flush=True)
                continue
            if rc == "done":
                break
            if isinstance(rc, int):
                return rc
        else:
            shot(page, "timeout")
            print("STOP timeout:", visible_text(page), flush=True)
            return 6

        page.wait_for_timeout(6000)
        shot(page, "gemini")
        cookies = {}
        for _ in range(4):  # 10-14 tomlinsonmurtha: on gemini.google.com with only 1PSIDTS set yet; give it time
            cookies = {c["name"]: c["value"] for c in ctx.cookies("https://gemini.google.com")
                       if c["name"] in ("__Secure-1PSID", "__Secure-1PSIDTS")}
            if len(cookies) == 2:
                break
            page.wait_for_timeout(5000)
            page.reload(wait_until="domcontentloaded")
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
    try:
        sys.exit(main())
    except Exception as e:  # guarantee the runner always sees a STOP line
        print(f"STOP crashed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        sys.exit(9)
