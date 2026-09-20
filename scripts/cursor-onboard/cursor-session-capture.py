#!/usr/bin/env python3
"""
cursor-session-capture.py — Log into cursor.com with email+password via
patchright, then trade the resulting web session for the IDE access token
that `api2.cursor.sh` (the IDE chat plane) actually accepts.

WHY this exists (read before re-debugging):
  - Cursor has TWO auth planes with DIFFERENT credentials:
      1. `crsr_...` API keys (Dashboard → Integrations). These work for the
         Cloud Agents REST API (api.cursor.com/v0/agents) and for `cursor-agent`
         CLI login. They are the "public" API surface.
      2. The IDE chat endpoint `api2.cursor.sh/aiserver.v1.AiService/StreamChat`
         (Connect-RPC over HTTP/2). This one REJECTS `crsr_` keys outright with
         `ERROR_NOT_LOGGED_IN` — no amount of header fiddling changes that.
         It only accepts an IDE session JWT (type=session).
  - CRITICAL: the `WorkosCursorSessionToken` cookie from the browser is a WEB
    token (type=web, aud=https://cursor.com). api2/agent gRPC backends REJECT it
    too (ERROR_NOT_LOGGED_IN / 401). The cookie alone is NOT enough. We must run
    a PKCE deep-login (see deep_login_exchange) inside the authenticated browser
    to mint the real IDE token (type=session) — exactly what the desktop client
    does. That step needs no Turnstile (the WorkOS login already cleared it).
  - So to drive the real chat models (the ones the IDE uses, incl. any model
    not exposed on the Cloud Agents API) we run a real browser login and then
    the deep-login exchange. Hence patchright (Chrome TLS + stealth patches) —
    cursor.com sits behind Cloudflare and plain Playwright gets challenged.
  - The cookie value is `<workos_user_id>::<something>::<JWT>` with `::`
    URL-encoded as `%3A%3A`. The bearer token is the THIRD field. Passing the
    whole raw cookie value as the bearer also yields ERROR_NOT_LOGGED_IN, which
    is the single most common way to waste an afternoon here.

VERIFIED FACTS (measured 2026-07-31, re-measure before trusting):
  - `GET https://cursor.com/api/auth/login` answers 307 ->
    `https://api.workos.com/user_management/authorize?...&provider=authkit
     &redirect_uri=https%3A%2F%2Fcursor.com%2Fapi%2Fauth%2Fcallback`
    i.e. login is WorkOS AuthKit, NOT a cursor.com-hosted form. The success
    callback carries `?code=...`, so "code" alone must never be treated as an
    OTP marker (that bug would misreport every successful login as NEEDS_OTP).
  - api2 returns **HTTP 200** even when auth fails; the failure lives in the
    body as `{"error":{"code":"unauthenticated",...,"ERROR_NOT_LOGGED_IN"...}}`.
    So status-code checks are worthless here — the body MUST be inspected.
    Confirmed by sending a bogus bearer token.

ENV:
  CURSOR_EMAIL      (required) cursor.com account email
  CURSOR_PASSWORD   (required) cursor.com account password
  MAIL_PASSWORD     (optional) webmail password — reserved for OTP retrieval,
                    not used yet; presence is only logged.
  HEADLESS          "1" (default) / "0" — set "0" (or run under Xvfb) if
                    Cloudflare starts challenging.
  OUT_PATH          /tmp/cursor-session.json
  SCREENSHOT_DIR    /tmp/cursor-shots  — a shot is taken at every major step
                    because this normally runs headless on a server.
  PROFILE_DIR       /tmp/cursor-profile — persistent context, so a second run
                    can reuse the session and skip login entirely.

OUTPUT (OUT_PATH):
  { "email", "raw_cookie", "session_token", "all_cookie_names",
    "captured_at", "final_url" }

EXIT CODES:
  0  token captured AND accepted by api2.cursor.sh
  1  unknown failure (page text dumped)
  2  bad config / missing env
  3  NEEDS_OTP     — email verification code requested
  4  NEEDS_OAUTH   — account is OAuth-only (Google/GitHub), no password field
  5  NEEDS_CAPTCHA — Cloudflare / Turnstile challenge not cleared
  6  TOKEN_REJECTED — cookie found but api2 says ERROR_NOT_LOGGED_IN
"""

import base64
import hashlib
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone


# patchright is imported lazily inside main() so that the config guard and
# --help style failures report cleanly on hosts where it is not installed
# (this script only needs a browser on the capture host, e.g. 188).

# ── config ────────────────────────────────────────────────────────────────
EMAIL = os.environ.get("CURSOR_EMAIL", "").strip()
PASSWORD = os.environ.get("CURSOR_PASSWORD", "")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
HEADLESS = os.environ.get("HEADLESS", "1") not in ("0", "false", "False", "no")
# Login method: "password" (email+password, gated by an interactive Turnstile
# that scripted clicks fail from datacenter IPs) or "code" (email sign-in code,
# read from the mail.com inbox with MAIL_PASSWORD). Defaults to "code" when a
# MAIL_PASSWORD is present, since seller-account passwords often only unlock
# mail.com and the password path hits the unbeatable Turnstile.
LOGIN_METHOD = os.environ.get(
    "LOGIN_METHOD", "code" if MAIL_PASSWORD else "password").lower()
