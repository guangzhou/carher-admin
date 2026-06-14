#!/usr/bin/env python3
"""
cc-acct-hold-review.py — Anthropic 账户 on-hold 申诉自动化

适用场景：账户已确认 on hold（v3 跑过实证），需要点 "Request a review" 提交申诉

ARCHITECTURE: single patchright context（仿 cc-oauth-mailcom-v3.py）
  1. claude_page  goto claude.ai/login → fill email → "Continue with email"
  2. mail_page    goto www.mail.com → login → find Anthropic magic-link
  3. claude_page  goto magic-link → land on hold page
  4. claude_page  click "Request a review"
  5. claude_page  dump form structure
  6. claude_page  fill reason (REVIEW_REASON env) into textarea
  7. claude_page  click submit → screenshot confirmation

ENV:
  CC_EMAIL         claude.ai email
  MAIL_PW          mail.com webmail password
  REVIEW_REASON    申诉理由文本（多行 UTF-8，日语/英语均可）
"""
import os, re, time
from urllib.parse import urlparse, parse_qs, unquote
from patchright.sync_api import sync_playwright

EMAIL = os.environ["CC_EMAIL"]
MAIL_PW = os.environ["MAIL_PW"]
REASON = os.environ["REVIEW_REASON"]
SS = "/work/screenshots"
os.makedirs(SS, exist_ok=True)

CLAUDE_LOGIN = "https://claude.ai/login"


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


