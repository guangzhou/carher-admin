#!/usr/bin/env python3
"""
cc-oauth-oneshot.py — mint a Claude Code OAuth code end-to-end in ONE browser.

Why one browser: the claude.ai magic-link is SINGLE USE. Splitting "request the
link" and "click the link" across two containers burns it — the first navigation
consumes it and every later attempt renders "This link has expired". So the same
browser context that asks for the link is the one that redeems it, in a 2nd tab
for Proton so the login tab keeps its pending-auth state.

ENV: CC_EMAIL, MAIL_USER, MAIL_PW_FILE, CC_OAUTH_URL,
     PROXY_SERVER, EXPECT_EXIT_IP
out: /work/out/code.txt, status.json, screenshots per step
"""
import json, os, re, time
from patchright.sync_api import sync_playwright

EMAIL = os.environ["CC_EMAIL"]
MAIL_USER = os.environ["MAIL_USER"]
MAIL_PW = open(os.environ["MAIL_PW_FILE"]).read().strip()
OAUTH_URL = os.environ["CC_OAUTH_URL"]
PROXY = os.environ.get("PROXY_SERVER", "").strip()
EXPECT_IP = os.environ.get("EXPECT_EXIT_IP", "").strip()

SS, OUT = "/work/screenshots", "/work/out"
for d in (SS, OUT):
    os.makedirs(d, exist_ok=True)

MAGIC_RE = re.compile(r"https://claude\.ai/magic-link#[A-Za-z0-9_\-:+/=%.]+")
CODE_RE = re.compile(r"\b([A-Za-z0-9_-]{40,}#[A-Za-z0-9_-]{16,})\b")


def log(m):
    print(m, flush=True)


def shot(page, name):
    try:
        page.screenshot(path=f"{SS}/{name}.png")
    except Exception:
        pass


def dump(page, name):
    try:
        t = page.inner_text("body") or ""
    except Exception as e:
        t = f"ERR {e}"
    try:
        open(f"{OUT}/{name}.txt", "w").write(f"url={page.url}\n---\n{t}")
    except Exception:
        pass
    return t


def dismiss(page):
    for pat in (r"accept|agree|got it|continue|close|skip|later|no thanks|不,谢谢|以后再说|关闭"):
        try:
            b = page.get_by_role("button", name=re.compile(pat, re.I))
            if b.count():
                b.first.click(timeout=2500)
                time.sleep(0.6)
        except Exception:
            pass


def request_magic_link(page):
    """Type the email on claude.ai and submit, so Anthropic mails a fresh link."""
    log(f"[oauth] goto authorize; will submit {EMAIL}")
    page.goto(OAUTH_URL, timeout=90000, wait_until="domcontentloaded")
    time.sleep(6)
    shot(page, "01-login")
    try:
        inp = page.get_by_placeholder(re.compile(r"enter your email", re.I))
        if inp.count() == 0:
            inp = page.locator("input[type='email'], input[name='email'], input")
        inp.first.click()
        time.sleep(0.3)
        page.keyboard.type(EMAIL, delay=45)
        time.sleep(0.5)
        btn = page.get_by_role("button", name=re.compile(r"continue with email", re.I))
        if btn.count():
            btn.first.click()
        else:
            page.keyboard.press("Enter")
    except Exception as e:
        log(f"  email submit err: {e}")
        return False
    time.sleep(8)
    shot(page, "02-submitted")
    dump(page, "02-submitted")
    log("  email submitted; link should be in flight")
    return True


def proton_login(page):
    log("[mail] login account.proton.me")
    page.goto("https://account.proton.me/login", timeout=90000, wait_until="domcontentloaded")
    time.sleep(5)
    page.locator("input#username, input[name='username']").first.fill(f"{MAIL_USER}@proton.me")
    p = page.locator("input#password, input[type='password']").first
    p.fill(MAIL_PW)
    p.press("Enter")
    # settle: Proton lands on /apps or straight into mail
    for i in range(24):
        time.sleep(5)
        u = page.url
        if "mail.proton.me" in u or "/u/" in u or "/apps" in u or "/dashboard" in u:
            log(f"  logged in at {u[:70]}")
            break
    dismiss(page)
    page.goto("https://mail.proton.me/u/0/inbox", timeout=90000, wait_until="domcontentloaded")
    time.sleep(10)
    dismiss(page)
    shot(page, "03-inbox")


