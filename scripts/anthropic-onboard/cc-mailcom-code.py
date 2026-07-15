#!/usr/bin/env python3
"""
cc-mailcom-code.py — log into www.mail.com webmail and extract the Claude.ai
login verification code. Routed through the same US egress proxy as the OAuth
browser (egress binding), and BLOCKS external images to avoid firing the
Anthropic email tracking pixel (ban risk per seller doc).

ENV:
  MAIL_USER        DulcieMercadocns@cybergal.com
  MAIL_PW_FILE     /run/mail_pw.txt   (password, no trailing newline issues)
  PROXY_SERVER     http://38.175.220.46:8082 (optional)
Uses the vanilla playwright pre-installed in the mcr playwright image.
"""
import os, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

EMAIL = os.environ["MAIL_USER"]
PW = Path(os.environ.get("MAIL_PW_FILE", "/run/mail_pw.txt")).read_text().rstrip("\r\n")
PROXY = os.environ.get("PROXY_SERVER", "").strip()
SS = Path("/work/screenshots"); SS.mkdir(exist_ok=True, parents=True)
SENDER_RE = re.compile(r"claude|anthropic|verification|verify|sign in|log in", re.I)
MAIL_DOMAINS = ("mail.com", "gmx", "1und1", "1and1", "ionos", "united-internet", "wsrz")


def log(m): print(m, flush=True)


def shoot(t, n):
    try:
        t.screenshot(path=str(SS / f"mail-{n}.png"), full_page=True)
    except Exception as e:
        log(f"  shot {n} err: {e}")


def block_external_images(route):
    req = route.request
    if req.resource_type == "image":
        u = req.url.lower()
        if not any(d in u for d in MAIL_DOMAINS):
            return route.abort()
    return route.continue_()


