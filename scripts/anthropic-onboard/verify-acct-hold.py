#!/usr/bin/env python3
"""
verify-acct-hold.py — 验证 Anthropic 卖号是否被 on hold

不做 OAuth, 只走到 claude.ai 主页 / hold banner 阶段就停下截图.

三段串行(非嵌套, 规避 patchright asyncio loop + vanilla sync API 冲突):
  1. patchright: 打开 claude.ai/login, 输入 email 触发 magic-link
  2. vanilla playwright: 登 mail.com 抓最新 claude.ai magic-link href
  3. patchright: goto magic-link, 等加载, 抓 body 文本判断是否含 "on hold"

ENV:
  CC_EMAIL   e.g. Mayo_Haneynns@therapist.net
  MAIL_PW    mail.com webmail 密码

OUTPUT (stdout 最后一行):
  HOLD_STATUS=YES  / HOLD_STATUS=NO  / HOLD_STATUS=UNKNOWN
"""
import os, re, time, sys
from urllib.parse import urlparse, parse_qs, unquote
from patchright.sync_api import sync_playwright as patchright_pw
from playwright.sync_api import sync_playwright as vanilla_pw

EMAIL = os.environ["CC_EMAIL"]
MAIL_PW = os.environ["MAIL_PW"]
SS = "/work/screenshots"
os.makedirs(SS, exist_ok=True)

HOLD_PATTERNS = [
    r"account is on hold",
    r"your account is on hold",
    r"on hold",
    r"unusual activity",
    r"we put your account on hold",
    r"account suspended",
    r"account has been suspended",
    r"account.*disabled",
]


def shoot(p, name):
    try:
        p.screenshot(path=f"{SS}/{name}.png")
        print(f"  shot: {name}.png", flush=True)
    except Exception as e:
        print(f"  shot {name} failed: {e}", flush=True)


def wait_past_turnstile(page, max_wait=90):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        title = (page.title() or "").lower()
        body = page.content().lower()[:3000]
        if "just a moment" not in title and "performing security verification" not in body:
            return True
        for fr in page.frames:
            url = (fr.url or "").lower()
            if "challenges.cloudflare" in url or "turnstile" in url:
                try:
                    cb = fr.locator("input[type='checkbox']").first
                    if cb.count() > 0 and cb.is_visible():
                        print(f"  [turnstile] clicking", flush=True)
                        cb.click(force=True)
                        time.sleep(4)
                except Exception:
                    pass
        time.sleep(3)
    return False