def harvest_magic_link(page, deadline_s=300):
    """Find the newest claude.ai magic-link. Read HTML, not just rows: Proton's
    row locators drift between builds, but the link is in the frame HTML."""
    end = time.time() + deadline_s
    attempt = 0
    while time.time() < end:
        attempt += 1
        # 1) whole-page HTML across all frames (cheapest, no clicking)
        for fr in page.frames:
            try:
                h = fr.content()
            except Exception:
                continue
            m = MAGIC_RE.findall(h or "")
            if m:
                log(f"  magic-link in frame html (attempt {attempt})")
                return m[-1]
        # 2) click rows whose text smells like the Anthropic mail
        try:
            rows = page.locator(
                "[data-shortcut-target='item-container'], .item-container, [role='row'], "
                "[data-testid='message-item'], li[role='listitem'], a[href*='/inbox/']"
            )
            n = rows.count()
        except Exception:
            n = 0
        log(f"  attempt {attempt}: rows={n}")
        for i in range(min(n, 12)):
            try:
                rt = (rows.nth(i).inner_text() or "").lower()
            except Exception:
                continue
            if not any(k in rt for k in ("claude", "anthropic", "secure link", "sign in", "log in")):
                continue
            try:
                rows.nth(i).click(timeout=8000)
                time.sleep(5)
            except Exception:
                continue
            for fr in page.frames:
                try:
                    h = fr.content()
                except Exception:
                    continue
                m = MAGIC_RE.findall(h or "")
                if m:
                    log(f"  magic-link in opened mail row {i}")
                    return m[-1]
        # 3) refresh: the mail may simply not have landed yet
        try:
            page.reload(timeout=60000, wait_until="domcontentloaded")
            time.sleep(8)
            dismiss(page)
        except Exception:
            pass
    return None


def find_code(page):
    t = ""
    try:
        t = page.inner_text("body") or ""
    except Exception:
        pass
    m = CODE_RE.search(t)
    if m:
        return m.group(1)
    u = page.url
    # NOTE: the authorize URL itself carries a literal `code=true` flag. Matching
    # that as "the code" reports success while holding nothing — only accept a
    # real code, which is long and lives on the /callback redirect.
    if "/oauth/code/callback" in u or "platform.claude.com" in u:
        m = re.search(r"[?&]code=([A-Za-z0-9_\-#]{20,})", u)
        if m and m.group(1) != "true":
            return m.group(1)
    return None


