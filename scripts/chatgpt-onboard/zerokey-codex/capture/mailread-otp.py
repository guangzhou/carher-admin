#!/usr/bin/env python3
"""mailread-otp.py — standalone mail.com reader: fetch the newest ChatGPT
6-digit login code and print it as `ZKOTP=<code>`.

Decoupled from the capture flow so the OTP can be injected via the file
fallback (OTP_FILE) reliably, without depending on the in-capture extractor.

ENV:
  MAIL_USER, MAIL_LOGIN_PW_FILE   mail.com creds
  PROFILE_DIR                     browser profile (default /work/mailprofile)
  SCREENSHOT_DIR                  screenshots (default /work/screenshots)
  READ_MAX                        max poll attempts (default 40)
"""
import os, re, sys, time
from patchright.sync_api import sync_playwright

EMAIL   = os.environ["MAIL_USER"]
MAIL_PW = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
PROFILE = os.environ.get("PROFILE_DIR", "/work/mailprofile")
SS_DIR  = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
READ_MAX = int(os.environ.get("READ_MAX", "40"))
MODE = os.environ.get("MODE", "read")  # read | purge
OTP_RE = re.compile(r"\b(\d{6})\b")
SENDER_RE = re.compile(r"(openai|chatgpt|noreply)", re.I)
SUBJECT_RE = re.compile(r"(login code|verification|temporary)", re.I)
CODE_ROW_RE = re.compile(r"(login code|temporary)", re.I)

os.makedirs(SS_DIR, exist_ok=True)


def ss(p, name):
    try:
        p.screenshot(path=f"{SS_DIR}/{name}.png", full_page=False)
    except Exception:
        pass


def login(ctx):
    p = ctx.new_page()
    p.goto("https://www.mail.com/", wait_until="domcontentloaded")
    try:
        p.locator("a:has-text('Log in')").first.click()
    except Exception:
        pass
    p.wait_for_timeout(1500)
    p.locator("input[placeholder='Email address']").first.fill(EMAIL)
    p.locator("input[placeholder='Password']").first.fill(MAIL_PW)
    btns = p.locator("button:has-text('Log in')")
    for i in range(btns.count()):
        box = btns.nth(i).bounding_box()
        if box and box["y"] > 50:
            btns.nth(i).click()
            break
    for _ in range(30):
        if "navigator" in p.url:
            break
        time.sleep(1)
    p.wait_for_timeout(3000)
    for sel in ["a:has-text('Continue to Account')", "button:has-text('Continue to Account')",
                "button:has-text('No, thanks')", "button:has-text('Maybe later')", "button:has-text('Skip')"]:
        try:
            loc = p.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                p.wait_for_timeout(2000)
        except Exception:
            pass
    return p


def wait_inbox(p):
    for attempt in range(45):
        for fr in p.frames:
            try:
                txt = fr.evaluate("() => document.body.innerText")
            except Exception:
                continue
            if txt and len(txt) > 200 and SENDER_RE.search(txt):
                return True
        if attempt > 0 and attempt % 10 == 0:
            try:
                p.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
        time.sleep(2)
    return False


def mail_frame(p):
    for fr in p.frames:
        if fr.name == "mail":
            return fr
    return None


def read_code(p):
    """Open the newest ChatGPT login-code email and pull the 6-digit code."""
    fr = mail_frame(p)
    if not fr:
        return None
    # click the first inbox row referencing a ChatGPT login code / sender
    for needle in ["temporary ChatGPT login code", "ChatGPT login code", "login code", "ChatGPT"]:
        try:
            loc = fr.get_by_text(needle, exact=False)
            if loc.count() > 0:
                loc.first.click(timeout=5000)
                p.wait_for_timeout(4000)
                break
        except Exception:
            continue
    ss(p, "mailread-opened")
    # read every frame body, prefer text near 'code'
    texts = []
    for f in p.frames:
        try:
            texts.append(f.evaluate("() => document.body.innerText"))
        except Exception:
            pass
    for text in texts:
        if not text or "code" not in text.lower():
            continue
        for m in OTP_RE.finditer(text):
            ctx = text[max(0, m.start() - 120): m.start() + 40]
            if re.search(r"code|verif|openai|chatgpt", ctx, re.I):
                return m.group(1)
    # fallback: any 6-digit in a chatgpt/openai-bearing frame
    for text in texts:
        if text and SENDER_RE.search(text):
            m = OTP_RE.search(text)
            if m:
                return m.group(1)
    return None


def delete_open_email(p):
    """Try to delete the currently open email via toolbar / keyboard."""
    sels = [
        "button[title='Delete']", "a[title='Delete']", "[aria-label='Delete']",
        "button:has-text('Delete')", "span[title='Delete']", ".icon-trash", ".icon-delete",
    ]
    for fr in p.frames:
        for sel in sels:
            try:
                loc = fr.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    p.wait_for_timeout(1500)
                    return True
            except Exception:
                continue
    try:
        p.keyboard.press("Delete")
        p.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def purge_codes(p):
    """Open and delete every ChatGPT login-code email so only fresh ones remain later."""
    deleted = 0
    for rnd in range(12):
        fr = mail_frame(p)
        if not fr:
            break
        opened = False
        for needle in ["temporary ChatGPT login code", "ChatGPT login code", "login code"]:
            try:
                loc = fr.get_by_text(needle, exact=False)
                if loc.count() > 0:
                    loc.first.click(timeout=5000)
                    p.wait_for_timeout(2500)
                    opened = True
                    break
            except Exception:
                continue
        if not opened:
            break
        if delete_open_email(p):
            deleted += 1
            print(f"  purged code email #{deleted}", flush=True)
        else:
            print("  could not delete an open code email — stopping purge", flush=True)
            break
        try:
            p.reload(wait_until="domcontentloaded", timeout=20000)
            p.wait_for_timeout(2500)
        except Exception:
            pass
    ss(p, "mailread-after-purge")
    return deleted


def main():
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE, headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            p = login(ctx)
            if not wait_inbox(p):
                print("WARN inbox not confirmed, proceeding", flush=True)
            if MODE == "purge":
                n = purge_codes(p)
                print(f"ZKPURGE={n}", flush=True)
                return 0
            for attempt in range(READ_MAX):
                code = read_code(p)
                if code:
                    print(f"ZKOTP={code}", flush=True)
                    # delete it so it can't be reused as a stale code next time
                    delete_open_email(p)
                    return 0
                print(f"  no code yet [{attempt+1}/{READ_MAX}]", flush=True)
                time.sleep(5)
                try:
                    p.reload(wait_until="domcontentloaded", timeout=20000)
                    p.wait_for_timeout(2500)
                except Exception:
                    pass
            print("ZKOTP=NONE", flush=True)
            return 2
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