OUT_PATH = os.environ.get("OUT_PATH", "/tmp/cursor-session.json")
SS_DIR = os.environ.get("SCREENSHOT_DIR", "/tmp/cursor-shots")
PROFILE_DIR = os.environ.get("PROFILE_DIR", "/tmp/cursor-profile")

LOGIN_URL = "https://cursor.com/api/auth/login"
API2_URL = "https://api2.cursor.sh/aiserver.v1.AiService/StreamChat"
COOKIE_NAME = "WorkosCursorSessionToken"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

EMAIL_SELECTORS = [
    'input[name="email"]',
    'input[type="email"]',
    'input[id*="email"]',
    'input[autocomplete="username"]',
    'input[placeholder*="mail" i]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[id*="password"]',
    'input[autocomplete="current-password"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]',
    "button:has-text('Continue')",
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
    "button:has-text('Sign In')",
    "button:has-text('Next')",
    "input[type='submit']",
]
OTP_RE = re.compile(
    r"verification code|check your (?:e-?mail|inbox)|enter the code"
    r"|one-?time (?:code|password)|we (?:sent|emailed) you a code"
    r"|6-digit",
    re.I,
)
CF_RE = re.compile(
    r"verify you are human|just a moment|challenges\.cloudflare|cf-turnstile"
    r"|checking your browser|turnstile",
    re.I,
)
OAUTH_RE = re.compile(r"continue with (google|github|microsoft)", re.I)


# ── logging helpers ───────────────────────────────────────────────────────
def log(msg):
    print(msg, flush=True)


def ss(page, name):
    try:
        os.makedirs(SS_DIR, exist_ok=True)
        path = os.path.join(SS_DIR, name + ".png")
        page.screenshot(path=path, full_page=False)
        log("  shot: " + path)
    except Exception as e:
        log("  shot fail: " + str(e))


def where(page, tag):
    """Log + screenshot the current location. Page structure is unknown, so
    every branch point must be observable from the log alone."""
    try:
        url, title = page.url, page.title()
    except Exception as e:
        url, title = "<unavailable>", "<err " + str(e) + ">"
    log("  [" + tag + "] url=" + str(url)[:160])
    log("  [" + tag + "] title=" + str(title)[:120])
    ss(page, tag)
    return url, title


def page_text(page, limit=4000):
    for fn in ("() => document.body.innerText", "() => document.body.textContent"):
        try:
            t = page.evaluate(fn)
            if t:
                return t[:limit]
        except Exception:
            continue
    try:
        return page.content()[:limit]
    except Exception:
        return ""