def get_mail_frame(page, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for fr in page.frames:
            if fr.name == "mail":
                return fr
        time.sleep(2)
    return None


# ── Step 1 (patchright): 触发 magic-link ─────────────────────────────
print("[1] patchright: trigger magic-link on claude.ai/login", flush=True)
with patchright_pw() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    page = ctx.new_page()
    page.goto("https://claude.ai/login", wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    if "moment" in (page.title() or "").lower():
        wait_past_turnstile(page, 90)
        time.sleep(3)
    shoot(page, "01-claude-login")

    page.wait_for_selector("input[type='email'], input[name='email']", timeout=15000)
    page.locator("input[type='email'], input[name='email']").first.click()
    page.keyboard.type(EMAIL, delay=70)
    btn = page.get_by_role("button", name=re.compile(r"continue with email", re.I))
    if btn.count() == 0:
        btn = page.get_by_role("button", name=re.compile(r"^continue$|next", re.I))
    if btn.count() > 0:
        btn.first.click()
    else:
        page.keyboard.press("Enter")
    time.sleep(6)
    shoot(page, "02-after-email-submit")
    br.close()

# ── Step 2 (vanilla playwright): 取 mail.com 里的 magic-link ─────────
print("[2] vanilla playwright: fetch magic-link from mail.com", flush=True)
secure_link = None
with vanilla_pw() as vpw:
    mb = vpw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    mctx = mb.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    mp = mctx.new_page()
    mp.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    shoot(mp, "03-mailcom-landing")

    for label_pat in [r"accept all", r"agree", r"i agree", r"同意", r"接受", r"^ok$"]:
        try:
            b = mp.get_by_role("button", name=re.compile(label_pat, re.I))
            if b.count() > 0 and b.first.is_visible():
                print(f"  dismissing cookie banner via '{label_pat}'", flush=True)
                b.first.click()
                time.sleep(2)
                break
        except Exception:
            pass

    login_clicked = False
    for sel in ["a:has-text('Log in')", "button:has-text('Log in')", "a[href*='login']"]:
        try:
            cand = mp.locator(sel)
            for i in range(cand.count()):
                el = cand.nth(i)
                if el.is_visible():
                    print(f"  clicking login via '{sel}' #{i}", flush=True)
                    el.click()
                    login_clicked = True
                    break
            if login_clicked:
                break
        except Exception:
            pass
    time.sleep(5)

    try:
        mp.wait_for_selector(
            "input[placeholder='Email address'], input[placeholder*='Email' i], "
            "input[type='email'], input[name='username']",
            timeout=20000,
        )
    except Exception:
        shoot(mp, "04-mailcom-no-form")
        mb.close()
        print("HOLD_STATUS=UNKNOWN")
        print("REASON=mailcom_no_login_form")
        sys.exit(1)

    e_in = mp.locator("input[placeholder='Email address']").first
    if e_in.count() == 0:
        e_in = mp.locator("input[type='email']").first
    e_in.click()
    e_in.fill(EMAIL)

    p_in = mp.locator("input[placeholder='Password']").first
    if p_in.count() == 0:
        p_in = mp.locator("input[type='password']").first
    p_in.click()
    p_in.fill(MAIL_PW)
    shoot(mp, "04-mailcom-filled")

    btns = mp.locator("button:has-text('Log in'), input[type='submit']")
    clicked = False
    for i in range(btns.count()):
        b = btns.nth(i)
        if b.is_visible():
            box = b.bounding_box()
            if box and box["y"] > 50:
                b.click()
                clicked = True
                break
    if not clicked:
        mp.keyboard.press("Enter")
    time.sleep(10)
    shoot(mp, "05-mailcom-after-login")
    print(f"  after-login url: {mp.url[:120]}", flush=True)

    # 等邮件到达 + 找 mail frame
    time.sleep(45)
    fr = get_mail_frame(mp, timeout=30)
    if not fr:
        body = mp.inner_text("body")[:1000]
        print(f"  ⚠️ no mail frame. body: {body!r}", flush=True)
        shoot(mp, "06-no-mail-frame")
        mb.close()
        print("HOLD_STATUS=UNKNOWN")
        print("REASON=mailcom_no_mail_frame")
        sys.exit(1)
    shoot(mp, "06-mailcom-inbox")

    SENDER_RE = re.compile(r"anthropic|claude|secure link", re.I)
    for attempt in range(5):
        if secure_link:
            break
        print(f"  [scan {attempt+1}/5]", flush=True)
        try:
            text = fr.evaluate("() => document.body.innerText")
        except Exception:
            time.sleep(8)
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        target = None
        for ln in lines:
            if SENDER_RE.search(ln):
                target = ln
                print(f"    hint: {ln[:80]!r}", flush=True)
                break
        if not target:
            time.sleep(15)
            try:
                refresh = fr.locator("button:has-text('Refresh'), [title*='Refresh' i]").first
                if refresh.count() > 0 and refresh.is_visible():
                    refresh.click()
                    time.sleep(5)
            except Exception:
                pass
            continue
        try:
            fr.get_by_text(target, exact=False).first.click()
            time.sleep(6)
        except Exception:
            time.sleep(8)
            continue
        for f in mp.frames:
            try:
                for a in f.locator("a").all():
                    try:
                        href = a.get_attribute("href") or ""
                    except Exception:
                        continue
                    h = href.lower()
                    if "claude" in h and ("login" in h or "magic" in h or "verify" in h or "token=" in h):
                        secure_link = href
                        print(f"    href: {href[:120]}", flush=True)
                        break
                if secure_link:
                    break
                body = f.evaluate("() => document.body.innerText")
                m = re.search(r"(https?://[^\s\"'<>]*claude[^\s\"'<>]*(?:login|magic|verify|token=)[^\s\"'<>]*)", body, re.I)
                if m:
                    secure_link = m.group(1)
                    print(f"    text-extract: {secure_link[:120]}", flush=True)
                    break
            except Exception:
                continue
        if not secure_link:
            time.sleep(15)
    shoot(mp, "07-mailcom-final")
    mb.close()

if not secure_link:
    print("HOLD_STATUS=UNKNOWN")
    print("REASON=no_magic_link_received")
    sys.exit(1)

# mail.com 把外链包装成 deref-mail.com?redirectUrl=<urlencoded real_url>
# 直接解出真实 claude.ai magic-link, 避免在 deref 中转页卡住
if "deref-mail.com" in secure_link or "redirectUrl=" in secure_link:
    try:
        qs = parse_qs(urlparse(secure_link).query)
        if "redirectUrl" in qs:
            real = unquote(qs["redirectUrl"][0])
            print(f"  unwrapped deref: {real[:160]}", flush=True)
            secure_link = real
    except Exception as e:
        print(f"  deref unwrap failed: {e}", flush=True)

# ── Step 3 (patchright): open magic-link + 抓 hold banner ────────────
print(f"[3] patchright: open magic-link, look for hold banner", flush=True)
with patchright_pw() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    page = ctx.new_page()
    page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    if "moment" in (page.title() or "").lower():
        wait_past_turnstile(page, 60)
        time.sleep(3)
    shoot(page, "08-after-magic-link")
    final_url = page.url
    body = page.inner_text("body")
    print(f"  final_url: {final_url[:200]}", flush=True)
    print(f"  body (first 2000 chars):", flush=True)
    print(body[:2000], flush=True)

    matched = []
    for pat in HOLD_PATTERNS:
        if re.search(pat, body, re.I):
            matched.append(pat)
    if matched:
        print(f"\nHOLD_STATUS=YES")
        print(f"MATCHED_PATTERNS={matched}")
    else:
        # 若到达 /new 或 /chats 主页, 说明账号正常
        if any(x in final_url for x in ("/new", "/projects", "/chats", "/onboarding")):
            print(f"\nHOLD_STATUS=NO")
            print(f"REASON=reached_app_page url={final_url}")
        else:
            print(f"\nHOLD_STATUS=UNKNOWN")
            print(f"REASON=no_hold_match_but_not_app_page url={final_url}")
    br.close()
