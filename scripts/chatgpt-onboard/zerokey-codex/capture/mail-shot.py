#!/usr/bin/env python3
"""mail-shot.py — log into mail.com, open the newest ChatGPT login-code email,
and save a viewport screenshot of it so a human/agent can read the 6-digit code
when the brittle innerText extractor fails (the inbox is a cross-origin iframe).

Heavily instrumented: saves a screenshot at every step into SCREENSHOT_DIR.

ENV:
  MAIL_USER, MAIL_LOGIN_PW_FILE   mail.com creds
  PROFILE_DIR                     browser profile (default /work/mailprofile)
  SCREENSHOT_DIR                  step screenshots (default /work/screenshots)
  SHOT_OUT                        opened-mail screenshot (default /work/out/mailshot.png)
"""
import os, re, time
from patchright.sync_api import sync_playwright

EMAIL = os.environ["MAIL_USER"]
MAIL_PW = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
PROFILE = os.environ.get("PROFILE_DIR", "/work/mailprofile")
SS_DIR = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
SHOT_OUT = os.environ.get("SHOT_OUT", "/work/out/mailshot.png")
os.makedirs(SS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(SHOT_OUT), exist_ok=True)


def ss(p, name):
    try:
        p.screenshot(path=f"{SS_DIR}/ms-{name}.png", full_page=False)
        print(f"  shot ms-{name} url={p.url[:80]}", flush=True)
    except Exception as e:
        print(f"  shot {name} fail: {e}", flush=True)


def dismiss_consent(p):
    """mail.com shows a GDPR consent overlay (often in an iframe) that blocks input."""
    labels = ["Accept All", "Accept all", "Agree", "I agree", "Accept", "Got it",
              "Akzeptieren", "Alle akzeptieren", "Continue", "OK"]
    for _ in range(3):
        hit = False
        for fr in p.frames:
            for lab in labels:
                try:
                    loc = fr.get_by_role("button", name=re.compile(lab, re.I))
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click(timeout=3000)
                        print(f"  consent: clicked '{lab}'", flush=True)
                        p.wait_for_timeout(1500)
                        hit = True
                        break
                except Exception:
                    continue
            if hit:
                break
        if not hit:
            break


def main():
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE, headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        p = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            p.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=45000)
            p.wait_for_timeout(2500)
            ss(p, "01-home")
            dismiss_consent(p)
            ss(p, "02-postconsent")
            try:
                p.locator("a:has-text('Log in')").first.click(timeout=8000)
            except Exception as e:
                print(f"  login-link err: {e}", flush=True)
            p.wait_for_timeout(2000)
            ss(p, "03-loginform")
            try:
                p.locator("input[placeholder='Email address']").first.fill(EMAIL, timeout=15000)
                p.locator("input[placeholder='Password']").first.fill(MAIL_PW, timeout=15000)
            except Exception as e:
                print(f"  fill err: {e}", flush=True)
            ss(p, "04-filled")
            # submit by pressing Enter in the password field — clicking the
            # ambiguous 'Log in' buttons navigates to the support page instead.
            try:
                p.locator("input[placeholder='Password']").first.press("Enter")
                print("  submitted via Enter", flush=True)
            except Exception as e:
                print(f"  enter submit err: {e}", flush=True)
            for _ in range(30):
                if "navigator" in p.url:
                    break
                time.sleep(1)
            p.wait_for_timeout(3000)
            ss(p, "05-postlogin")
            for sel in ["a:has-text('Continue to Account')", "button:has-text('Continue to Account')",
                        "button:has-text('No, thanks')", "button:has-text('Maybe later')", "button:has-text('Skip')"]:
                try:
                    loc = p.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click(timeout=3000)
                        p.wait_for_timeout(1500)
                except Exception:
                    pass
            p.wait_for_timeout(4000)
            ss(p, "06-inbox")
            # open newest code email via cross-origin-safe frame_locator
            opened = False
            for fsel in ["iframe[name='mail']", "iframe[name='mail.com']", "iframe[src*='mail']", "iframe"]:
                try:
                    fl = p.frame_locator(fsel)
                    for needle in ["temporary ChatGPT login code", "ChatGPT login code", "login code"]:
                        loc = fl.get_by_text(needle, exact=False)
                        if loc.count() > 0:
                            loc.first.click(timeout=8000)
                            p.wait_for_timeout(4000)
                            opened = True
                            print(f"  opened code email via {fsel} :: {needle}", flush=True)
                            break
                except Exception as e:
                    print(f"  fl {fsel} err: {str(e)[:80]}", flush=True)
                if opened:
                    break
            p.wait_for_timeout(2000)
            ss(p, "07-opened")
            try:
                p.screenshot(path=SHOT_OUT, full_page=False)
                print(f"  SHOT_OUT saved: {SHOT_OUT}", flush=True)
            except Exception as e:
                print(f"  SHOT_OUT fail: {e}", flush=True)
            print("DONE", flush=True)
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