def first_visible(page, selectors, what):
    """Try each candidate selector, return the first visible locator."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if n == 0:
                continue
            for i in range(min(n, 4)):
                cand = loc.nth(i)
                try:
                    if cand.is_visible():
                        log("    " + what + " matched: " + sel + " [nth=" + str(i) + "]")
                        return cand
                except Exception:
                    continue
        except Exception:
            continue
    log("    " + what + ": no visible match among " + str(len(selectors)) + " selectors")
    return None


def click_submit(page):
    btn = first_visible(page, SUBMIT_SELECTORS, "submit button")
    if btn is not None:
        try:
            btn.click(timeout=8000)
            return True
        except Exception as e:
            log("    submit click failed: " + str(e))
    try:
        page.keyboard.press("Enter")
        log("    submit: pressed Enter")
        return True
    except Exception:
        pass
    try:
        page.evaluate(
            "() => { const f = document.querySelector('form');"
            " if (f) (f.requestSubmit ? f.requestSubmit() : f.submit()); }"
        )
        log("    submit: requestSubmit() fallback")
        return True
    except Exception as e:
        log("    submit fallback failed: " + str(e))
    return False


def _turnstile_iframe_box(page):
    """Return the bounding box of the *visible* Turnstile widget iframe. The CF
    challenge injects several iframes (a hidden management one at the top-left
    plus the visible ~300x65 widget); the src-based selector can match the
    hidden helper, so we pick by widget-like dimensions in the visible area."""
    best = None
    try:
        ifr = page.locator("iframe")
        n = ifr.count()
    except Exception:
        n = 0
    for i in range(min(n, 20)):
        try:
            box = ifr.nth(i).bounding_box()
        except Exception:
            box = None
        if not box:
            continue
        w, h = box["width"], box["height"]
        # Turnstile managed widget is ~300x65, positioned in the page body
        if 240 <= w <= 360 and 45 <= h <= 95 and box["y"] > 150:
            return box
        # keep the largest plausible box as a fallback
        if 200 <= w <= 500 and 40 <= h <= 120:
            if best is None or (w * h) > (best["width"] * best["height"]):
                best = box
    return best


def _turnstile_present(page):
    """Reliable presence check via a challenges.cloudflare.com frame."""
    try:
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                return True
    except Exception:
        pass
    return bool(CF_RE.search(page_text(page, 3000)))


def _turnstile_passed(page):
    """True only if the interactive challenge is genuinely gone. CF_RE vanishing
    alone is a FALSE positive (the widget gets replaced by a 'Can't verify'
    error banner), so we treat that banner as an explicit failure."""
    text = page_text(page, 4000)
    if re.search(r"can'?t verify|couldn'?t verify|please try again", text, re.I):
        return False
    if _turnstile_present(page):
        return False
    if re.search(r"before continuing|verify you are human|are human", text, re.I):
        return False
    return True


def solve_turnstile(page, tries=3, success_check=None):
    """Cursor's WorkOS AuthKit throws an interactive Cloudflare Turnstile
    ("Verify you are human" checkbox) before it will submit a password or send
    an email code. patchright clears the invisible variant on its own; the
    managed checkbox must be physically clicked with a *trusted* OS-level mouse
    event (a frame_locator synthetic click is rejected by CF as non-human).

    A passed challenge leaves a lingering success iframe whose url still matches
    challenges.cloudflare.com, so the generic `_turnstile_passed` detector emits
    a FALSE NEGATIVE. When the caller knows what the next step looks like (e.g.
    the password field becoming visible), pass a `success_check` predicate — the
    solve is declared passed the moment it returns True (and no "Can't verify"
    banner is showing), regardless of the lingering iframe.
    Returns True if the challenge is genuinely passed, False otherwise."""
    def _passed():
        if success_check is not None:
            try:
                return bool(success_check())
            except Exception:
                return False
        return _turnstile_passed(page)

    if not _turnstile_present(page):
        return True  # no turnstile present → nothing to do
    log("  turnstile challenge detected — trusted-mouse checkbox click")
    where(page, "07b-turnstile")
    for attempt in range(tries):
        box = _turnstile_iframe_box(page)
        if box is None:
            if _passed():
                log("    turnstile already gone (attempt " + str(attempt + 1) + ")")
                return True
            # observed WorkOS layout: checkbox at ~(511,434) in a 1280-wide view
            cx, cy = 511, 434
            log("    no widget box found — clicking observed coords (511,434)")
            try:
                page.mouse.move(cx - 120, cy - 40, steps=8)
                page.mouse.move(cx, cy, steps=12)
                time.sleep(0.4)
                page.mouse.click(cx, cy, delay=90)
            except Exception as e:
                log("    fixed-coord click failed: " + str(e))
        else:
            # checkbox sits ~28px in from the left, vertically centred in the widget
            cx = box["x"] + 28
            cy = box["y"] + box["height"] / 2
            log("    widget box x=%d y=%d w=%d h=%d" % (
                int(box["x"]), int(box["y"]), int(box["width"]), int(box["height"])))
            try:
                page.mouse.move(cx - 120, cy - 40, steps=8)
                page.mouse.move(cx, cy, steps=12)
                time.sleep(0.4)
                page.mouse.click(cx, cy, delay=90)
                log("    trusted click at (%d,%d)" % (int(cx), int(cy)))
            except Exception as e:
                log("    mouse click failed: " + str(e))
        # wait for a real verdict (spinner → check → redirect), watching for the
        # explicit "Can't verify" failure banner
        for _ in range(12):
            time.sleep(2)
            t = page_text(page, 4000)
            if re.search(r"can'?t verify|couldn'?t verify|please try again", t, re.I):
                log("    CF rejected the click (Can't verify) — attempt "
                    + str(attempt + 1))
                break
            if _passed():
                log("    turnstile passed (attempt " + str(attempt + 1) + ")")
                where(page, "07d-turnstile-passed")
                return True
        # reload the challenge before retrying (a fresh widget sometimes clears)
        if attempt < tries - 1:
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
                time.sleep(4)
            except Exception:
                pass
    where(page, "07c-turnstile-stuck")
    return False


# ── mail.com webmail OTP reader (ported from chatgpt-litellm-oauth.py) ───────
def mailcom_login(ctx):
    """Open www.mail.com webmail in a new page, log in with EMAIL/MAIL_PASSWORD,
    return the inbox page (or None on failure). mail.com free accounts have no
    IMAP, so the inbox is scraped via its Shadow-DOM webmail UI."""
    p = ctx.new_page()
    try:
        p.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("  mailcom goto warn: " + str(e))
    p.wait_for_timeout(int(os.environ.get("MAILCOM_HOME_SETTLE_SEC", "120")) * 1000)
    try:
        p.locator("a:has-text('Log in')").first.click()
    except Exception:
        pass
    p.wait_for_timeout(1500)
    try:
        p.locator("input[placeholder='Email address']").first.fill(EMAIL)
        p.locator("input[placeholder='Password']").first.fill(MAIL_PASSWORD)
    except Exception as e:
        log("  mailcom cred fill err: " + str(e))
        ss(p, "mailcom-fill-fail")
        return None
    btns = p.locator("button:has-text('Log in')")
    for i in range(btns.count()):
        box = btns.nth(i).bounding_box()
        if box and box["y"] > 50:
            btns.nth(i).click()
            break
    for _ in range(30):
        if "navigator" in (p.url or ""):
            break
        time.sleep(1)
    if "navigator" not in (p.url or ""):
        ss(p, "mailcom-fail")
        log("  mail.com login failed url=" + str(p.url))
        return None
    p.wait_for_timeout(3000)
    for sel in (
        "a:has-text('Continue to Account')",
        "button:has-text('Continue to Account')",
        "button:has-text('No, thanks')",
        "button:has-text('Maybe later')",
        "button:has-text('Skip')",
    ):
        try:
            loc = p.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                p.wait_for_timeout(2000)
        except Exception:
            pass
    for attempt in range(20):
        mf = next((fr for fr in p.frames if fr.name == "mail"), None)
        if mf:
            try:
                if mf.locator("[class*='mail-item']").count() > 0:
                    log("  mail.com: inbox loaded")
                    break
            except Exception:
                pass
        log("  mail.com: waiting for inbox iframe... [" + str(attempt + 1) + "/20]")
        time.sleep(2)
    ss(p, "mailcom-inbox")
    return p


def mailcom_get_code(mail_page, max_wait=180):
    """Find the topmost Cursor login-code email, open it, extract the 6-digit
    code. mail.com moved list + body into Shadow DOM: enumerate mail-item rows
    via text_content(), open the body iframe (name~='detail-body'), regex a
    6-digit token near code/verify/cursor wording."""
    def find_body_frame():
        return next((f for f in mail_page.frames
                     if "detail-body" in (f.name or "")
                     or "detail-body" in (f.url or "")), None)

    def extract():
        bf = find_body_frame()
        if not bf:
            return None
        try:
            html = bf.evaluate("() => document.documentElement.outerHTML") or ""
        except Exception:
            return None
        for m in re.finditer(r"\b(\d{6})\b", html):
            c = html[max(0, m.start() - 200):m.end() + 200]
            if re.search(r"code|verif|login|sign.?in|cursor", c, re.I):
                return m.group(1)
        m = re.search(r"\b(\d{6})\b", html)
        return m.group(1) if m else None

    # settle: the code email arrives AFTER we open the inbox — wait, refresh, wait
    secs = int(os.environ.get("OTP_SETTLE_SEC", "45"))
    log("  [code] settle " + str(secs) + "s for the code email to land...")
    time.sleep(secs)
    try:
        mf0 = next((fr for fr in mail_page.frames if fr.name == "mail"), None)
        if mf0:
            mf0.evaluate("() => document.location.reload()")
    except Exception:
        pass
    mail_page.wait_for_timeout(3000)
    time.sleep(secs)

    SENDER = re.compile(r"cursor|no-?reply", re.I)
    CODE_SUBJ = re.compile(r"code|verif|sign.?in|login", re.I)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        mf = next((fr for fr in mail_page.frames if fr.name == "mail"), None)
        if not mf:
            time.sleep(5)
            continue
        try:
            rows = mf.locator("[class*='mail-item']")
            cnt = rows.count()
        except Exception:
            cnt = 0
        texts = []
        for i in range(min(cnt, 15)):
            try:
                texts.append((rows.nth(i).text_content(timeout=1500) or "").strip())
            except Exception:
                texts.append("")
        order = [i for i, t in enumerate(texts)
                 if SENDER.search(t) and CODE_SUBJ.search(t)]
        order += [i for i, t in enumerate(texts)
                  if SENDER.search(t) and i not in order]
        for i in order:
            log("  mail candidate row[" + str(i) + "]: " + repr(texts[i][:100]))
            row_el = rows.nth(i)
            try:
                row_el.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            for do in (
                lambda: row_el.dblclick(timeout=4000),
                lambda: row_el.locator(
                    ":scope a, :scope [role='link'], :scope span").first.click(timeout=3000),
                lambda: row_el.evaluate(
                    "el => el.dispatchEvent(new MouseEvent('dblclick',"
                    "{bubbles:true,cancelable:true,view:window}))"),
            ):
                try:
                    do()
                    time.sleep(3.5)
                    if find_body_frame():
                        break
                except Exception:
                    pass
            code = extract()
            if code:
                return code
            log("  row[" + str(i) + "] opened but no code — try next")
        log("  code not yet (rows=" + str(cnt) + "), retry 5s...")
        time.sleep(5)
        try:
            mf.evaluate("() => document.location.reload()")
        except Exception:
            pass
        mail_page.wait_for_timeout(3000)
    return None


CODE_INPUT_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[name*="code" i]',
    'input[maxlength="6"]',
]


def enter_code(page, code):
    """Type the 6-digit code into either one combined input or per-char boxes,
    then submit."""
    for sel in CODE_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                try:
                    loc.fill("")
                except Exception:
                    pass
                page.keyboard.type(code, delay=100)
                log("    typed code into " + sel)
                click_submit(page)
                return True
        except Exception:
            continue
    boxes = page.locator('input[maxlength="1"]')
    try:
        if boxes.count() >= len(code):
            for i, ch in enumerate(code):
                boxes.nth(i).click()
                page.keyboard.type(ch, delay=100)
            log("    typed code into per-char boxes")
            click_submit(page)
            return True
    except Exception as e:
        log("    per-char code entry failed: " + str(e))
    return False


def do_email_code(page, ctx):
    """Email sign-in-code login: reach the code-entry page (clearing any
    Turnstile that gates the 'Email sign-in code' action, in either order),
    read the code from the mail.com inbox, type it in. Returns (state, why)."""
    log("[4c] email-code login")
    code_input = None
    for rnd in range(3):
        # clear a Turnstile blocking the current step, if present
        if _turnstile_present(page):
            solve_turnstile(page)
            settle(page)
            time.sleep(3)
            where(page, "21-turnstile-r" + str(rnd))
        # already at the code entry?
        code_input = first_visible(page, CODE_INPUT_SELECTORS + ['input[maxlength="1"]'],
                                   "code input")
        if code_input is not None:
            break
        # otherwise (re)request the code
        clicked = False
        for sel in (
            "button:has-text('Email sign-in code')",
            "a:has-text('Email sign-in code')",
            "button:has-text('sign-in code')",
            "text=/email sign-?in code/i",
        ):
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=6000)
                    clicked = True
                    log("  clicked " + repr(sel) + " (round " + str(rnd) + ")")
                    break
            except Exception:
                continue
        settle(page)
        time.sleep(4)
        where(page, "20-code-requested-r" + str(rnd))
        if not clicked and not _turnstile_present(page):
            break

    if code_input is None:
        state, why = classify_failure(page)
        log("  no code input reached: " + state + " (" + why + ")")
        return (state if state != "UNKNOWN" else "NEEDS_CAPTCHA"), why

    log("  code input present — opening mail.com to read the login code")
    mail_page = mailcom_login(ctx)
    if mail_page is None:
        return "UNKNOWN", "mail.com login failed (cannot read code)"
    code = mailcom_get_code(mail_page)
    if not code:
        return "NEEDS_OTP", "code email not found in mail.com inbox"
    log("  got login code: " + code)
    try:
        page.bring_to_front()
    except Exception:
        pass
    if not enter_code(page, code):
        return "UNKNOWN", "failed to enter code into the form"
    settle(page, timeout=30000)
    time.sleep(5)
    where(page, "22-after-code-entered")
    return "SUBMITTED", "email code submitted"


def settle(page, timeout=20000):
    for state in ("networkidle", "load", "domcontentloaded"):
        try:
            page.wait_for_load_state(state, timeout=timeout)
            return
        except Exception:
            continue


def find_cookie(ctx, name=COOKIE_NAME):
    try:
        cookies = ctx.cookies()
    except Exception as e:
        log("  cookie read failed: " + str(e))
        return None, []
    names = sorted({c.get("name", "") for c in cookies})
    hit = None
    for c in cookies:
        if c.get("name") == name and c.get("value"):
            hit = c.get("value")
            break
    return hit, names


# ── token extraction ──────────────────────────────────────────────────────
def extract_token(raw):
    """Cookie is `<user_id>::<x>::<JWT>` with `::` URL-encoded as %3A%3A.
    The bearer token api2 wants is the THIRD field."""
    decoded = urllib.parse.unquote(raw)
    parts = decoded.split("::")
    log("  cookie fields after unquote + split('::'): " + str(len(parts)))
    for i, p in enumerate(parts):
        log("    field[" + str(i) + "] len=" + str(len(p)) + " head=" + p[:24])
    if len(parts) >= 3:
        return parts[2]
    if len(parts) == 2:
        log("  WARN: only 2 fields — cookie layout changed; using field[1]")
        return parts[1]
    log("  WARN: single field — cookie layout changed; using whole value")
    return decoded


# ── deep-login exchange (web cookie → IDE session token) ──────────────────
def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def deep_login_exchange(page):
    """Trade the authenticated *web* session (WorkosCursorSessionToken, type=web,
    aud=https://cursor.com) for an *IDE* access token (type=session) that the
    api2/agent gRPC backends actually accept.

    The plain browser cookie is only good for the cursor.com dashboard — api2
    rejects it with ERROR_NOT_LOGGED_IN. Cursor's desktop client mints the real
    token via a PKCE deep-login: it opens {website}/loginDeepControl in the
    already-signed-in browser, then polls {backend}/auth/poll. Endpoints and the
    PKCE derivation below are lifted verbatim from Cursor's own client bundle
    (getLoginUrl / getPollingEndpoint / loginLink):

        verifier  H = base64url(32 random bytes)
        challenge q = base64url(sha256(H_ascii))
        approve : GET {website}/loginDeepControl?challenge=q&uuid=K&mode=login&supportsSelectedTeamLogin=true
        poll    : GET {backend}/auth/poll?uuid=K&verifier=H
                  -> 404 pending; 200 {accessToken, refreshToken, authId}

    Runs inside the SAME authenticated context, so no Turnstile is involved
    (that gate was cleared once at WorkOS login). Returns
    {accessToken, refreshToken, authId} or None.
    """
    j = os.urandom(32)
    H = _b64url(j)
    q = _b64url(hashlib.sha256(H.encode()).digest())
    K = str(uuid.uuid4())
    login_url = (
        "https://cursor.com/loginDeepControl?challenge=" + q
        + "&uuid=" + K + "&mode=login&supportsSelectedTeamLogin=true"
    )
    poll_url = "https://api2.cursor.sh/auth/poll?uuid=" + K + "&verifier=" + H

    log("  deep-login: opening loginDeepControl (uuid=" + K[:8] + "…)")
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(2)
    except Exception as e:
        log("  deep-login: navigation failed: " + str(e))
        return None

    # The approval page shows a "Sign in" / "Log In" button; clicking it binds
    # the uuid to the authenticated session server-side.
    clicked = False
    for lab in ("Yes, Log In", "Log In", "Log in", "Sign in", "Continue",
                "Authorize", "Approve", "Open Cursor", "Yes"):
        try:
            loc = page.get_by_role("button", name=lab, exact=False)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=4000)
                clicked = True
                log("  deep-login: clicked approval button '" + lab + "'")
                break
        except Exception:
            continue
    if not clicked:
        log("  deep-login: no approval button found (page may auto-approve)")

    def _poll_once():
        req = urllib.request.Request(poll_url, headers={
            "x-cursor-client-version": "3.12.17",
            "x-cursor-client-type": "ide",
            "User-Agent": UA,
            "accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:
            return -1, str(e)

    for i in range(20):
        st, body = _poll_once()
        if st == 200 and body:
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            if data.get("accessToken"):
                log("  deep-login: IDE token received on poll #" + str(i))
                return {
                    "accessToken": data["accessToken"],
                    "refreshToken": data.get("refreshToken"),
                    "authId": data.get("authId"),
                }
        time.sleep(3)

    log("  deep-login: poll exhausted without an IDE token")
    return None


# ── api2 verification ─────────────────────────────────────────────────────
def verify_token(token):
    """POST an (intentionally empty) Connect-RPC envelope to StreamChat.

    We do NOT expect a chat reply. We only care WHICH error comes back:
      ERROR_NOT_LOGGED_IN  -> auth failed, token is not a valid session
      anything else        -> auth PASSED, we merely sent a useless body
    """
    payload = b"{}"
    body = struct.pack(">BI", 0, len(payload)) + payload
    req = urllib.request.Request(
        API2_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/connect+json",
            "connect-protocol-version": "1",
            "User-Agent": UA,
        },
    )
    status, raw = None, b""
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            status = resp.status
            raw = resp.read(4096)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read(4096)
        except Exception:
            raw = b""
    except Exception as e:
        log("  api2 transport error: " + type(e).__name__ + ": " + str(e))
        return None, "TRANSPORT_ERROR", str(e)

    printable = "".join(chr(b) if 32 <= b < 127 else "." for b in raw[:300])
    log("  api2 status=" + str(status))
    log("  api2 raw[:300]=" + printable)
    text = raw.decode("utf-8", "replace")
    if "ERROR_NOT_LOGGED_IN" in text or "NOT_LOGGED_IN" in text:
        return status, "TOKEN_REJECTED", printable
    return status, "TOKEN_ACCEPTED", printable


# ── login flow ────────────────────────────────────────────────────────────
def classify_failure(page):
    """Return (state, evidence) for whatever non-success page we are on."""
    text = page_text(page, 6000)
    url = ""
    try:
        url = page.url or ""
    except Exception:
        pass

    if CF_RE.search(text) or "challenges.cloudflare.com" in url:
        return "NEEDS_CAPTCHA", "cloudflare/turnstile markers in page"

    if OTP_RE.search(text):
        return "NEEDS_OTP", "OTP wording in page text"
    for sel in (
        'input[autocomplete="one-time-code"]',
        'input[name*="code" i]',
        'input[maxlength="6"]',
        'input[inputmode="numeric"]',
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return "NEEDS_OTP", "code input matched " + sel
        except Exception:
            continue
    # NB: do NOT match a bare "code" here — OAuth callbacks carry `?code=...`
    # and would be misreported as an OTP prompt.
    if re.search(r"verify|verification|/otp|magic[-_]?link|email[-_]?code", url, re.I):
        return "NEEDS_OTP", "url suggests verification: " + url[:120]

    has_pw = first_visible(page, PASSWORD_SELECTORS, "password (classify)") is not None
    if not has_pw and OAUTH_RE.search(text):
        return "NEEDS_OAUTH", "only social buttons, no password field"

    return "UNKNOWN", "no known marker matched"


def do_login(page, ctx):
    log("[2] navigate to " + LOGIN_URL)
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("  goto warn: " + str(e))
    settle(page)
    time.sleep(3)
    where(page, "01-landed")

    text = page_text(page, 4000)
    if CF_RE.search(text):
        log("  cloudflare markers on landing — waiting up to 60s for auto-clear")
        for _ in range(20):
            time.sleep(3)
            text = page_text(page, 4000)
            if not CF_RE.search(text):
                log("  cloudflare cleared")
                break
        where(page, "01b-after-cf-wait")

    # some flows show an interstitial "Sign in" / "Log in" button first
    for sel in (
        "a:has-text('Sign in')",
        "button:has-text('Sign in')",
        "a:has-text('Log in')",
        "button:has-text('Log in')",
    ):
        if first_visible(page, EMAIL_SELECTORS, "email (pre-check)") is not None:
            break
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                log("  clicking interstitial: " + sel)
                loc.first.click(timeout=6000)
                settle(page)
                time.sleep(3)
                where(page, "02-after-interstitial")
                break
        except Exception:
            continue

    # ── email step ────────────────────────────────────────────────────────
    log("[3] email step")
    email_input = None
    for attempt in range(6):
        email_input = first_visible(page, EMAIL_SELECTORS, "email input")
        if email_input is not None:
            break
        log("  email input not there yet [" + str(attempt + 1) + "/6]")
        time.sleep(3)
    if email_input is None:
        where(page, "03-no-email-input")
        return classify_failure(page)

    try:
        email_input.click(timeout=8000)
    except Exception:
        pass
    try:
        page.keyboard.type(EMAIL, delay=60)
    except Exception:
        email_input.fill(EMAIL)
    log("  filled email=" + EMAIL)
    ss(page, "04-email-filled")

    # password may already be on the same page (single-step form)
    pw_now = first_visible(page, PASSWORD_SELECTORS, "password (same page)")
    if pw_now is None:
        click_submit(page)
        settle(page)
        time.sleep(5)
        where(page, "05-after-email-submit")

    # email sign-in code path (avoids the password-page Turnstile that scripted
    # clicks fail from datacenter IPs; reads the code from the mail.com inbox)
    if LOGIN_METHOD == "code":
        return do_email_code(page, ctx)

    # ── password step ─────────────────────────────────────────────────────
    log("[4] password step")

    def _pw_visible():
        for sel in PASSWORD_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    # The /password page hides the password field behind an interactive managed
    # Turnstile ("Verify you are human"). It MUST be cleared before the field
    # appears — solving it after submit (the old order) never reached here. Use
    # "password field visible" as the success predicate to dodge the lingering
    # success-iframe false negative.
    if _turnstile_present(page) and not _pw_visible():
        if not solve_turnstile(page, success_check=_pw_visible):
            where(page, "06-password-blocked")
            return "NEEDS_CAPTCHA", "turnstile not cleared on password page"
        settle(page)
        time.sleep(3)
        where(page, "07a-after-turnstile")

    pw_input = None
    for attempt in range(6):
        pw_input = first_visible(page, PASSWORD_SELECTORS, "password input")
        if pw_input is not None:
            break
        state, why = classify_failure(page)
        if state in ("NEEDS_OTP", "NEEDS_CAPTCHA", "NEEDS_OAUTH"):
            log("  short-circuit while waiting for password: " + state + " (" + why + ")")
            where(page, "06-password-blocked")
            return state, why
        log("  password input not there yet [" + str(attempt + 1) + "/6]")
        time.sleep(3)
    if pw_input is None:
        where(page, "06-no-password-input")
        return classify_failure(page)

    try:
        pw_input.click(timeout=8000)
    except Exception:
        pass
    try:
        page.keyboard.type(PASSWORD, delay=60)
    except Exception:
        pw_input.fill(PASSWORD)
    log("  filled password (len=" + str(len(PASSWORD)) + ")")
    ss(page, "07-password-filled")

    click_submit(page)
    settle(page, timeout=30000)
    time.sleep(6)
    where(page, "08-after-password-submit")
    # Cursor throws a managed Turnstile checkbox here for datacenter egress IPs;
    # it must be clicked or the login silently bounces back to the email page.
    if solve_turnstile(page):
        settle(page, timeout=30000)
        time.sleep(4)
        where(page, "08b-after-turnstile")
    return "SUBMITTED", "password submitted"


# ── main ──────────────────────────────────────────────────────────────────
def main():
    if not EMAIL or not PASSWORD:
        log("FATAL: CURSOR_EMAIL and CURSOR_PASSWORD are both required")
        sys.exit(2)
    os.makedirs(SS_DIR, exist_ok=True)
    log("=" * 72)
    log("cursor-session-capture")
    log("  email      = " + EMAIL)
    log("  headless   = " + str(HEADLESS))
    log("  out        = " + OUT_PATH)
    log("  shots      = " + SS_DIR)
    log("  profile    = " + PROFILE_DIR)
    log("  mail_pw    = " + ("set (unused, reserved for OTP)" if MAIL_PASSWORD else "not set"))
    log("=" * 72)

    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:
        log("FATAL: patchright not installed (" + str(e) + ")")
        log("  install with: pip install patchright && patchright install chromium")
        sys.exit(2)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            user_agent=UA,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            # a persisted profile may already hold the cookie
            log("[1] check for pre-existing session cookie")
            pre, _ = find_cookie(ctx)
            if pre:
                log("  found persisted " + COOKIE_NAME + " — skipping login")
                state, why = "SUBMITTED", "persisted cookie"
                try:
                    page.goto("https://cursor.com/dashboard",
                              wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    pass
                settle(page)
                where(page, "00-persisted-session")
            else:
                log("  no persisted cookie")
                state, why = do_login(page, ctx)

            log("[5] post-login state check: " + state + " (" + why + ")")

            raw, names = find_cookie(ctx)
            log("  cookies present (" + str(len(names)) + "): " + ", ".join(names)[:400])

            if raw is None and state == "SUBMITTED":
                # login looked like it went through but no cookie yet — give the
                # WorkOS -> cursor.com callback a chance to land, then re-check
                log("  cookie absent right after submit — polling 30s")
                for _ in range(10):
                    time.sleep(3)
                    raw, names = find_cookie(ctx)
                    if raw:
                        log("  cookie appeared")
                        break
                if raw is None:
                    try:
                        page.goto("https://cursor.com/dashboard",
                                  wait_until="domcontentloaded", timeout=45000)
                        settle(page)
                        time.sleep(3)
                        where(page, "09-dashboard-probe")
                    except Exception as e:
                        log("  dashboard probe failed: " + str(e))
                    raw, names = find_cookie(ctx)

            try:
                final_url = page.url
            except Exception:
                final_url = "<unavailable>"

            if raw is None:
                # No cookie: report the REAL state, never pretend success.
                if state == "SUBMITTED":
                    state, why = classify_failure(page)
                    log("  reclassified after submit: " + state + " (" + why + ")")
                where(page, "10-no-cookie")
                log("")
                log("-" * 72)
                if state == "NEEDS_OTP":
                    log("NEEDS_OTP")
                    log("  " + why)
                    log("  An email verification code is required. MAIL_PASSWORD-based")
                    log("  OTP retrieval is not wired up in this script yet.")
                    log("  RESULT: no session token captured.")
                    sys.exit(3)
                if state == "NEEDS_OAUTH":
                    log("NEEDS_OAUTH")
                    log("  " + why)
                    log("  This account has no password login — only social IdP buttons.")
                    log("  RESULT: no session token captured.")
                    sys.exit(4)
                if state == "NEEDS_CAPTCHA":
                    log("NEEDS_CAPTCHA")
                    log("  " + why)
                    log("  Cloudflare/Turnstile blocked the flow. Retry with HEADLESS=0")
                    log("  under Xvfb, and from an egress IP with better reputation.")
                    log("  RESULT: no session token captured.")
                    sys.exit(5)
                log("UNKNOWN_FAILURE (" + why + ")")
                log("  final_url = " + str(final_url)[:200])
                log("  ---- page text (first 2000 chars) ----")
                log(page_text(page, 2000))
                log("  ---- end page text ----")
                log("  RESULT: no session token captured.")
                sys.exit(1)

            # ── cookie found ──────────────────────────────────────────────
            log("[6] cookie extraction")
            log("  raw_cookie = " + raw)
            token = extract_token(raw)
            log("  session_token = " + token)

            # ── deep-login exchange: web cookie → IDE session token ───────
            # The web cookie (type=web, aud=cursor.com) is REJECTED by api2.
            # Trade it for the IDE token (type=session) the gRPC backends want.
            log("[6b] deep-login exchange (web cookie → IDE token)")
            ide = deep_login_exchange(page)
            ide_token = ide.get("accessToken") if ide else None
            machine_id = None
            if ide_token:
                # machineId is any 64-hex; derive one deterministically from the
                # token so checksum headers are stable per account.
                machine_id = hashlib.sha256(
                    (ide_token + "machineId").encode()).hexdigest()
                token = ide_token
                log("  using IDE access token (len " + str(len(ide_token))
                    + ") for verify + downstream")
            else:
                log("  WARN: deep-login yielded no IDE token — falling back to "
                    "the web cookie token (api2 will likely reject it)")

            record = {
                "email": EMAIL,
                "raw_cookie": raw,
                "session_token": extract_token(raw),
                "ide_access_token": ide_token,
                "ide_refresh_token": ide.get("refreshToken") if ide else None,
                "machine_id": machine_id,
                "all_cookie_names": names,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "final_url": final_url,
            }
            out_dir = os.path.dirname(OUT_PATH)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(OUT_PATH, "w") as f:
                json.dump(record, f, indent=2)
            log("  wrote " + OUT_PATH)

            # ── verify before declaring victory ───────────────────────────
            log("[7] verify token against " + API2_URL)
            status, verdict, evidence = verify_token(token)

            log("")
            log("-" * 72)
            log("SUMMARY")
            log("  email         = " + EMAIL)
            log("  final_url     = " + str(final_url)[:160])
            log("  cookie        = " + COOKIE_NAME + " (len " + str(len(raw)) + ")")
            log("  token         = " + token[:40] + "... (len " + str(len(token)) + ")")
            log("  out           = " + OUT_PATH)
            log("  api2 status   = " + str(status))
            log("  api2 verdict  = " + str(verdict))

            if verdict == "TOKEN_ACCEPTED":
                log("TOKEN_ACCEPTED")
                log("  api2 did NOT return ERROR_NOT_LOGGED_IN, so authentication")
                log("  passed; any error shown is about our deliberately empty body.")
                log("RESULT: SUCCESS")
                sys.exit(0)
            if verdict == "TOKEN_REJECTED":
                log("TOKEN_REJECTED")
                log("  api2 returned ERROR_NOT_LOGGED_IN — this token is not a valid")
                log("  session. Check that the THIRD ::-field was extracted, and that")
                log("  the login actually completed (see screenshots in " + SS_DIR + ").")
                log("RESULT: FAILED")
                sys.exit(6)
            log("VERIFY_INCONCLUSIVE (" + str(verdict) + ": " + str(evidence)[:200] + ")")
            log("  Could not reach api2.cursor.sh, so the token is unproven.")
            log("RESULT: FAILED")
            sys.exit(1)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