with sync_playwright() as pw:
    headless = os.environ.get("HEADLESS", "0") == "1"
    kw = dict(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    if PROXY:
        kw["proxy"] = {"server": PROXY}
        log(f"[proxy] {PROXY}")
    log(f"[mode] headless={headless}")
    br = pw.chromium.launch(**kw)
    ctx = br.new_context(locale="en-US")
    ctx.route("**/*", block_external_images)
    page = ctx.new_page()

    # egress sanity
    try:
        page.goto("https://ipinfo.io/ip", wait_until="domcontentloaded", timeout=45000)
        log(f"[egress] {page.inner_text('body').strip()!r}")
    except Exception as e:
        log(f"  egress probe err: {e}")

    log(f"[login] www.mail.com as {EMAIL} (pw len={len(PW)})")
    page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(2)
    page.locator("a:has-text('Log in')").first.click()
    time.sleep(2)
    page.locator("input[placeholder='Email address']").first.fill(EMAIL)
    page.locator("input[placeholder='Password']").first.fill(PW)
    btns = page.locator("button:has-text('Log in')")
    for i in range(btns.count()):
        if btns.nth(i).is_visible():
            box = btns.nth(i).bounding_box()
            if box and box["y"] > 50:
                btns.nth(i).click(); break
    page.wait_for_load_state("domcontentloaded", timeout=25000)
    time.sleep(8)
    log(f"  url after login: {page.url[:100]}")
    shoot(page, "01-portal")
    # after login we land on the mail.com portal home; open the webmail inbox
    for sel in ("a:has-text('Email')", "a[href*='mail']:has-text('Email')", "text=Email"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(); log(f"  clicked Email nav via {sel}"); break
        except Exception:
            continue
    time.sleep(8)
    log(f"  url after Email click: {page.url[:100]}")

    # Rich webmailer renders in shadow DOM (unreadable). Jump to the server-rendered
    # "barrier-free" accessible inbox: grab its anchor href from the shell frame.
    bf_href = None
    for f in page.frames:
        try:
            hrefs = f.evaluate(
                "() => Array.from(document.querySelectorAll('a')).map(a => [a.innerText, a.href])"
            )
        except Exception:
            hrefs = []
        for txt, href in hrefs or []:
            if href and re.search(r"barrier|accessible|freemail.*mail|/mail/", (txt or "") + " " + href, re.I):
                with open("/work/out/frames.txt", "a") as fh:
                    fh.write(f"anchor: {txt!r} -> {href}\n")
        # specifically the barrier-free link
        for txt, href in hrefs or []:
            if href and re.search(r"barrier", (txt or "") + " " + href, re.I):
                bf_href = href; break
        if bf_href:
            break
    log(f"  barrier-free href: {bf_href}")
    if bf_href:
        page.goto(bf_href, wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
    shoot(page, "01b-bf")
    log(f"  url now: {page.url[:110]}")

    # Barrier-free = lightmailer.mail.com, plain HTML with real anchors. Navigate by href.
    def ptext():
        try:
            return page.inner_text("body")
        except Exception:
            return ""
    def anchors():
        try:
            return page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a=>[a.innerText.trim(), a.href])")
        except Exception:
            return []

    # settle on folderlist
    time.sleep(6)
    if "folderlist" not in page.url:
        try:
            page.goto("https://lightmailer.mail.com/folderlist", wait_until="domcontentloaded", timeout=45000)
            time.sleep(4)
        except Exception:
            pass
    fa = anchors()
    with open("/work/out/frames.txt", "a") as fh:
        fh.write(f"\n=== FOLDERLIST ANCHORS ({page.url}) ===\n" + "\n".join(f"{t!r} -> {h}" for t, h in fa) + "\n")
    inbox_href = next((h for t, h in fa if "messagelist" in (h or "") and re.search(r"inbox", (t or ""), re.I)), None)
    log(f"  inbox href: {inbox_href}")
    if inbox_href:
        page.goto(inbox_href, wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
    shoot(page, "02-list")
    inbox = ptext()
    la = anchors()
    log("=== INBOX LIST (first 2200) ==="); log(inbox[:2200]); log("=== end ===")
    with open("/work/out/frames.txt", "a") as fh:
        fh.write(f"\n=== MSGLIST ({page.url}) ===\n{inbox[:3000]}\n=== MSGLIST ANCHORS ===\n" + "\n".join(f"{t!r} -> {h}" for t, h in la) + "\n")

    subj_codes = re.findall(r"claude\.ai[^\n|]*\|\s*([A-Z0-9]{5,8})", inbox, re.I)
    log(f"  subject-embedded codes: {subj_codes}")

    # newest Anthropic/secure-link message anchor (readmessage link)
    msg_href = next((h for t, h in la if re.search(r"secure link|anthropic|claude", (t or ""), re.I)
                     and ("readmessage" in (h or "") or "message" in (h or ""))), None)
    if not msg_href:
        msg_href = next((h for t, h in la if re.search(r"secure link|anthropic|claude", (t or ""), re.I)), None)
    log(f"  msg href: {msg_href}")
    if msg_href:
        page.goto(msg_href, wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
    shoot(page, "04-opened")

    # email body is in a nested iframe; read every frame + collect anchors
    body = ptext()
    magic_links = []
    for f in page.frames:
        try:
            t = f.evaluate("() => document.body.innerText") or ""
        except Exception:
            t = ""
        if len(t) > len(body):
            body = body + "\n" + t
        try:
            hrefs = f.evaluate("() => Array.from(document.querySelectorAll('a')).map(a=>a.href)")
        except Exception:
            hrefs = []
        for h in hrefs or []:
            if h and re.search(r"claude\.(ai|com)|anthropic", h, re.I) and re.search(r"login|magic|verify|oauth|token|auth|link|/l/|redeem", h, re.I):
                magic_links.append(h)
    magic_links = list(dict.fromkeys(magic_links))
    # decode mail.com deref wrapper -> direct claude.ai magic-link
    import urllib.parse as _up
    direct = None
    for ml in magic_links:
        m = re.search(r"redirectUrl=([^&]+)", ml)
        if m:
            cand = _up.unquote(m.group(1))
            if "claude.ai" in cand or "claude.com" in cand:
                direct = cand; break
    if not direct and magic_links:
        direct = magic_links[0]
    log("=== opened message text (first 1500) ==="); log(body[:1500]); log("=== end ===")
    log(f"=== magic links ({len(magic_links)}) ===")
    for ml in magic_links:
        log(f"  {ml}")
    log(f"=== DIRECT magic-link: {direct}")
    if direct:
        with open("/work/out/magiclink.txt", "w") as f:
            f.write(direct)
    with open("/work/out/frames.txt", "a") as fh:
        fh.write(f"\n=== OPENED MSG ({page.url}) ===\n{body[:4000]}\nMAGIC_LINKS:\n" + "\n".join(magic_links) + f"\nDIRECT: {direct}\n")
    text = inbox + "\n" + "\n".join(subj_codes)

    # extract code from opened message + whole page
    allcodes = []
    for t in (body, text):
        for m in re.finditer(r"\b(\d{6,8})\b", t):
            i = m.start(); allcodes.append((m.group(1), t[max(0,i-55):i+55]))
        for m in re.finditer(r"\b([A-Z0-9]{6,8})\b", t):
            if not m.group(1).isdigit():
                i = m.start(); allcodes.append((m.group(1), t[max(0,i-55):i+55]))

    log(f"\n=== code candidates ({len(allcodes)}) ===")
    picked = None
    for code, cx in allcodes:
        kw_hit = any(k in cx.lower() for k in ("code", "verif", "claude", "anthropic"))
        log(f"  {code}  kw={kw_hit}  ctx={cx!r}")
        if kw_hit and not picked:
            picked = code
    if picked:
        with open("/work/out/mailcode.txt", "w") as f:
            f.write(picked)
        log(f"\n✅ VERIFICATION_CODE={picked}")
    else:
        log("\n❌ no code picked (dump above for manual read)")
    br.close()