with sync_playwright() as pw:
    kw = dict(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage",
                                    "--disable-blink-features=AutomationControlled"])
    if PROXY:
        kw["proxy"] = {"server": PROXY}
        log(f"[proxy] {PROXY}")
    br = pw.chromium.launch(**kw)
    ctx = br.new_context(viewport={"width": 1366, "height": 950}, locale="en-US")

    if EXPECT_IP:
        p0 = ctx.new_page()
        p0.goto("https://api.ipify.org", timeout=60000)
        ip = (p0.inner_text("body") or "").strip()
        open(f"{OUT}/exit_ip.txt", "w").write(ip)
        log(f"[egress] exit IP={ip!r} expect={EXPECT_IP!r}")
        p0.close()
        if ip != EXPECT_IP:
            log("❌ EGRESS_MISMATCH — refusing to mint a token on the wrong IP")
            json.dump({"done": False, "note": f"egress {ip}"}, open(f"{OUT}/status.json", "w"))
            br.close()
            raise SystemExit(2)

    login_tab = ctx.new_page()
    if not request_magic_link(login_tab):
        raise SystemExit(3)

    mail_tab = ctx.new_page()
    proton_login(mail_tab)
    link = harvest_magic_link(mail_tab)
    if not link:
        log("❌ NO_MAGICLINK")
        json.dump({"done": False, "note": "no magiclink"}, open(f"{OUT}/status.json", "w"))
        br.close()
        raise SystemExit(4)
    open(f"{OUT}/magiclink.txt", "w").write(link)
    log(f"✅ MAGICLINK={link}")

    # Redeem it exactly once, in the tab that asked for it.
    log("[redeem] consuming link in the login tab")
    login_tab.bring_to_front()
    login_tab.goto(link, timeout=90000, wait_until="domcontentloaded")
    # POLL, don't sleep-then-judge. The magic-link page sits on "Loading..." for
    # a while before it swaps the session in and redirects; a fixed sleep reads
    # that spinner as the final state and throws away a link that actually worked.
    t = ""
    for i in range(30):
        time.sleep(5)
        t = dump(login_tab, "04-redeemed")
        u = login_tab.url
        low = t.lower().strip()
        log(f"  redeem poll {i}: url={u[:70]} body={low[:40]!r}")
        if "expired" in low:
            break
        if "magic-link" not in u and low and "loading" not in low:
            log(f"  redeem settled at {u[:80]}")
            break
    shot(login_tab, "04-redeemed")
    if "expired" in t.lower():
        log("❌ LINK_EXPIRED at redeem — the link was consumed before this point")
        json.dump({"done": False, "note": "expired"}, open(f"{OUT}/status.json", "w"))
        br.close()
        raise SystemExit(5)

    # The redeem lands directly on the consent screen ("Claude Code would like to
    # connect to your account"). Click Authorize there before re-navigating --
    # a fresh goto to OAUTH_URL drops us back to /login and loses the consent.
    for i in range(8):
        try:
            btn = login_tab.get_by_role(
                "button", name=re.compile(r"^\s*(authorize|allow|approve|connect)", re.I))
            if btn.count():
                log(f"  consent screen: clicking Authorize (try {i})")
                btn.first.click()
                for _ in range(12):
                    time.sleep(4)
                    shot(login_tab, f"07-consent-{i}")
                    ct = dump(login_tab, f"07-consent-{i}")
                    c = find_code(login_tab)
                    if c:
                        log(f"✅ CODE={c}")
                        open(f"{OUT}/code.txt", "w").write(c)
                        json.dump({"done": True, "code": c}, open(f"{OUT}/status.json", "w"))
                        br.close()
                        raise SystemExit(0)
                    if "expired" in ct.lower():
                        break
                break
        except SystemExit:
            raise
        except Exception as e:
            log(f"  consent click err: {e}")
        time.sleep(4)

    # Fallback: either already at the code, or we must re-hit authorize.
    for i in range(6):
        code = find_code(login_tab)
        if code:
            log(f"✅ CODE={code}")
            open(f"{OUT}/code.txt", "w").write(code)
            json.dump({"done": True, "code": code}, open(f"{OUT}/status.json", "w"))
            break
        log(f"[authorize] retry {i}: goto authorize url")
        login_tab.goto(OAUTH_URL, timeout=90000, wait_until="domcontentloaded")
        time.sleep(8)
        shot(login_tab, f"05-authorize-{i}")
        body = dump(login_tab, f"05-authorize-{i}")
        try:
            btn = login_tab.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
            if btn.count():
                log("  clicking Authorize")
                btn.first.click()
                time.sleep(9)
                shot(login_tab, f"06-after-authorize-{i}")
                dump(login_tab, f"06-after-authorize-{i}")
        except Exception as e:
            log(f"  authorize click err: {e}")
        if "expired" in body.lower() or "sign in" in body.lower():
            log("  still unauthenticated")
        time.sleep(3)
    else:
        log("❌ NO_CODE after retries")
        json.dump({"done": False, "note": "no code"}, open(f"{OUT}/status.json", "w"))

    br.close()
