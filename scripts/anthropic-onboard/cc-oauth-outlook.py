#!/usr/bin/env python3
"""
cc-oauth-outlook.py — Outlook (live.com) variant of cc-oauth-full.py.

Differences from Gmail flow:
  - Step 3: login.live.com (no TOTP — relies on email+password only)
  - Step 4: outlook.live.com/mail/0/inbox  (search "Anthropic")

ENV:
  CC_EMAIL        xxx@outlook.com
  MAIL_PW         Outlook 登录密码
  CC_OAUTH_URL    https://claude.com/cai/oauth/authorize?...

Same Docker invocation as cc-oauth-full.py.
"""
import os, re, time
from patchright.sync_api import sync_playwright

EMAIL = os.environ["CC_EMAIL"]
MAIL_PW = os.environ["MAIL_PW"]
OAUTH_URL = os.environ["CC_OAUTH_URL"]
SS = "/work/screenshots"
os.makedirs(SS, exist_ok=True)


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


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width":1280,"height":800}, locale="en-US")
    claude_page = ctx.new_page()

    # ── Step 1: 打开 OAuth URL,过 Turnstile ──────────────────────
    print(f"[1] Open OAuth URL on claude_page", flush=True)
    claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 90)
        time.sleep(3)
    shoot(claude_page, "01-claude-landing")

    # ── Step 2: 填 email + Continue with email ────────────────────
    print(f"[2] Fill email + trigger magic link", flush=True)
    claude_page.wait_for_selector("input[type='email'], input[name='email']", timeout=15000)
    claude_page.locator("input[type='email'], input[name='email']").first.click()
    claude_page.keyboard.type(EMAIL, delay=70)
    btn = claude_page.get_by_role("button", name=re.compile(r"continue with email", re.I))
    if btn.count() == 0:
        btn = claude_page.get_by_role("button", name=re.compile(r"^continue$|next", re.I))
    if btn.count() > 0:
        btn.first.click()
    else:
        claude_page.keyboard.press("Enter")
    time.sleep(6)
    shoot(claude_page, "02-claude-after-email")

    # ── Step 3: 新 tab 登 Outlook (live.com) ──────────────────────
    print(f"[3] Open Outlook login", flush=True)
    mail_page = ctx.new_page()
    mail_page.goto("https://login.live.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    shoot(mail_page, "03-outlook-landing")

    # email field (name=loginfmt)
    mail_page.wait_for_selector("input[name='loginfmt'], input[type='email']", timeout=15000)
    mail_page.locator("input[name='loginfmt'], input[type='email']").first.click()
    mail_page.keyboard.type(EMAIL, delay=80)
    nxt = mail_page.locator("input[type='submit'], button[type='submit']").first
    nxt.click()
    time.sleep(6)
    shoot(mail_page, "04-outlook-after-email")
    print(f"  after-email url: {mail_page.url[:120]}", flush=True)

    # Outlook now defaults to passwordless ("Verify your email" code page).
    # The page exposes a "Use your password" link to switch back to password login.
    body_after_email = mail_page.inner_text("body")[:600].lower()
    if "use your password" in body_after_email or "verify your email" in body_after_email:
        print(f"  [3a] passwordless prompt detected, switching to password login", flush=True)
        link = mail_page.get_by_text(re.compile(r"use your password", re.I))
        if link.count() == 0:
            link = mail_page.locator("a:has-text('Use your password'), button:has-text('Use your password')")
        if link.count() > 0 and link.first.is_visible():
            link.first.click()
            time.sleep(5)
            shoot(mail_page, "04a-outlook-switch-to-pw")
        else:
            shoot(mail_page, "04b-outlook-no-pw-link")
            print(f"  ⚠️ 'Use your password' link not clickable. body: {body_after_email!r}", flush=True)

    # password field (name=passwd)
    try:
        mail_page.wait_for_selector("input[name='passwd'], input[type='password']", timeout=20000)
    except Exception:
        body = mail_page.inner_text("body")[:1000]
        print(f"  ⚠️ password field not found. body: {body!r}", flush=True)
        shoot(mail_page, "04b-outlook-no-pw")
        raise SystemExit("OUTLOOK_NO_PASSWORD_FIELD")

    mail_page.locator("input[name='passwd'], input[type='password']").first.click()
    mail_page.keyboard.type(MAIL_PW, delay=80)
    mail_page.locator("input[type='submit'], button[type='submit']").first.click()
    time.sleep(10)
    shoot(mail_page, "05-outlook-after-pw")
    print(f"  after-pw url: {mail_page.url[:120]}", flush=True)
    print(f"  after-pw body excerpt: {mail_page.inner_text('body')[:400]!r}", flush=True)

    # "Stay signed in?" — click No (or Yes, doesn't matter for our purpose)
    for label in ["No", "Yes"]:
        btn = mail_page.get_by_role("button", name=re.compile(f"^{label}$", re.I))
        if btn.count() == 0:
            btn = mail_page.locator(f"input[value='{label}']")
        if btn.count() > 0 and btn.first.is_visible():
            print(f"  [ksmi] clicking '{label}'", flush=True)
            btn.first.click()
            time.sleep(5)
            break

    # Check for "Verify your identity" / captcha challenges
    body_after = mail_page.inner_text("body")[:600].lower()
    if any(kw in body_after for kw in ("verify your identity", "help us protect", "captcha",
                                       "we need to verify", "unusual activity")):
        shoot(mail_page, "05b-outlook-challenge")
        print(f"  ⚠️ Outlook challenge detected: {body_after!r}", flush=True)
        print(f"  ❌ Cannot auto-bypass. Token onboarding needs manual intervention.",
              flush=True)
        raise SystemExit("OUTLOOK_CHALLENGE")

    # ── Step 4: 找新触发的 Anthropic magic-link (今天日期) ─────────
    # 关键: Outlook 把 anthropic 邮件分到不同 tab/folder; 必须找**今天**的
    # 否则会撞到 4 月的老 magic link → expired
    print(f"[4] Wait for magic link to arrive (90s)", flush=True)
    time.sleep(90)

    secure_link = None
    # 今天日期可能格式: "上午 / PM / 刚刚 / minute / now" 或 "今天" 或 "5月24" / "5/24" / "2026/5/24"
    today_keywords = [
        "刚刚", "分钟前", "minute", "minutes ago", "just now", " now ",
        "今天", "today",
        time.strftime("%m月%d"),  # e.g. 5月24
        time.strftime("%-m月%-d"),  # 5月24 without 0
        time.strftime("%-m/%-d"),  # 5/24
        time.strftime("%Y/%-m/%-d"),  # 2026/5/24
        time.strftime("%Y-%m-%d"),  # 2026-05-24
        time.strftime("%H:"),  # 当前小时 "00:" "13:" 等 (用于 today's HH:MM)
    ]
    today_keywords = [k.lower() for k in today_keywords]
    print(f"  today keywords: {today_keywords}", flush=True)

    def find_anthropic_today_in_current_view():
        """In current Outlook view, find Anthropic email from today; return link or None."""
        rows = mail_page.locator("div[role='option']")
        n = min(rows.count(), 20)
        print(f"    rows: {n}", flush=True)
        for i in range(n):
            row = rows.nth(i)
            try:
                aria = (row.get_attribute("aria-label") or "")[:300]
            except Exception:
                continue
            full = aria.lower()
            is_anthropic = ("anthropic" in full
                            and ("secure link" in full or "sign in" in full))
            is_today = any(kw in full for kw in today_keywords)
            print(f"      [{i}] anthropic={is_anthropic} today={is_today}  aria={aria[:120]!r}",
                  flush=True)
            if is_anthropic and is_today:
                print(f"      → match row {i}", flush=True)
                row.click()
                time.sleep(8)
                shoot(mail_page, "08-outlook-email-opened")
                for a in mail_page.locator("a").all():
                    try:
                        href = a.get_attribute("href") or ""
                    except Exception:
                        continue
                    if "claude.ai/magic-link" in href.lower():
                        return href
                return None
        return None

    # Folder rotation: Focused → Other → Junk → All
    folders = [
        ("Focused inbox", "https://outlook.live.com/mail/0/inbox"),
        ("Junk", "https://outlook.live.com/mail/0/junkemail"),
    ]

    for label, url in folders:
        if secure_link:
            break
        print(f"  [4.{label}] {url}", flush=True)
        try:
            mail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(12)
            shoot(mail_page, f"07-{label.replace(' ', '-').lower()}")
            # try Focused first, then click "其他/Other" tab if no match
            secure_link = find_anthropic_today_in_current_view()
            if secure_link is None and label == "Focused inbox":
                # try clicking 其他/Other tab
                for sel in ["button:has-text('其他')", "button:has-text('Other')",
                            "[role='tab']:has-text('其他')", "[role='tab']:has-text('Other')"]:
                    cand = mail_page.locator(sel).first
                    if cand.count() > 0 and cand.is_visible():
                        print(f"    [click Other tab via {sel}]", flush=True)
                        cand.click()
                        time.sleep(5)
                        shoot(mail_page, "07-other-tab")
                        secure_link = find_anthropic_today_in_current_view()
                        break
        except Exception as e:
            print(f"    err: {e}", flush=True)

    if secure_link:
        print(f"  ✅ link: {secure_link[:120]}", flush=True)
    else:
        try:
            body = mail_page.inner_text("body")[:2500]
            print(f"  ⚠️ final body excerpt: {body!r}", flush=True)
        except Exception:
            pass
        shoot(mail_page, "08b-outlook-no-link-final")

    if not secure_link:
        print(f"  ❌ No secure link found", flush=True)
        br.close()
        raise SystemExit("NO_SECURE_LINK")

    # ── Step 5+ : 同 Gmail 流程 ───────────────────────────────────
    print(f"[5] Open secure link in claude_page", flush=True)
    claude_page.bring_to_front()
    claude_page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(8)
    shoot(claude_page, "09-after-magic-link")

    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)

    body_text = claude_page.inner_text("body")
    if "Join " in body_text and "invited you to join" in body_text:
        print(f"[5b] Team invite — clicking Accept", flush=True)
        accept_btn = claude_page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if accept_btn.count() > 0:
            accept_btn.first.click()
            time.sleep(8)

    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)

    if any(x in claude_page.url for x in ("/new", "claude.ai/projects", "claude.ai/chats")):
        print(f"  [5c] At /new — navigate back to OAuth URL", flush=True)
        claude_page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)

    print(f"[6] Click Authorize", flush=True)
    auth_btn = claude_page.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
    if auth_btn.count() > 0:
        auth_btn.first.click()
        time.sleep(6)
    shoot(claude_page, "10-after-authorize")

    m = re.search(r"[?&]code=([^&\s]+)", claude_page.url)
    if m:
        print(f"\n✅ CALLBACK_CODE={m.group(1)}", flush=True)
    else:
        body_text = claude_page.inner_text("body")
        m = re.search(r"\b([a-zA-Z0-9_-]{50,}#[a-zA-Z0-9_-]{20,})\b", body_text)
        if m:
            print(f"\n✅ CALLBACK_CODE={m.group(1)}", flush=True)
        else:
            print(f"\n❌ No code. body: {body_text[:1500]!r}", flush=True)

    br.close()