def get_mail_frame(page, timeout=30):
    """优先 iframe[name=mail]，其次任何含 anthropic/claude/secure link 文本的 frame，
    最后兜底主 page。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # pass 1: 优先 name=="mail"
        for fr in page.frames:
            if fr.name == "mail":
                print(f"  [frame] picked name='mail'", flush=True)
                return fr
        # pass 2: 任何 frame 含 anthropic/claude/secure link 文本
        for fr in page.frames:
            try:
                text = fr.evaluate("() => document.body ? document.body.innerText : ''")[:500]
                if any(x in text.lower() for x in ("anthropic", "secure link", "claude")):
                    print(f"  [frame] picked by content url={fr.url[:80]!r}", flush=True)
                    return fr
            except Exception:
                continue
        time.sleep(2)
    # pass 3: 兜底主 page (含全部 frame 的 page 自己)
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")[:500]
        if any(x in text.lower() for x in ("anthropic", "secure link", "claude")):
            print(f"  [frame] picked main page as fallback", flush=True)
            return page
    except Exception:
        pass
    return None


def unwrap_deref(url):
    if "deref-mail.com" in url or "redirectUrl=" in url:
        try:
            qs = parse_qs(urlparse(url).query)
            if "redirectUrl" in qs:
                return unquote(qs["redirectUrl"][0])
        except Exception:
            pass
    return url


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")

    # ── Step 1: trigger email via claude.ai/login ───────────────────
    print("[1] claude_page: goto /login + trigger email", flush=True)
    claude_page = ctx.new_page()
    claude_page.goto(CLAUDE_LOGIN, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 90)
        time.sleep(3)
    shoot(claude_page, "1-login")

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
    shoot(claude_page, "2-after-email")

    # ── Step 2: mail.com fetch magic-link ───────────────────────────
    print("[2] mail_page: login mail.com + fetch magic-link", flush=True)
    mail_page = ctx.new_page()
    mail_page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)

    for label_pat in [r"accept all", r"agree", r"i agree", r"^ok$"]:
        try:
            b = mail_page.get_by_role("button", name=re.compile(label_pat, re.I))
            if b.count() > 0 and b.first.is_visible():
                b.first.click()
                time.sleep(2)
                break
        except Exception:
            pass

    for sel in ["a:has-text('Log in')", "button:has-text('Log in')", "a[href*='login']"]:
        try:
            cand = mail_page.locator(sel)
            for i in range(cand.count()):
                el = cand.nth(i)
                if el.is_visible():
                    el.click()
                    break
            break
        except Exception:
            pass
    time.sleep(5)

    mail_page.wait_for_selector(
        "input[placeholder='Email address'], input[type='email'], input[name='username']",
        timeout=20000)
    e_in = mail_page.locator("input[placeholder='Email address']").first
    if e_in.count() == 0:
        e_in = mail_page.locator("input[type='email']").first
    e_in.click()
    e_in.fill(EMAIL)
    p_in = mail_page.locator("input[placeholder='Password']").first
    if p_in.count() == 0:
        p_in = mail_page.locator("input[type='password']").first
    p_in.click()
    p_in.fill(MAIL_PW)

    btns = mail_page.locator("button:has-text('Log in'), input[type='submit']")
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
        mail_page.keyboard.press("Enter")
    time.sleep(10)
    time.sleep(45)

    fr = get_mail_frame(mail_page, timeout=30)
    if not fr:
        shoot(mail_page, "3-no-mail-frame")
        br.close()
        raise SystemExit("NO_MAIL_FRAME")
    shoot(mail_page, "3-inbox")

    SENDER_RE = re.compile(r"anthropic|claude|secure link", re.I)
    secure_link = None
    for attempt in range(5):
        if secure_link:
            break
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
        for f in mail_page.frames:
            try:
                for a in f.locator("a").all():
                    try:
                        href = a.get_attribute("href") or ""
                    except Exception:
                        continue
                    h = href.lower()
                    if "claude" in h and any(x in h for x in ("login", "magic", "verify", "token=")):
                        secure_link = href
                        break
                if secure_link:
                    break
                body = f.evaluate("() => document.body.innerText")
                m = re.search(r"(https?://[^\s\"'<>]*claude[^\s\"'<>]*(?:login|magic|verify|token=)[^\s\"'<>]*)", body, re.I)
                if m:
                    secure_link = m.group(1)
                    break
            except Exception:
                continue

    if not secure_link:
        br.close()
        raise SystemExit("NO_SECURE_LINK")

    secure_link = unwrap_deref(secure_link)
    print(f"  magic-link: {secure_link[:120]}", flush=True)

    # ── Step 3: claude_page goto magic-link → expect hold page ──────
    print("[3] claude_page: goto magic-link", flush=True)
    claude_page.bring_to_front()
    claude_page.goto(secure_link, wait_until="domcontentloaded", timeout=30000)
    time.sleep(10)
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)
    shoot(claude_page, "4-after-magic-link")

    body = claude_page.inner_text("body")
    if "on hold" not in body.lower():
        print(f"  ⚠️ hold not detected. body: {body[:800]!r}", flush=True)
        br.close()
        raise SystemExit("NOT_HOLD_PAGE")
    print(f"  ✅ hold page confirmed", flush=True)

    # ── Step 4: click "Request a review" — JS dom scan (a/button/[role=button] 全覆盖) ──
    print("[4] click Request a review (broad DOM scan)", flush=True)
    # 先列出所有候选元素
    candidates = claude_page.evaluate("""() => {
        const all = [...document.querySelectorAll('*')];
        return all
            .filter(el => {
                const t = (el.innerText || '').trim().toLowerCase();
                return t === 'request a review';
            })
            .map((el, i) => {
                const r = el.getBoundingClientRect();
                return {
                    i,
                    tag: el.tagName,
                    role: el.getAttribute('role') || '',
                    href: el.getAttribute('href') || '',
                    cls: (el.className || '').toString().slice(0, 80),
                    width: r.width,
                    height: r.height,
                    area: r.width * r.height,
                    childCount: el.children.length,
                };
            });
    }""")
    print(f"  candidates ({len(candidates)}):", flush=True)
    for c in candidates:
        print(f"    {c}", flush=True)

    # 点击策略: 选 childCount==0 的（最叶子节点，即按钮本身而非容器）
    # 否则选 area 最小的
    clicked = claude_page.evaluate("""() => {
        const targets = [...document.querySelectorAll('*')].filter(el => {
            const t = (el.innerText || '').trim().toLowerCase();
            return t === 'request a review';
        });
        if (targets.length === 0) return {ok: false, reason: 'no_targets'};
        // 先找 leaf (childCount==0) 中最大的（叶子节点 = 按钮文本元素本身）
        const leaves = targets.filter(el => el.children.length === 0);
        let target = null;
        if (leaves.length > 0) {
            // 多个叶子时选最大那个（最可能是按钮）
            target = leaves.reduce((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (ar.width * ar.height) >= (br.width * br.height) ? a : b;
            });
        } else {
            // 没有叶子, 选 area 最小的（最内层容器）
            target = targets.reduce((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (ar.width * ar.height) <= (br.width * br.height) ? a : b;
            });
        }
        // 找 target 的最近 clickable 祖先 (button/a/[role=button])
        let cur = target;
        while (cur && cur !== document.body) {
            const tag = cur.tagName.toLowerCase();
            const role = cur.getAttribute('role');
            if (tag === 'button' || tag === 'a' || role === 'button' || cur.onclick) {
                cur.click();
                return {ok: true, clicked_tag: tag, clicked_role: role || ''};
            }
            cur = cur.parentElement;
        }
        // 没找到 clickable 祖先, 直接 click target 本身
        target.click();
        return {ok: true, clicked_tag: target.tagName.toLowerCase(), clicked_role: 'fallback_self'};
    }""")
    print(f"  click result: {clicked}", flush=True)
    if not clicked.get("ok"):
        shoot(claude_page, "5a-no-review-button")
        br.close()
        raise SystemExit("NO_REVIEW_BUTTON")
    time.sleep(10)
    if "moment" in (claude_page.title() or "").lower():
        wait_past_turnstile(claude_page, 60)
        time.sleep(3)
    shoot(claude_page, "5-review-form")

    # ── Step 5: dump form structure ────────────────────────────────
    print("[5] dump review form structure", flush=True)
    form_body = claude_page.inner_text("body")
    print(f"  url: {claude_page.url}", flush=True)
    print(f"  body (first 2500): {form_body[:2500]!r}", flush=True)
    # 列举所有 visible textarea / input
    print(f"  --- visible inputs:", flush=True)
    for sel in ["textarea", "input[type='text']", "input[type='email']", "input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='submit'])"]:
        try:
            els = claude_page.locator(sel).all()
            for i, el in enumerate(els):
                if el.is_visible():
                    name = el.get_attribute("name") or ""
                    pl = el.get_attribute("placeholder") or ""
                    aria = el.get_attribute("aria-label") or ""
                    print(f"    {sel} #{i}: name={name!r} placeholder={pl!r} aria-label={aria!r}", flush=True)
        except Exception:
            pass

    # ── Step 6: fill reason (textarea 优先) ─────────────────────────
    print("[6] fill reason", flush=True)
    filled = False
    # 优先 textarea (review form 典型用 textarea)
    try:
        textareas = claude_page.locator("textarea").all()
        for ta in textareas:
            try:
                if ta.is_visible():
                    ta.click()
                    ta.fill(REASON)
                    print(f"  ✅ filled textarea (len={len(REASON)})", flush=True)
                    filled = True
                    break
            except Exception as e:
                print(f"  textarea fill failed: {e}", flush=True)
    except Exception:
        pass

    if not filled:
        print(f"  ❌ no fillable textarea, abort", flush=True)
        shoot(claude_page, "6-fail-no-textarea")
        br.close()
        raise SystemExit("NO_TEXTAREA")

    shoot(claude_page, "6-form-filled")

    # ── Step 7: submit review ──────────────────────────────────────
    print("[7] submit review", flush=True)
    submit_btn = claude_page.get_by_role("button", name=re.compile(r"^submit$|^send$|^request review$|^request$|^next$|^continue$|提交", re.I))
    if submit_btn.count() == 0:
        # 兜底: 所有 visible button, 选离 textarea 最近的非 cancel/back
        all_btns = claude_page.locator("button").all()
        for b in all_btns:
            try:
                if b.is_visible():
                    label = (b.inner_text() or "").strip().lower()
                    if label and label not in ("cancel", "back", "close", "sign out"):
                        submit_btn = b
                        print(f"  fallback submit btn: {label!r}", flush=True)
                        break
            except Exception:
                pass
        if not hasattr(submit_btn, "click") and (not hasattr(submit_btn, "count") or submit_btn.count() == 0):
            print(f"  ❌ no submit button found", flush=True)
            shoot(claude_page, "7-fail-no-submit")
            br.close()
            raise SystemExit("NO_SUBMIT_BUTTON")
    try:
        if hasattr(submit_btn, "first"):
            submit_btn.first.click()
        else:
            submit_btn.click()
    except Exception as e:
        print(f"  submit click failed: {e}", flush=True)
        shoot(claude_page, "7-fail-click")
        br.close()
        raise SystemExit("SUBMIT_CLICK_FAILED")
    time.sleep(12)
    shoot(claude_page, "7-after-submit")

    # ── Step 8: capture confirmation ───────────────────────────────
    final_body = claude_page.inner_text("body")
    print(f"\n=== FINAL STATE ===", flush=True)
    print(f"  url: {claude_page.url}", flush=True)
    print(f"  body (first 1500): {final_body[:1500]!r}", flush=True)

    success_markers = ["submitted", "received", "thank you", "we'll review", "we have received",
                       "受け取りました", "送信しました", "ありがとう", "your request"]
    if any(m in final_body.lower() for m in success_markers):
        print(f"\n✅ REVIEW_SUBMITTED", flush=True)
    else:
        print(f"\n⚠️ status unclear, see screenshots 7-after-submit.png", flush=True)

    br.close()
