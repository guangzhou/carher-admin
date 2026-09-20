#!/usr/bin/env python3
"""
cc-proton-code.py — fetch the claude.ai magic-link out of a Proton Mail inbox.

Proton has no usable IMAP (Bridge is a paid local daemon, and imap.protonmail.ch
is not publicly resolvable), and mail bodies are E2E encrypted — so the only way
in is a real browser, which decrypts client-side. That's what this does.

Counterpart of cc-mailcom-code.py (mail.com/lightmailer). Same contract:
  in : MAIL_USER / MAIL_PW_FILE / PROXY_SERVER
  out: /work/out/magiclink.txt   (the direct https://claude.ai/magic-link#... URL)
       /work/screenshots/mail-*.png on every step, for post-mortem

MODE=probe  -> only log in and report whether the mailbox opens (no link hunt).
              Use this FIRST, before triggering any Anthropic email: a mailbox we
              can't read makes the whole magic-link flow unrunnable.
MODE=fetch  -> log in and hunt for the newest claude.ai magic-link (default).

Env:
  MAIL_USER      ethancccccai@proton.me   (bare username also accepted)
  MAIL_PW_FILE   /run/mail_pw.txt         (password, trailing newline stripped)
  PROXY_SERVER   socks5://10.68.13.236:17890
  EXPECT_EXIT_IP 192.204.59.143           (hard gate; abort on mismatch)
  MODE           probe | fetch
  SINCE_TS       only accept a link from a mail newer than this epoch (fetch)
  MAX_WAIT       seconds to keep polling the inbox for the mail (default 240)
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from patchright.sync_api import sync_playwright

MAIL_USER = os.environ["MAIL_USER"]
PW = Path(os.environ.get("MAIL_PW_FILE", "/run/mail_pw.txt")).read_text().rstrip("\r\n")
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()
EXPECT_EXIT_IP = os.environ.get("EXPECT_EXIT_IP", "").strip()
MODE = os.environ.get("MODE", "fetch").strip()
SINCE_TS = float(os.environ.get("SINCE_TS", "0"))
MAX_WAIT = float(os.environ.get("MAX_WAIT", "240"))

SS = "/work/screenshots"
OUT = "/work/out"
os.makedirs(SS, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

# Proton wants the full address in the login form; a bare username also works but
# normalise so the screenshots/logs are unambiguous.
LOGIN_USER = MAIL_USER if "@" in MAIL_USER else f"{MAIL_USER}@proton.me"


def log(m):
    print(m, flush=True)


def shoot(page, tag):
    try:
        page.screenshot(path=f"{SS}/mail-{tag}.png", full_page=False)
    except Exception as e:
        log(f"  shoot {tag} err: {e}")


def write_status(**kw):
    with open(f"{OUT}/mail_status.json", "w") as f:
        json.dump(kw, f)


def dismiss_overlays(page):
    """Proton throws cookie banners / 'get the app' / onboarding modals."""
    for pat in (r"accept all", r"got it", r"^next$", r"^skip", r"^close",
                r"start using proton", r"^continue$", r"maybe later", r"^dismiss"):
        try:
            b = page.get_by_role("button", name=re.compile(pat, re.I))
            if b.count() > 0 and b.first.is_visible():
                b.first.click(timeout=4000)
                log(f"  dismissed: {pat}")
                time.sleep(1.2)
        except Exception:
            pass


MAILBOX_MARKERS = ("inbox", "drafts", "sent", "archive", "spam", "trash")


def wait_for_mailbox(page, timeout=120):
    """Poll until the Mail SPA has actually painted the mailbox.

    mail.proton.me returns its shell immediately and hydrates later, so
    checking content right after navigation sees a blank page. Poll for folder
    chrome (Inbox/Drafts/Sent/...) and settle on the first hit.
    """
    deadline = time.time() + timeout
    i = 0
    while time.time() < deadline:
        body = ""
        err = ""
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        # Dump what we actually see: guessing at marker strings wasted a whole
        # run when the mailbox HAD rendered but this check said otherwise.
        try:
            with open(f"{OUT}/body_dump.txt", "w") as f:
                f.write(f"len={len(body)} err={err}\nurl={page.url}\n---\n{body[:4000]}")
        except Exception:
            pass
        hits = [m for m in MAILBOX_MARKERS if m in body]
        if len(hits) >= 2:
            log(f"  mailbox rendered (markers: {','.join(hits[:4])})")
            time.sleep(3)
            return True
        if i % 5 == 0:
            log(f"  waiting for mailbox to render… ({int(deadline - time.time())}s left)")
            shoot(page, f"03-render-{i}")
        i += 1
        time.sleep(3)
    return False


def do_login(page):
    log(f"[login] account.proton.me as {LOGIN_USER}")
    page.goto("https://account.proton.me/login", wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)
    dismiss_overlays(page)
    shoot(page, "01-login")

    # Username field: Proton uses #username, sometimes inside an iframe-free form.
    try:
        u = page.locator("input#username, input[name='username'], input[autocomplete='username']").first
        u.wait_for(state="visible", timeout=25000)
        u.click()
        u.fill(LOGIN_USER)
    except Exception as e:
        log(f"  ❌ username field not found: {e}")
        shoot(page, "01b-no-username")
        return False

    # Password may be on the same step or a second step.
    try:
        p = page.locator("input#password, input[name='password'], input[type='password']").first
        if p.count() > 0 and p.is_visible():
            p.click()
            p.fill(PW)
        else:
            page.keyboard.press("Enter")
            time.sleep(3)
            p = page.locator("input#password, input[name='password'], input[type='password']").first
            p.wait_for(state="visible", timeout=20000)
            p.click()
            p.fill(PW)
    except Exception as e:
        log(f"  ❌ password field not found: {e}")
        shoot(page, "01c-no-password")
        return False

    shoot(page, "02-filled")
    try:
        btn = page.get_by_role("button", name=re.compile(r"sign in|log in", re.I))
        if btn.count() > 0:
            btn.first.click()
        else:
            page.keyboard.press("Enter")
    except Exception:
        page.keyboard.press("Enter")

    # Settle: Proton redirects login -> mail.proton.me/u/0/inbox, but it can also
    # land on account.proton.me/apps (the app picker) — that IS a logged-in state,
    # so treat it as success and navigate to Mail ourselves.
    for i in range(40):
        time.sleep(3)
        url = page.url
        if "mail.proton.me" in url or "/u/" in url:
            log(f"  logged in, url={url[:100]}")
            shoot(page, "03-landed")
            return True
        # NOTE: match on PATH only. "/account" as a substring of the whole URL
        # also matches the host "account.proton.me/login" — i.e. still logged
        # OUT — which silently reported success. Parse the path explicitly.
        from urllib.parse import urlparse
        path = urlparse(url).path or "/"
        if path.startswith(("/apps", "/dashboard", "/u/", "/mail")):
            log(f"  logged in (landed path {path}), going to Mail")
            try:
                page.goto("https://mail.proton.me/u/0/inbox",
                          wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"  mail goto err: {e}")
            # Proton Mail is a heavy SPA: domcontentloaded fires while the page
            # is still blank. Poll for actual mailbox chrome instead of sleeping
            # a fixed amount, or we assert on an empty page.
            if not wait_for_mailbox(page):
                log("  mailbox did not render in time")
            shoot(page, "03-landed")
            return True
        body = ""
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            pass
        # hard failures worth reporting instead of spinning the full 2 min
        for bad, why in (
            ("incorrect login credentials", "BAD_CREDENTIALS"),
            ("password is incorrect", "BAD_CREDENTIALS"),
            ("account does not exist", "NO_SUCH_ACCOUNT"),
            ("too many failed", "RATE_LIMITED"),
            ("account has been disabled", "ACCOUNT_DISABLED"),
            ("suspended", "ACCOUNT_SUSPENDED"),
        ):
            if bad in body:
                log(f"  ❌ {why}: matched {bad!r}")
                shoot(page, "03b-login-failed")
                write_status(ok=False, note=why, url=page.url[:200])
                return False
        if any(k in body for k in ("verification code", "two-factor", "authenticator", "2fa")):
            log("  ❌ 2FA/verification challenge on the mailbox itself")
            shoot(page, "03c-mail-2fa")
            write_status(ok=False, note="MAIL_2FA_REQUIRED", url=page.url[:200])
            return False
        if i % 5 == 0:
            shoot(page, f"03-settle-{i}")
            log(f"  settling.. url={url[:90]}")
    log("  ❌ login did not settle into the mailbox")
    shoot(page, "03d-timeout")
    return False


MAGIC_RE = re.compile(r"https://claude\.ai/magic-link#[A-Za-z0-9_\-:+/=%.]+")
REDIR_RE = re.compile(r"redirectUrl=([^&\"'\s]+)")


def extract_magic(text):
    m = MAGIC_RE.search(text or "")
    if m:
        return m.group(0)
    m = REDIR_RE.search(text or "")
    if m:
        import urllib.parse
        cand = urllib.parse.unquote(m.group(1))
        if "claude.ai/magic-link" in cand:
            return cand
    return None


def hunt_magiclink(page, ctx):
    """Open the newest Anthropic mail and pull the magic-link out of it."""
    deadline = time.time() + MAX_WAIT
    seen_rows = 0
    while time.time() < deadline:
        try:
            page.goto("https://mail.proton.me/u/0/inbox", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"  inbox goto err: {e}")
        time.sleep(6)
        dismiss_overlays(page)
        shoot(page, "10-inbox")

        # Whole-inbox scrape first: the link sometimes shows in the preview text.
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        got = extract_magic(body)
        if got:
            log("  magic-link found in inbox listing")
            return got

        # Otherwise open candidate mails. Proton rows are role=row / .item-container.
        rows = page.locator("[data-shortcut-target='item-container'], .item-container, [role='row']")
        n = rows.count()
        if n != seen_rows:
            log(f"  inbox rows: {n}")
            seen_rows = n
        opened = 0
        for i in range(min(n, 8)):
            try:
                row = rows.nth(i)
                rt = (row.inner_text() or "").lower()
            except Exception:
                continue
            if not any(k in rt for k in ("claude", "anthropic", "secure link", "sign in", "log in")):
                continue
            try:
                row.click(timeout=8000)
            except Exception:
                continue
            opened += 1
            time.sleep(5)
            shoot(page, f"11-mail-{i}")

            # Body text (Proton decrypts client-side once rendered).
            try:
                mb = page.inner_text("body")
            except Exception:
                mb = ""
            got = extract_magic(mb)
            if got:
                log(f"  magic-link found in mail row {i} text")
                return got

            # The link is usually behind an <a>; Proton renders mail in an iframe.
            for fr in page.frames:
                try:
                    fhtml = fr.content()
                except Exception:
                    continue
                got = extract_magic(fhtml)
                if got:
                    log(f"  magic-link found in mail row {i} iframe html")
                    return got
                try:
                    hrefs = fr.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)")
                except Exception:
                    hrefs = []
                for h in hrefs:
                    got = extract_magic(h)
                    if got:
                        log(f"  magic-link found in mail row {i} anchor")
                        return got
            # Also scan the top-level HTML in case it's not framed.
            try:
                got = extract_magic(page.content())
            except Exception:
                got = None
            if got:
                log(f"  magic-link found in mail row {i} page html")
                return got
        log(f"  no link yet (opened {opened} candidate mails); waiting…")
        time.sleep(12)
    return None


with sync_playwright() as pw:
    launch_kwargs = dict(headless=os.environ.get("HEADLESS", "0") == "1", args=[
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ])
    if PROXY_SERVER:
        launch_kwargs["proxy"] = {"server": PROXY_SERVER}
        log(f"[proxy] browser via {PROXY_SERVER}")
    br = pw.chromium.launch(**launch_kwargs)
    ctx = br.new_context(
        viewport={"width": 1366, "height": 950}, locale="en-US",
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"),
    )

    # ── Egress gate: never touch the mailbox from an unexpected IP. Proton +
    #    Anthropic must both see the SAME US exit as the served traffic.
    if EXPECT_EXIT_IP:
        pr = ctx.new_page()
        eip = ""
        try:
            pr.goto("https://ipinfo.io/ip", wait_until="domcontentloaded", timeout=45000)
            eip = (pr.inner_text("body") or "").strip()
        except Exception as e:
            log(f"  exit-ip probe err: {e}")
        pr.close()
        log(f"[egress] exit IP = {eip!r} (expect {EXPECT_EXIT_IP!r})")
        with open(f"{OUT}/mail_exit_ip.txt", "w") as f:
            f.write(eip)
        if eip != EXPECT_EXIT_IP:
            write_status(ok=False, note=f"EGRESS_MISMATCH exit={eip}")
            log("❌ EGRESS MISMATCH — aborting before any mailbox contact")
            br.close()
            sys.exit(2)

    page = ctx.new_page()
    ok = do_login(page)
    if not ok:
        log("❌ MAIL_LOGIN_FAILED")
        if not Path(f"{OUT}/mail_status.json").exists():
            write_status(ok=False, note="MAIL_LOGIN_FAILED", url=page.url[:200])
        br.close()
        sys.exit(3)

    dismiss_overlays(page)
    if MODE == "probe":
        # Don't trust "login didn't error" as proof. Assert we are really in a
        # mailbox: the URL must be on mail.proton.me AND the page must show
        # mailbox chrome (an Inbox/folder affordance). Otherwise it's a FAIL.
        from urllib.parse import urlparse
        u = urlparse(page.url)
        on_mail = "mail.proton.me" in (u.netloc or "")
        body = ""
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            pass
        has_mailbox = len([m for m in MAILBOX_MARKERS if m in body]) >= 2
        still_login = "sign in" in body or "password" in body
        shoot(page, "99-probe-final")
        if on_mail and has_mailbox and not still_login:
            log("✅ MAILBOX_OK (reached inbox)")
            write_status(ok=True, note="MAILBOX_OK", url=page.url[:200])
            br.close()
            sys.exit(0)
        log(f"❌ MAILBOX_NOT_REACHED url={page.url[:120]} "
            f"on_mail={on_mail} has_mailbox={has_mailbox} still_login={still_login}")
        write_status(ok=False, note="MAILBOX_NOT_REACHED", url=page.url[:200])
        br.close()
        sys.exit(3)

    link = hunt_magiclink(page, ctx)
    shoot(page, "99-final")
    if link:
        with open(f"{OUT}/magiclink.txt", "w") as f:
            f.write(link)
        write_status(ok=True, note="MAGICLINK_FOUND", link=link[:80])
        log(f"✅ MAGICLINK={link}")
    else:
        write_status(ok=False, note="NO_MAGICLINK", url=page.url[:200])
        log("❌ no magic-link found")
    br.close()
    sys.exit(0 if link else 4)
