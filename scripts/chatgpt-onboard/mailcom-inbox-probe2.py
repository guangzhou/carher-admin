#!/usr/bin/env python3
# mailcom-inbox-probe2.py — DOM-enumeration probe for the mail.com navigator hub.
#
# probe v1 proved: after login we sit on navigator-lxa.mail.com/mail (the "hub"
# with tiles Email/Photos&Files/Services/Upgrade) and the inbox message list
# NEVER renders — 5min of patience (no reload) changed nothing, and a
# button:has-text('Email') click was a no-op. So it is NOT a timing problem.
#
# This probe does NOT touch ChatGPT (no OTP burn). It logs into mail.com then,
# instead of waiting, it DUMPS THE TRUTH:
#   - every frame (url+name+first 240 chars of innerText), not just mail-ish ones
#     -> reveals a hidden consent/CMP iframe if one is blocking the SPA
#   - every <a>/<button>/[role=button] with text+href+target+visible+box
#     -> reveals the REAL inbox entry point (and whether it is target=_blank)
#   - listens for popups (context 'page' events) -> catches a new-tab inbox
#   - detects+clicks a consent "Accept/Agree/Zustimmen/同意" control if present,
#     then re-dumps to see whether the list appears
#   - tries a couple of direct inbox-app URL candidates as a last check
#
# Env: MAIL_USER (or CHATGPT_EMAIL), MAIL_PW_FILE (default /run/mail_pw),
#      PRECLICK_SEC (default 60). Screenshots -> /work/probe2.
import os
import re
import time

from patchright.sync_api import sync_playwright

EMAIL = os.environ.get("MAIL_USER") or os.environ.get("CHATGPT_EMAIL") or ""
MAIL_PASSWORD = open(os.environ.get("MAIL_PW_FILE", "/run/mail_pw")).read().strip()
OUT = "/work/probe2"
os.makedirs(OUT, exist_ok=True)
PRE = int(os.environ.get("PRECLICK_SEC", "60"))

SENDER_RE = re.compile(r"(openai|chatgpt|登录代码|verification|验证码|临时)", re.I)
LIST_MARKERS = ("收件箱", "Inbox", "Posteingang", "撰写", "Compose", "New email", "新邮件")
CONSENT_RE = re.compile(r"(accept all|accept|agree|zustimmen|einverstanden|同意|接受|akzeptieren|i agree|got it)", re.I)


def log(m):
    print(m, flush=True)


def shot(page, name):
    try:
        page.screenshot(path=f"{OUT}/{name}.png")
    except Exception as e:
        log(f"  shot {name} err={e}")


def dump_all_frames(page, tag):
    try:
        url = page.url
    except Exception:
        url = "?"
    log(f"[{tag}] top.url={url}  frames={len(page.frames)}")
    list_hit = False
    for i, fr in enumerate(page.frames):
        try:
            t = fr.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            t = ""
        if any(k in t for k in LIST_MARKERS):
            list_hit = True
        flat = " ".join(x.strip() for x in t.split("\n") if x.strip())[:240]
        log(f"  frame[{i}] name={fr.name!r} url={(fr.url or '')[:80]}")
        log(f"     TXT: {flat}")
    log(f"  LIST_CHROME_PRESENT={list_hit}")
    shot(page, tag)
    return list_hit


def dump_clickables(page):
    log("== clickable inventory (a / button / [role=button]) ==")
    for i, fr in enumerate(page.frames):
        try:
            items = fr.evaluate(
                """() => {
                    const out=[];
                    const els=document.querySelectorAll("a,button,[role=button]");
                    for (const e of els) {
                        const t=(e.innerText||e.textContent||'').trim().replace(/\\s+/g,' ').slice(0,40);
                        const href=e.getAttribute('href')||'';
                        const tgt=e.getAttribute('target')||'';
                        const r=e.getBoundingClientRect();
                        const vis=r.width>0&&r.height>0;
                        if(!t && !href) continue;
                        out.push({t,href,tgt,vis,x:Math.round(r.x),y:Math.round(r.y)});
                    }
                    return out.slice(0,60);
                }"""
            ) or []
        except Exception as e:
            log(f"  frame[{i}] clickable eval err={e}")
            continue
        if not items:
            continue
        log(f"  frame[{i}] name={fr.name!r} ({len(items)} clickables)")
        for it in items:
            log(f"     t={it['t']!r:44} href={it['href'][:50]!r} target={it['tgt']!r} vis={it['vis']} @({it['x']},{it['y']})")


def try_consent(page):
    """Find & click a consent/accept control in any frame. Return True if clicked."""
    for i, fr in enumerate(page.frames):
        try:
            btns = fr.locator("button, a, [role=button]")
            n = min(btns.count(), 40)
        except Exception:
            continue
        for j in range(n):
            b = btns.nth(j)
            try:
                txt = (b.inner_text(timeout=800) or "").strip()
            except Exception:
                continue
            if txt and CONSENT_RE.search(txt):
                try:
                    if b.is_visible(timeout=800):
                        log(f"  consent candidate frame[{i}] text={txt!r} -> clicking")
                        b.click(timeout=5000)
                        return True
                except Exception as e:
                    log(f"  consent click err={e}")
    return False


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

        popups = []
        ctx.on("page", lambda p: popups.append(p))

        page = ctx.new_page()
        page.set_default_timeout(30000)

        log(f"== goto www.mail.com (email={EMAIL}) ==")
        page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(int(os.environ.get("MAILCOM_HOME_SETTLE_SEC", "120")))

        # a consent wall often appears on the marketing home BEFORE login
        if try_consent(page):
            log("  (accepted a consent wall on home)")
            time.sleep(5)

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
        time.sleep(20)

        # ---- TRUTH DUMP #1: the hub as landed ----
        dump_all_frames(page, "10-hub-landed")
        dump_clickables(page)

        # ---- consent inside webmail? ----
        log("== try consent inside webmail ==")
        if try_consent(page):
            log("  consent clicked; settle 15s then re-dump")
            time.sleep(15)
            dump_all_frames(page, "20-after-consent")
        else:
            log("  no consent control matched inside webmail")

        # ---- try clicking a real 'Email' ANCHOR (href-based, not the no-op button) ----
        log("== try Email anchor (href-based) ==")
        email_click = False
        for sel in ("a[href*='mailintern']", "a[href*='/mail/']", "a[href$='/mail']",
                    "a[data-portal='mail']", "a:has-text('Email')", "*[title='Email']"):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    href = loc.get_attribute("href")
                    log(f"  clicking {sel!r} href={href!r}")
                    loc.click(timeout=5000)
                    email_click = True
                    break
            except Exception:
                pass
        log(f"  email_anchor_clicked={email_click}")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        time.sleep(12)
        dump_all_frames(page, "30-after-email-anchor")

        # ---- popups? (new-tab inbox) ----
        if popups:
            log(f"== {len(popups)} popup page(s) opened ==")
            for k, p in enumerate(popups):
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                dump_all_frames(p, f"40-popup-{k}")
        else:
            log("== no popup pages opened ==")

        log("== PROBE2 DONE ==")
        browser.close()


if __name__ == "__main__":
    main()
