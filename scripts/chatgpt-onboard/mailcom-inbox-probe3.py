#!/usr/bin/env python3
# mailcom-inbox-probe3.py — prove shadow-DOM-aware OTP read on mail.com poseidon.
#
# ROOT CAUSE (proven by probe2 screenshot 2026-08-19): the inbox DOES render.
# mail.com's "poseidon" webmail draws the message list inside Shadow DOM web
# components, so document.body.innerText (what the old scraper uses) is
# SHADOW-BLIND and returns only the portal chrome -> scraper thinks "still
# loading" -> blind-clicks y=180 (an AD row / header) -> never reads the code.
#
# This probe proves the fix WITHOUT burning a fresh ChatGPT OTP: it logs into
# mail.com, then uses (a) a recursive deepText() walker that descends into
# shadowRoot, and (b) Playwright locators (which pierce open shadow roots) to
#   1. confirm the list text ("Inbox"/"Compose"/"登录代码") is now visible,
#   2. locate the NEWEST ChatGPT/OpenAI "login code" row (skipping ad rows),
#   3. click it and extract the 6-digit code from the opened message.
#
# Env: MAIL_USER (or CHATGPT_EMAIL), MAIL_PW_FILE (default /run/mail_pw),
#      PRECLICK_SEC (default 60), MAILCOM_HOME_SETTLE_SEC (default 120),
#      INBOX_SETTLE_SEC (default 40). Screenshots -> /work/probe3.
import os
import re
import time

from patchright.sync_api import sync_playwright

EMAIL = os.environ.get("MAIL_USER") or os.environ.get("CHATGPT_EMAIL") or ""
MAIL_PASSWORD = open(os.environ.get("MAIL_PW_FILE", "/run/mail_pw")).read().strip()
OUT = "/work/probe3"
os.makedirs(OUT, exist_ok=True)
PRE = int(os.environ.get("PRECLICK_SEC", "60"))
HOME = int(os.environ.get("MAILCOM_HOME_SETTLE_SEC", "120"))
INBOX_SETTLE = int(os.environ.get("INBOX_SETTLE_SEC", "40"))

# subject text that marks a real ChatGPT/OpenAI login-code mail
OTP_SUBJ_RE = re.compile(r"(登录代码|登入代码|login code|verification code|临时.*代码|code.*ChatGPT)", re.I)
AD_RE = re.compile(r"(anzeige|mail\.com games|play for free|sponsored|advertisement)", re.I)
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# JS: recursive text extraction that DESCENDS INTO shadowRoot (innerText does not)
DEEP_TEXT_JS = r"""
() => {
  function deep(node, acc){
    if(!node) return;
    if(node.nodeType===3){ acc.push(node.textContent); return; }
    if(node.shadowRoot) deep(node.shadowRoot, acc);
    const kids = node.childNodes||[];
    for(const c of kids) deep(c, acc);
  }
  const acc=[];
  deep(document.body, acc);
  return acc.join(' ').replace(/\s+/g,' ').trim();
}
"""


def log(m):
    print(m, flush=True)


def shot(page, name):
    try:
        page.screenshot(path=f"{OUT}/{name}.png")
    except Exception as e:
        log(f"  shot {name} err={e}")


def deep_text_of_frame(fr):
    try:
        return fr.evaluate(DEEP_TEXT_JS) or ""
    except Exception:
        return ""


def find_mail_frame(page):
    """The poseidon webmail app iframe (name='mail', url webmailer.mail.com)."""
    best = None
    for fr in page.frames:
        try:
            if fr.name == "mail" or "webmailer.mail.com" in (fr.url or ""):
                best = fr
        except Exception:
            pass
    return best


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
        log(f"== inbox settle {INBOX_SETTLE}s ==")
        time.sleep(INBOX_SETTLE)
        shot(page, "10-inbox")

        # ---- STEP 1: prove shadow-pierce detection ----
        log("== STEP 1: deepText (shadow-piercing) across frames ==")
        mail_fr = find_mail_frame(page)
        log(f"  mail_frame found={mail_fr is not None} url={(mail_fr.url if mail_fr else '')[:70]}")
        target_fr = None
        for i, fr in enumerate(page.frames):
            dt = deep_text_of_frame(fr)
            has_chrome = any(k in dt for k in ("Inbox", "收件箱", "Compose", "Compose email", "Unread"))
            has_otp = bool(OTP_SUBJ_RE.search(dt))
            if has_chrome or has_otp:
                log(f"  frame[{i}] name={fr.name!r:10} chrome={has_chrome} otp={has_otp} len={len(dt)}")
                log(f"     deepText[:300]: {dt[:300]}")
                if has_chrome or has_otp:
                    target_fr = fr
        if target_fr is None:
            target_fr = mail_fr
        log(f"  -> target_fr name={getattr(target_fr,'name',None)!r}")

        # ---- STEP 2: locate ChatGPT OTP rows via shadow-piercing locators ----
        log("== STEP 2: locate OTP rows (Playwright locators pierce open shadow) ==")
        if target_fr is None:
            log("  NO target frame; abort")
            log("== PROBE3 DONE ==")
            browser.close()
            return

        # find elements whose text mentions a login code; collect their box+text
        cands = []
        try:
            loc = target_fr.get_by_text(OTP_SUBJ_RE)
            n = loc.count()
            log(f"  get_by_text(OTP_SUBJ_RE) matches={n}")
            for k in range(min(n, 12)):
                el = loc.nth(k)
                try:
                    txt = (el.inner_text(timeout=1500) or "").strip().replace("\n", " ")[:60]
                    box = el.bounding_box()
                    vis = el.is_visible(timeout=800)
                except Exception:
                    txt, box, vis = "?", None, False
                y = box["y"] if box else 99999
                is_ad = bool(AD_RE.search(txt))
                log(f"     cand[{k}] y={y} vis={vis} ad={is_ad} txt={txt!r}")
                if vis and not is_ad and box:
                    cands.append((y, k, txt))
        except Exception as e:
            log(f"  get_by_text err={e}")

        cands.sort()
        if not cands:
            log("  NO visible non-ad OTP row found via locator; abort STEP3")
            shot(page, "20-no-otp-row")
            log("== PROBE3 DONE ==")
            browser.close()
            return
        top_y, top_k, top_txt = cands[0]
        log(f"  -> newest OTP row: k={top_k} y={top_y} txt={top_txt!r}")

        # ---- STEP 3: click it, read the code ----
        log(f"== STEP 3: wait {PRE}s then click OTP row ==")
        time.sleep(PRE)
        try:
            target_fr.get_by_text(OTP_SUBJ_RE).nth(top_k).click(timeout=8000)
            log("  clicked OTP row")
        except Exception as e:
            log(f"  row click err={e}")
        time.sleep(12)
        shot(page, "30-opened")

        # read code from the whole page via deepText (message opens in reading pane)
        code = None
        for i, fr in enumerate(page.frames):
            dt = deep_text_of_frame(fr)
            m = CODE_RE.search(dt)
            if m and ("ChatGPT" in dt or "OpenAI" in dt or OTP_SUBJ_RE.search(dt)):
                code = m.group(1)
                log(f"  frame[{i}] name={fr.name!r} -> CODE candidate {code}")
                log(f"     ctx[:200]: {dt[:200]}")
                break
        if not code:
            # fallback: any 6-digit in the mail frame
            dt = deep_text_of_frame(target_fr)
            m = CODE_RE.search(dt)
            if m:
                code = m.group(1)
                log(f"  fallback CODE {code}")
        log(f"== RESULT CODE={code} ==")
        log("== PROBE3 DONE ==")
        browser.close()


if __name__ == "__main__":
    main()
