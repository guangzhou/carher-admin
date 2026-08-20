#!/usr/bin/env python3
"""
zerokey-web-capture.py — Log into chatgpt.com on 188 and capture a real
/backend-api/f/conversation POST (headers + cookies + body) so zerokey can
replay the web-chat session as an OpenAI-compatible API.

WHY this exists (read before re-debugging):
  - zerokey's ChatGPT provider replays a captured browser request. It REQUIRES
    the `openai-sentinel-proof-token` request header (it decodes it for the real
    UA + POW config). A bare OAuth token is NOT enough — we need the full
    browser request incl. cf_clearance cookie, which is bound to the 188 egress
    IP. Hence capture MUST run on 188 (JP exit), same host where zerokey runs.
  - CF on chatgpt.com requires patchright (real Chrome TLS) + headed (Xvfb).
    Login flow reused from chatgpt-litellm-oauth.py Phase 1.5.

ENV:
  MAIL_USER           kristine_free517@mail.com
  MAIL_LOGIN_PW_FILE  /run/mail_pw.txt      (webmail password, for OTP)
  CHATGPT_PW_FILE     /run/chatgpt_pw.txt   (ChatGPT login password)
  OUT_JSON            /work/out/zerokey-users.json   (zerokey temp/users.json)
  ZK_USER             username key inside users.json (default: kristine)
  SCREENSHOT_DIR      /work/screenshots
  CAPTURE_PROMPT      message to send to trigger the request
                      (default: a random natural greeting, e.g. "hello" /
                      "hey there" / "what's your name?")

OUTPUT (OUT_JSON), zerokey temp/users.json shape:
  { "chatgpt": { "<ZK_USER>": {
      "username": "<ZK_USER>",
      "parsedFetch": { "url": "...", "method": "POST", "headers": {...}, "body": {...} },
      "sessions": [] } } }
"""

import os, re, sys, json, time, random
from patchright.sync_api import sync_playwright

# A repeated "hihihihi…" seed makes it obvious a bot is driving capture. Pick a
# short natural greeting at random instead, so the trigger message looks human.
GREETINGS = [
    "hi", "hello", "hey", "hey there", "yo", "hiya",
    "good morning", "how's it going", "what's up",
    "what's your name?", "how are you today?", "nice to meet you",
]

EMAIL      = os.environ["MAIL_USER"]
MAIL_PW    = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
CHATGPT_PW = open(os.environ["CHATGPT_PW_FILE"]).read().strip()
SS_DIR     = os.environ.get("SCREENSHOT_DIR", "/work/screenshots")
OUT_JSON   = os.environ.get("OUT_JSON", "/work/out/zerokey-users.json")
ZK_USER    = os.environ.get("ZK_USER", "kristine")
PROMPT     = os.environ.get("CAPTURE_PROMPT") or random.choice(GREETINGS)
OTP_FILE   = os.environ.get("OTP_FILE", "/work/out/otp.txt")
OTP_FILE_WAIT = int(os.environ.get("OTP_FILE_WAIT", "600"))
OTP_AUTO_ONLY = os.environ.get("OTP_AUTO_ONLY", "0") == "1"
OTP_AUTO_MAX = int(os.environ.get("OTP_AUTO_MAX", "240"))
# LOGIN_MODE: "password" (default, 188 behavior) or "otp" (passwordless email
# one-time-code login — for accounts whose web password is unknown/stale).
LOGIN_MODE = os.environ.get("LOGIN_MODE", "password").lower()
OTP_SHOT = os.environ.get("OTP_SHOT", "0") == "1"
OTP_SHOT_PATH = os.environ.get("OTP_SHOT_PATH", "/work/out/otpshot.png")
OTP_RE = re.compile(r"\b(\d{6})\b")
SENDER_HINTS_RE = re.compile(r"openai|chatgpt|noreply", re.I)
MAIL_OTP_PROVIDER = os.environ.get("MAIL_OTP_PROVIDER", "").lower()
if not MAIL_OTP_PROVIDER:
    MAIL_OTP_PROVIDER = "imap_qq" if EMAIL.endswith("@qq.com") else "mailcom"

os.makedirs(SS_DIR, exist_ok=True)


def imap_host_port():
    if MAIL_OTP_PROVIDER == "imap_qq":
        return ("imap.qq.com", 993)
    return (os.environ.get("IMAP_HOST", "imap.qq.com"), int(os.environ.get("IMAP_PORT", "993")))


def imap_fetch_otp(since_ts, max_wait=180):
    """Poll IMAP for the latest OpenAI/ChatGPT login OTP (QQ: mail_pw = 16-char auth code)."""
    import imaplib
    import email as _email

    host, port = imap_host_port()
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(host, port, timeout=20)
            M.login(EMAIL, MAIL_PW)
            M.select("INBOX")
            typ, data = M.search(None, "FROM", "tm.openai.com", "SUBJECT", "temporary")
            ids = data[0].split()
            for mid in reversed(ids[-5:]):
                typ, msg_data = M.fetch(mid, "(RFC822)")
                msg = _email.message_from_bytes(msg_data[0][1])
                try:
                    mail_ts = _email.utils.mktime_tz(_email.utils.parsedate_tz(msg["Date"]))
                except Exception:
                    mail_ts = 0
                if mail_ts < since_ts - 30:
                    continue
                body = ""
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        b = part.get_payload(decode=True)
                        if b:
                            body = b.decode(part.get_content_charset() or "utf-8", errors="replace")
                            break
                m = OTP_RE.search(body)
                if m:
                    code = m.group(1)
                    print(f"  IMAP: got OTP {code} from mail dated {msg['Date']}", flush=True)
                    try:
                        M.logout()
                    except Exception:
                        pass
                    return code, body[:200]
            try:
                M.logout()
            except Exception:
                pass
        except Exception as e:
            print(f"  IMAP fetch err: {e}", flush=True)
        print(f"  IMAP: OTP not yet (since_ts={since_ts}), retry in 10s...", flush=True)
        time.sleep(10)
    return None, None


def ss(page, name):
    try:
        page.screenshot(path=f"{SS_DIR}/{name}.png", full_page=False)
        print(f"  shot: {SS_DIR}/{name}.png", flush=True)
    except Exception as e:
        print(f"  shot fail: {e}", flush=True)


# ── mail.com OTP (ported from chatgpt-litellm-oauth.py) ───────────────────
def mailcom_login(ctx):
    p = ctx.new_page()
    p.goto("https://www.mail.com/", wait_until="domcontentloaded")
    p.locator("a:has-text('Log in')").first.click()
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
    if "navigator" not in p.url:
        ss(p, "mailcom-fail")
        print("  mail.com login may have failed url=" + p.url, flush=True)
    p.wait_for_timeout(3000)
    for sel in [
        "a:has-text('Continue to Account')",
        "button:has-text('Continue to Account')",
        "button:has-text('No, thanks')",
        "button:has-text('Maybe later')",
        "button:has-text('Skip')",
    ]:
        try:
            loc = p.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                p.wait_for_timeout(2000)
        except Exception:
            pass
    # wait for inbox content to actually render.
    # ⚠️ 不能用 document.body.innerText 判: mail.com 2026-06-18 起把**邮件列表和正文
    # 都搬进了 Shadow DOM**, innerText 返回空 → 45 轮全判"没加载"、取码 0 命中,
    # 而同一时刻脚本自己拍的截图上收件箱和验证码邮件清清楚楚(2026-08-06 acct-141
    # 实证, 累计 8 个号同一签名)。判据改成数 Shadow DOM 能穿透的列表行
    # (与 chatgpt-litellm-oauth.py 一致 —— 那份实现同期在同样这些邮箱上取码成功)。
    loaded = False
    for attempt in range(45):
        sc = mail_scope(p, dump=(attempt == 0))
        if sc:
            try:
                n = sc.locator("[class*='mail-item']").count()
            except Exception:
                n = 0
            if n > 0:
                print(f"  mail.com: inbox loaded ({n} rows, scope={sc.name or 'main'})", flush=True)
                loaded = True
                break
        if attempt > 0 and attempt % 10 == 0:
            print(f"  mail.com: skeleton stall — reload (attempt {attempt})", flush=True)
            try:
                p.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
        print(f"  mail.com: waiting inbox... [{attempt+1}/45]", flush=True)
        time.sleep(2)
    if not loaded:
        print("  mail.com: WARN inbox rows never appeared — proceeding anyway", flush=True)
    ss(p, "mailcom-inbox")
    return p


def mail_scope(mail_page, dump=False):
    """返回**能数到邮件列表行**的 scope(主文档或某个 iframe)。

    ⚠️ 不能假设存在 name='mail' 的 iframe。2026-08-07 实证: cap.py 打开的 mail.com
    页面里根本没有这个 frame, 旧实现(以及刚移植进来的版本)恒打
    `mail frame missing, retry 5s` 空转到超时 —— 而同一时刻截图上收件箱是渲染好的。
    新版 mail.com 把列表挪到了主文档/别名 frame。按"哪个 scope 数得到行"来找。
    """
    cands = [mail_page.main_frame] + [f for f in mail_page.frames if f != mail_page.main_frame]
    if dump:
        print("  [frames] " + " | ".join(
            f"name={f.name!r} url={(f.url or '')[:60]}" for f in cands[:8]), flush=True)
    for fr in cands:
        try:
            if fr.locator("[class*='mail-item']").count() > 0:
                return fr
        except Exception:
            continue
    return None


def settle_inbox(mail_page, label="otp"):
    """等 N 秒 → **刷新** → 再等 N 秒 → 才读码。

    本次登录触发的验证码是在打开收件箱**之后**才到的; 不刷新只能看到打开那一刻的
    旧列表 → 读到上一次的旧码或压根看不到新邮件。中间那次刷新不能省
    (ported from chatgpt-litellm-oauth.py)。
    """
    secs = 0 if os.environ.get("OTP_FAST") else int(os.environ.get("OTP_SETTLE_SEC", "60"))
    if not secs:
        return
    print(f"  [{label}] settle {secs}s (让本次验证码先到)...", flush=True)
    time.sleep(secs)
    refreshed = False
    try:
        sc = mail_scope(mail_page)
        if sc:
            sc.evaluate("() => document.location.reload()")
            refreshed = True
    except Exception:
        pass
    if not refreshed:
        try:
            mail_page.reload(wait_until="domcontentloaded", timeout=60000)
            refreshed = True
        except Exception as e:
            print(f"  [{label}] inbox refresh err: {str(e)[:60]}", flush=True)
    try:
        mail_page.wait_for_timeout(3000)
    except Exception:
        pass
    print(f"  [{label}] refreshed={refreshed}; settle another {secs}s...", flush=True)
    time.sleep(secs)


def mailcom_get_otp_shadowdom(mail_page, max_wait=None):
    """穿透 Shadow DOM 取 mail.com 验证码 —— 移植自 chatgpt-litellm-oauth.py 的 get_otp。

    与本文件旧实现的区别(旧实现是 8 个号取码失败的直接原因):
      列表行 : mf.locator("[class*='mail-item']").text_content()  ← 穿透 Shadow DOM
               (旧: 遍历 frames 取 document.body.innerText → 恒为空)
      邮件正文: frame name 含 'detail-body' 的 iframe 的 outerHTML
               (旧: 跨所有 frame 扫 innerText → 既取不到、又会把广告的 6 位数字误当码)
    """
    if max_wait is None:
        max_wait = OTP_AUTO_MAX
    CODE_SUBJ = re.compile(r"code|verification|登录代码|临时|temporary|验证码", re.I)
    # 只排除**确定没有验证码**的 sign-in 提醒。别往这里加"套餐/续订/账单":
    # 那些主题的邮件里也是验证码, 加了等于把真码筛掉。
    ALERT_SUBJ = re.compile(r"new sign-?in|new login|新登录|新的登录|sign-?in to your|security", re.I)

    def find_body_frame():
        return next((f for f in mail_page.frames
                     if "detail-body" in (f.name or "") or "detail-body" in (f.url or "")), None)

    def extract_otp_from_body():
        bf = find_body_frame()
        if not bf:
            return None
        try:
            html = bf.evaluate("() => document.documentElement.outerHTML") or ""
        except Exception:
            return None
        for m in re.finditer(r"\b(\d{6})\b", html):
            ctx = html[max(0, m.start() - 200): m.end() + 200]
            if re.search(r"code|verify|verification|login|openai|chatgpt", ctx, re.I):
                return m.group(1)
        m = re.search(r"\b(\d{6})\b", html)
        return m.group(1) if m else None

    deadline = time.time() + max_wait
    while time.time() < deadline:
        mf = mail_scope(mail_page, dump=(time.time() > deadline - max_wait + 1))
        if not mf:
            print("  mail list scope not found (无任何 scope 数得到 mail-item), retry 5s", flush=True)
            time.sleep(5)
            continue
        try:
            rows = mf.locator("[class*='mail-item']")
            cnt = rows.count()
        except Exception as e:
            print(f"  rows count err: {str(e)[:60]}", flush=True)
            cnt = 0
        texts = []
        for i in range(min(cnt, 15)):
            try:
                texts.append((rows.nth(i).text_content(timeout=1500) or "").strip())
            except Exception:
                texts.append("")
        order = [i for i, t in enumerate(texts)
                 if re.search(r"openai|chatgpt|noreply", t, re.I)
                 and CODE_SUBJ.search(t) and not ALERT_SUBJ.search(t)]
        order += [i for i, t in enumerate(texts)
                  if re.search(r"openai|chatgpt|noreply", t, re.I)
                  and i not in order and not ALERT_SUBJ.search(t)]
        for i in order:
            print(f"  candidate row[{i}]: {texts[i][:100]!r}", flush=True)
            row_el = rows.nth(i)
            try:
                row_el.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            opened = False
            for name, act in [
                ("dblclick", lambda: row_el.dblclick(timeout=4000)),
                ("subj-link-click", lambda: row_el.locator(":scope a, :scope [role='link'], :scope span").first.click(timeout=3000)),
                ("evaluate-dispatch", lambda: row_el.evaluate(
                    "el => { el.dispatchEvent(new MouseEvent('dblclick', {bubbles:true, cancelable:true, view:window})); }")),
            ]:
                try:
                    act()
                    time.sleep(3.5)
                    if find_body_frame():
                        print(f"  ✓ {name} opened body frame", flush=True)
                        opened = True
                        break
                except Exception as e:
                    print(f"  {name} err: {str(e)[:60]}", flush=True)
            if not opened:
                continue
            for _ in range(10):
                if find_body_frame():
                    break
                time.sleep(1)
            code = extract_otp_from_body()
            if code:
                print(f"  OTP via shadow-dom reader: {code}", flush=True)
                return code
            # 点开了但正文没码(账单/提醒类) → 试下一个候选, **别 break**:
            # break 会让外层重试又从头挑到同一封 → 死循环。
            print(f"  row[{i}] opened but no OTP in body — try next candidate", flush=True)
        print(f"  OTP not yet (rows={cnt}), retry in 5s...", flush=True)
        time.sleep(5)
        try:
            mf.evaluate("() => document.location.reload()")
        except Exception:
            pass
        mail_page.wait_for_timeout(3000)
    return None


def find_mail_frame(page):
    """Return mail.com inbox iframe (name=mail), polling up to ~25s.

    新版 mail.com 已没有 name='mail' 这个 frame(2026-08-07 实证), 找不到时回落到
    mail_scope() 按"哪个 scope 数得到 mail-item"来定位, 否则这条兜底路径必然空转。
    """
    deadline = time.time() + 25
    while time.time() < deadline:
        for fr in page.frames:
            if fr.name == "mail":
                return fr
        sc = mail_scope(page)
        if sc:
            return sc
        time.sleep(2)
    return None


def extract_otp_from_open_mail(mail_frame, page):
    """Extract 6-digit OTP from opened message body (skip inbox list frame)."""
    texts = []
    for fr in page.frames:
        try:
            if fr.name == "mail":
                continue
            texts.append(fr.evaluate("() => document.body.innerText"))
        except Exception:
            pass
    try:
        texts.append(mail_frame.evaluate("() => document.body.innerText"))
    except Exception:
        pass
    for text in texts:
        if not SENDER_HINTS_RE.search(text) and "code" not in text.lower():
            continue
        m = OTP_RE.search(text)
        if m:
            return m.group(1)
    return None


def mailcom_open_and_read_otp(mp):
    """Open the newest ChatGPT login-code email (frame_locator path that OTP_SHOT
    proved reliable on mail.com) and read the 6-digit code from the reading pane.
    Returns code str or None. This is the robust auto path (get_otp's list-item
    click is flaky on mail.com's iframe layout)."""
    opened = False
    for fsel in ["iframe[name='mail']", "iframe[src*='mail']", "iframe"]:
        try:
            fl = mp.frame_locator(fsel)
            for needle in ["temporary ChatGPT login code", "ChatGPT login code", "login code"]:
                loc = fl.get_by_text(needle, exact=False)
                if loc.count() > 0:
                    loc.first.click(timeout=8000)
                    mp.wait_for_timeout(4000)
                    opened = True
                    break
        except Exception as e:
            print(f"  otp-open fl {fsel} err: {str(e)[:80]}", flush=True)
        if opened:
            break
    if not opened:
        return None
    # read reading-pane text across all frames; the code sits near "code"/openai
    for _ in range(3):
        for fr in mp.frames:
            try:
                txt = fr.evaluate("() => document.body.innerText")
            except Exception:
                continue
            if not txt:
                continue
            low = txt.lower()
            if "code" not in low and not SENDER_HINTS_RE.search(txt):
                continue
            for m in OTP_RE.finditer(txt):
                ctx = txt[max(0, m.start() - 120): m.start() + 40]
                if re.search(r"code|verify|temporary", ctx, re.I):
                    return m.group(1)
        mp.wait_for_timeout(1500)
    return None


def get_otp(mail_page, max_wait=None):
    """Poll mail.com inbox for OpenAI OTP — fully automated (chatgpt-login-session pattern)."""
    if max_wait is None:
        max_wait = OTP_AUTO_MAX
    deadline = time.time() + max_wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        frame = find_mail_frame(mail_page)
        if frame:
            try:
                text = frame.evaluate("() => document.body.innerText")
            except Exception:
                text = ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for ln in lines:
                if not SENDER_HINTS_RE.search(ln):
                    continue
                m = OTP_RE.search(ln)
                if m:
                    print(f"  OTP found in inbox list (attempt {attempt})", flush=True)
                    return m.group(1)
                try:
                    frame.get_by_text(ln, exact=False).first.click(timeout=5000)
                    time.sleep(5)
                    ss(mail_page, "mailcom-message-opened")
                    code = extract_otp_from_open_mail(frame, mail_page)
                    if code:
                        print(f"  OTP found in opened mail (attempt {attempt})", flush=True)
                        return code
                except Exception:
                    pass
        # legacy frame scan fallback
        for fr in mail_page.frames:
            try:
                text = fr.evaluate("() => document.body.innerText")
            except Exception:
                continue
            if not text or len(text) < 50:
                continue
            if not SENDER_HINTS_RE.search(text):
                continue
            for m in OTP_RE.finditer(text):
                ctx = text[max(0, m.start() - 100): m.start() + 100]
                if re.search(r"code|verify|openai|login", ctx, re.I):
                    print(f"  OTP found via frame scan (attempt {attempt})", flush=True)
                    return m.group(1)
        print(f"  OTP not yet, retry in 5s... (attempt {attempt})", flush=True)
        time.sleep(5)
        try:
            mail_page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        mail_page.wait_for_timeout(3000)
    return None


# ── chatgpt.com login helpers (ported) ────────────────────────────────────
def submit_form(p):
    try:
        btns = p.evaluate("""() => {
            return [...document.querySelectorAll('button')].filter(b => {
                const t = (b.innerText||'').trim();
                return /^(Continue|Sign in|Submit|Verify|Log in)$/i.test(t)
                    && !/google|apple|phone|microsoft/i.test(t)
                    && (b.type === 'submit' || b.closest('form'));
            }).map(b => { const r=b.getBoundingClientRect();
                return {text:b.innerText.trim(), x:r.x, y:r.y, w:r.width, h:r.height}; });
        }""")
        for b in btns:
            if b["w"] > 0 and b["h"] > 0:
                p.mouse.click(b["x"] + b["w"] / 2, b["y"] + b["h"] / 2)
                print(f"    submit click: '{b['text']}'", flush=True)
                return
    except Exception as e:
        print(f"    submit dump fail: {e}", flush=True)
    try:
        p.keyboard.press("Enter")
        return
    except Exception:
        pass
    p.evaluate("() => { const f=document.querySelector('form'); if(f)(f.requestSubmit?f.requestSubmit():f.submit()); }")


def wait_cf(p, max_wait=90):
    deadline = time.time() + max_wait
    clicked = False
    while time.time() < deadline:
        try:
            if p.locator("input[type='email'], input[autocomplete='username']").count() > 0:
                return True
            title = p.title()
            body = p.content().lower()[:2000]
        except Exception:
            title, body = "", ""
        cf = ("verify you are human" in body or "challenges.cloudflare" in body
              or "turnstile" in body or "just a moment" in title.lower())
        if cf and not clicked:
            try:
                pos = p.evaluate("""() => {
                    for (const f of document.querySelectorAll('iframe')) {
                        const s=(f.src||'').toLowerCase(), t=(f.title||'').toLowerCase();
                        if (s.includes('cloudflare')||s.includes('turnstile')||t.includes('challenge')||t.includes('verify')){
                            const r=f.getBoundingClientRect();
                            if(r.width>0&&r.height>0) return {x:r.x,y:r.y,w:r.width,h:r.height};
                        }
                    } return null; }""")
                cx, cy = (pos["x"] + 30, pos["y"] + pos["h"] / 2) if pos else (510, 450)
                p.mouse.move(cx - 40, cy - 25, steps=10); time.sleep(0.3)
                p.mouse.move(cx, cy, steps=12); time.sleep(0.3)
                p.mouse.click(cx, cy)
                clicked = True
                print(f"    clicked CF @ ({int(cx)},{int(cy)})", flush=True)
            except Exception as e:
                print(f"    CF click failed: {e}", flush=True)
        time.sleep(2)
    return p.locator("input[type='email'], input[autocomplete='username']").count() > 0


def clear_cf(page, max_wait=90):
    """On chatgpt.com app pages a Cloudflare Turnstile checkbox may gate access.
    Click it and wait until the challenge clears. Returns True if cleared/absent."""
    deadline = time.time() + max_wait
    clicked = 0
    while time.time() < deadline:
        try:
            body = page.content().lower()[:3000]
            title = page.title().lower()
        except Exception:
            body, title = "", ""
        cf = ("verify you are human" in body or "challenges.cloudflare" in body
              or "turnstile" in body or "just a moment" in title)
        if not cf:
            return True
        try:
            pos = page.evaluate("""() => {
                for (const f of document.querySelectorAll('iframe')) {
                    const s=(f.src||'').toLowerCase(), t=(f.title||'').toLowerCase();
                    if (s.includes('cloudflare')||s.includes('turnstile')||t.includes('challenge')||t.includes('verify')){
                        const r=f.getBoundingClientRect();
                        if(r.width>0&&r.height>0) return {x:r.x,y:r.y,w:r.width,h:r.height};
                    }
                } return null; }""")
            cx, cy = (pos["x"] + 30, pos["y"] + pos["h"] / 2) if pos else (408, 360)
            page.mouse.move(cx - 40, cy - 25, steps=10); time.sleep(0.3)
            page.mouse.move(cx, cy, steps=12); time.sleep(0.3)
            page.mouse.click(cx, cy)
            clicked += 1
            print(f"    clear_cf: clicked turnstile @ ({int(cx)},{int(cy)}) [{clicked}]", flush=True)
        except Exception as e:
            print(f"    clear_cf click err: {e}", flush=True)
        time.sleep(3)
    return False


def is_logged_in(page):
    try:
        li = page.locator("button:has-text('Log in'), a:has-text('Log in'), button:has-text('Sign up for free')")
        if li.count() > 0 and li.first.is_visible():
            return False
    except Exception:
        pass
    return True


def _totp_now(secret, digits=6, period=30):
    """RFC-6238 TOTP from a base32 secret — pure stdlib (image has no pyotp)."""
    import hmac, hashlib, struct, base64, time as _t
    s = secret.strip().replace(" ", "").upper()
    s += "=" * ((8 - len(s) % 8) % 8)
    key = base64.b32decode(s)
    ctr = int(_t.time()) // period
    h = hmac.new(key, struct.pack(">Q", ctr), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def handle_mfa_challenge(page):
    """If the post-OTP page is the TOTP authenticator challenge, compute the
    6-digit code from TOTP_SECRET and submit. No-op if no MFA / no secret."""
    import time as _t
    for attempt in range(4):
        if "mfa" not in page.url.lower() and "authenticator" not in page.url.lower():
            return
        secret = os.environ.get("TOTP_SECRET", "").strip()
        if not secret:
            print("  MFA challenge but no TOTP_SECRET env — cannot answer", flush=True)
            return
        code = _totp_now(secret)
        print(f"  MFA challenge (try {attempt+1}) → TOTP={code}", flush=True)
        try:
            inp = None
            for sel in ["input[autocomplete='one-time-code']", "input[inputmode='numeric']",
                        "input[type='text']", "input[type='tel']", "input"]:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    inp = loc.first
                    break
            if inp is None:
                print("  MFA: no input found", flush=True); return
            inp.click()
            inp.fill("")            # clear any stale email-OTP value
            inp.fill(code)          # fill() clears+sets reliably
            _t.sleep(0.5)
            submit_form(page)
            # wait for the challenge to clear (url leaves mfa-challenge)
            for _ in range(10):
                _t.sleep(1.5)
                if "mfa" not in page.url.lower():
                    print(f"    MFA cleared url={page.url[:100]}", flush=True)
                    ss(page, "mfa-submitted")
                    return
            # still on mfa → check for incorrect-code, recompute next window
            try:
                bt = page.inner_text("body", timeout=2000).lower()
            except Exception:
                bt = ""
            if "incorrect" in bt or "try again" in bt:
                print("    MFA incorrect — waiting for next TOTP window", flush=True)
                _t.sleep(31)        # roll to next 30s window for a fresh code
            ss(page, "mfa-submitted")
        except Exception as e:
            print(f"  MFA submit err: {str(e)[:100]}", flush=True)
            _t.sleep(3)
    print("  MFA: exhausted attempts", flush=True)


def login_chatgpt(ctx, page):
    page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded")
    time.sleep(3)
    # there may be an intermediate "Log in" / "Stay logged out" button
    for sel in ["button:has-text('Log in')", "a:has-text('Log in')",
                "[data-testid='login-button']"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(force=True)
                time.sleep(3)
                break
        except Exception:
            pass
    wait_cf(page)
    page.wait_for_selector("input[type='email'], input[autocomplete='username']", timeout=30000)
    page.locator("input[type='email']").first.click()
    page.keyboard.type(EMAIL, delay=80)
    submit_form(page)
    time.sleep(5)
    print(f"    after email url={page.url[:100]}", flush=True)
    for _ in range(15):
        if "password" in page.url.lower() or "passkey" in page.url.lower():
            break
        time.sleep(1)
    if "passkey" in page.url.lower() or "auth_challenge" in page.url.lower():
        try:
            alt = page.locator("a, button").filter(has_text=re.compile(r"password|another.*(way|method)", re.I))
            if alt.count() > 0:
                alt.first.click()
                time.sleep(4)
        except Exception:
            pass
    try:
        page.wait_for_selector("input[type='password']", timeout=15000)
        if LOGIN_MODE == "otp":
            # Passwordless path: on the password page OpenAI shows a
            # "Log in with a one-time code" button. Click it instead of typing
            # the password, then fall through to the existing OTP-fetch machinery
            # below (need_otp becomes true on the verification page). Used for
            # accounts whose web password is unknown/stale but whose mailbox OTP
            # works (e.g. Aliyun accts onboarded only via codex OAuth).
            print("    LOGIN_MODE=otp → clicking 'Log in with a one-time code'", flush=True)
            clicked = False
            for _ in range(3):
                try:
                    otc = page.locator(
                        "button:has-text('one-time code'), a:has-text('one-time code'), "
                        "button:has-text('one time code'), a:has-text('one time code')")
                    if otc.count() > 0 and otc.first.is_visible():
                        otc.first.click()
                        clicked = True
                        time.sleep(4)
                        break
                except Exception as e:
                    print(f"    one-time-code click err: {str(e)[:80]}", flush=True)
                time.sleep(2)
            if not clicked:
                print("    one-time-code button not found → falling back to password", flush=True)
                page.locator("input[type='password']").first.click()
                page.keyboard.type(CHATGPT_PW, delay=80)
                ss(page, "pw-filled")
                submit_form(page)
            else:
                ss(page, "otc-requested")
            time.sleep(6)
            print(f"    after otc/pw url={page.url[:100]}", flush=True)
        else:
            page.locator("input[type='password']").first.click()
            page.keyboard.type(CHATGPT_PW, delay=80)
            ss(page, "pw-filled")
            submit_form(page)
            time.sleep(6)
            print(f"    after pw url={page.url[:100]}", flush=True)
    except Exception as e:
        print(f"    password step skipped: {e}", flush=True)

    need_otp = "verification" in page.url or "verification" in page.content().lower()[:5000]
    if LOGIN_MODE == "otp":
        # In passwordless mode we deliberately requested an email code, so the
        # verification page is expected even if the heuristic string isn't present.
        need_otp = True
    if not need_otp:
        for _ in range(15):
            if "verification" in page.url or "verification" in page.content().lower()[:3000]:
                need_otp = True
                break
            time.sleep(1)
    if need_otp:
        print("  need OTP - provider=%s (OTP_AUTO_ONLY=%s, OTP_AUTO_MAX=%s)" % (
            MAIL_OTP_PROVIDER, OTP_AUTO_ONLY, OTP_AUTO_MAX), flush=True)
        otp = None
        if MAIL_OTP_PROVIDER in ("imap_qq", "imap"):
            since_ts = int(time.time()) - 60
            otp, _ = imap_fetch_otp(since_ts, max_wait=min(OTP_FILE_WAIT, 180))
        elif OTP_AUTO_MAX > 0:
            try:
                mp = mailcom_login(ctx)
                # ① settle: 等→**刷新**→再等。本次的码是打开收件箱之后才到的,
                #    不刷新只看得到旧列表。
                settle_inbox(mp, "otp")
                # ② 穿透 Shadow DOM 的取码器(移植自 oauth.py, 同期在同样这些邮箱上
                #    取码成功)。mail.com 已把列表/正文搬进 Shadow DOM, innerText 恒空,
                #    所以它必须排在旧的 innerText 系实现**前面**。
                otp = mailcom_get_otp_shadowdom(mp)
                if otp:
                    print("  OTP via shadow-dom reader", flush=True)
                else:
                    # ③ 旧路径仅作兜底(innerText 系, 对当前 mail.com 基本无效,
                    #    留着是因为对老版页面/其他 webmail 仍可能命中)。
                    otp = mailcom_open_and_read_otp(mp)
                    if otp:
                        print("  OTP via open-and-read reading pane (legacy)", flush=True)
                    else:
                        otp = get_otp(mp)
                try:
                    mp.close()
                except Exception:
                    pass
            except Exception as e:
                print(f"  mail.com auto error: {e}", flush=True)
        elif OTP_SHOT:
            # Use the capture's working mail.com session to OPEN the newest code
            # email (via a cross-origin-safe frame locator) and screenshot it, so
            # an external reader can read the 6-digit code and inject it via file.
            try:
                mp = mailcom_login(ctx)
                opened = False
                for fsel in ["iframe[name='mail']", "iframe[src*='mail']", "iframe"]:
                    try:
                        fl = mp.frame_locator(fsel)
                        for needle in ["temporary ChatGPT login code", "ChatGPT login code", "login code"]:
                            loc = fl.get_by_text(needle, exact=False)
                            if loc.count() > 0:
                                loc.first.click(timeout=8000)
                                mp.wait_for_timeout(4000)
                                opened = True
                                break
                    except Exception as e:
                        print(f"  otpshot fl {fsel} err: {str(e)[:80]}", flush=True)
                    if opened:
                        break
                # the 6-digit code sits below the fold in the reading pane — scroll
                # down (mouse wheel over the message area) before screenshotting.
                try:
                    mp.mouse.move(700, 400)
                    for _ in range(5):
                        mp.mouse.wheel(0, 500)
                        mp.wait_for_timeout(400)
                except Exception as e:
                    print(f"  otpshot scroll err: {str(e)[:60]}", flush=True)
                mp.screenshot(path=OTP_SHOT_PATH, full_page=False)
                print(f"  OTP_SHOT saved: {OTP_SHOT_PATH} (opened={opened})", flush=True)
            except Exception as e:
                print(f"  OTP_SHOT error: {e}", flush=True)
        else:
            # OTP_AUTO_MAX=0 → skip the brittle webmail scraper entirely and go
            # straight to file-wait so a reliable external reader can inject the code.
            print("  OTP auto disabled (OTP_AUTO_MAX=0) → file-wait", flush=True)
        if not otp and not OTP_AUTO_ONLY:
            # file fallback when not in strict auto mode
            try:
                if os.path.exists(OTP_FILE):
                    os.remove(OTP_FILE)
            except Exception:
                pass
            print(f"  >>> OTP_WAIT_FILE: write the 6-digit code to {OTP_FILE} (waiting up to {OTP_FILE_WAIT}s)", flush=True)
            deadline = time.time() + OTP_FILE_WAIT
            while time.time() < deadline:
                try:
                    if os.path.exists(OTP_FILE):
                        v = open(OTP_FILE).read().strip()
                        m = re.search(r"\d{6}", v)
                        if m:
                            otp = m.group(0)
                            print(f"  got OTP from file: {otp}", flush=True)
                            break
                except Exception:
                    pass
                time.sleep(3)
        elif not otp and OTP_AUTO_ONLY:
            print("  OTP auto failed (OTP_AUTO_ONLY=1, no manual fallback)", flush=True)
        if otp:
            print(f"  OTP={otp}", flush=True)
            # 定位真正的 code 输入框:email-verification 页可能有 Email + Code 两个
            # input,locator("input").first 会命中 Email 框→OTP 打错位置→Code 空→
            # "verification code is required"。优先按 one-time-code/numeric 语义选,
            # 再退回最后一个可见 input(code 在 email 之后),用 fill 清旧值再填。
            otp_inp = None
            for sel in ["input[autocomplete='one-time-code']", "input[inputmode='numeric']",
                        "input[name='code']", "input[type='tel']"]:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    otp_inp = loc.first; break
            if otp_inp is None:
                vis = page.locator("input:visible")
                otp_inp = vis.last if vis.count() > 0 else page.locator("input").first
            try:
                otp_inp.click()
                otp_inp.fill("")
                otp_inp.fill(otp)
            except Exception:
                page.locator("input").first.click()
                page.keyboard.type(otp, delay=80)
            submit_form(page)
            time.sleep(8)
            print(f"    after OTP url={page.url[:100]}", flush=True)
            ss(page, "otp-submitted")
            # 2FA accounts: post-OTP lands on auth.openai.com/mfa-challenge/...
            # → answer the TOTP authenticator with a code from TOTP_SECRET.
            handle_mfa_challenge(page)
            # OpenAI rate-limits OTP submission on auth.openai.com/email-verification:
            # repeated capture retries → "Too many attempts / max_check_attempts".
            # Detect and fail-fast (cooldown ~10min) rather than fall through to SSO,
            # which would bounce us onto accounts.google.com sign-in (no composer).
            try:
                body_txt = page.inner_text("body", timeout=3000)
            except Exception:
                body_txt = ""
            if "max_check_attempts" in body_txt or "Too many attempts" in body_txt or "Too many tries" in body_txt:
                ss(page, "otp-rate-limited")
                sys.exit("❌ OpenAI OTP submission rate-limited (max_check_attempts) — wait ≥10min before retrying this account")
            # wait for the auth→chatgpt.com session callback to fully complete,
            # otherwise navigating away lands us in anonymous (logged-out) mode
            for _ in range(40):
                u = page.url
                if "chatgpt.com" in u and "auth" not in u and "verification" not in u:
                    break
                # click any post-OTP continue / stay-signed-in prompts
                for sel in ["button:has-text('Continue')", "button:has-text('Yes')",
                            "button:has-text('Stay signed in')",
                            "button:has-text('Verify')", "[data-testid='continue-button']"]:
                    try:
                        loc = page.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click()
                            time.sleep(2)
                    except Exception:
                        pass
                time.sleep(2)
            print(f"    post-OTP settled url={page.url[:100]}", flush=True)
            ss(page, "post-otp-settled")
            # late-cookie: the chatgpt.com session can land a few seconds after the
            # OAuth callback; reload a few times before treating it as anonymous.
            for r in range(4):
                if is_logged_in(page):
                    break
                print(f"    post-OTP not logged-in yet, reload {r+1}/4", flush=True)
                try:
                    page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                    time.sleep(5)
                    clear_cf(page)
                    time.sleep(3)
                except Exception:
                    pass
            print(f"    post-OTP login state={is_logged_in(page)} url={page.url[:80]}", flush=True)
        else:
            print("  OTP fetch failed", flush=True)


# ── main: login → send message → capture f/conversation request ───────────
captured = {"done": False, "data": None}


def main():
    PROFILE_DIR = os.environ.get("PROFILE_DIR", "/work/profile")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        time.sleep(5)
        clear_cf(page)
        time.sleep(2)
        logged_in = True
        if os.environ.get("FORCE_LOGIN") == "1":
            # Deterministic onboarding: never trust the persisted-session
            # heuristic (it false-positives on CF / /auth/login pages).
            logged_in = False
            print("[1] FORCE_LOGIN=1 → forcing full password+OTP login", flush=True)
        else:
            try:
                li = page.locator("button:has-text('Log in'), a:has-text('Log in')")
                if li.count() > 0 and li.first.is_visible():
                    logged_in = False
            except Exception:
                pass
        if not logged_in:
            print("[1] not logged in → running login flow", flush=True)
            login_chatgpt(ctx, page)
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            time.sleep(5)
            clear_cf(page)
            time.sleep(2)
        else:
            print("[1] reusing persisted session (already logged in)", flush=True)

        # verify logged-in; if anonymous, trigger silent SSO (auth cookie exists,
        # no OTP needed) by clicking Log in and waiting for redirect back
        if not is_logged_in(page):
            print("[1b] still anonymous → silent SSO via Log in", flush=True)
            for attempt in range(3):
                try:
                    lg = page.locator("button:has-text('Log in'), a:has-text('Log in')")
                    if lg.count() > 0:
                        lg.first.click(force=True)
                        time.sleep(4)
                        clear_cf(page)
                        # may show an account chooser / continue
                        for sel in ["button:has-text('Continue')",
                                    f"button:has-text('{EMAIL}')",
                                    "[data-testid='continue-button']"]:
                            try:
                                loc = page.locator(sel)
                                if loc.count() > 0 and loc.first.is_visible():
                                    loc.first.click()
                                    time.sleep(3)
                            except Exception:
                                pass
                except Exception as e:
                    print(f"    sso click err: {e}", flush=True)
                for _ in range(20):
                    if "chatgpt.com" in page.url and "auth" not in page.url:
                        break
                    time.sleep(2)
                # if stuck on auth.openai.com (OAuth callback), navigate back
                if "chatgpt.com" not in page.url or "auth" in page.url:
                    page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                    time.sleep(5)
                    clear_cf(page)
                if is_logged_in(page) and "chatgpt.com" in page.url:
                    print("[1b] SSO success — now logged in", flush=True)
                    break
                # if SSO bounced to full login, run the password+OTP flow
                if "auth.openai.com" in page.url or "/auth/login" in page.url:
                    print("[1b] SSO needs full login → running login flow", flush=True)
                    login_chatgpt(ctx, page)
                    page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                    time.sleep(5)
                    clear_cf(page)
                    if is_logged_in(page):
                        break
        ss(page, "app-loaded")
        print(f"[1] logged_in={is_logged_in(page)} url={page.url[:80]}", flush=True)

        # attach request capture for the REAL conversation POST (not /prepare)
        def on_request(req):
            try:
                u = req.url
                if req.method == "POST" and "/backend-api/" in u:
                    print(f"  [POST] {u}", flush=True)
                path = u.split("?")[0].rstrip("/")
                is_conv = req.method == "POST" and (
                    path.endswith("/backend-api/f/conversation")
                    or path.endswith("/backend-api/conversation")
                )
                if is_conv:
                    if captured["done"]:
                        return
                    hdrs = req.all_headers()
                    pd = req.post_data
                    body = {}
                    if pd:
                        try:
                            body = json.loads(pd)
                        except Exception:
                            body = {}
                    captured["data"] = {"url": u, "method": "POST", "headers": hdrs, "body": body}
                    captured["done"] = True
                    print(f"  [CAPTURED] {u}  headers={len(hdrs)} bodyKeys={list(body.keys())[:6]}", flush=True)
            except Exception as e:
                print(f"  on_request err: {e}", flush=True)

        page.on("request", on_request)

        # dismiss any promo/announcement modal (e.g. "ChatGPT Images 2.0")
        # whose transparent backdrop intercepts composer clicks
        for _ in range(3):
            for sel in [
                "[data-testid='modal-close-button']",
                "button[aria-label='Close']",
                "button[aria-label='Close dialog']",
                "div[role='dialog'] button:has-text(\"Okay, let's go\")",
                "div[role='dialog'] button:has-text('Okay')",
                "div[role='dialog'] button:has-text('Got it')",
                "div[role='dialog'] button:has-text('Continue')",
                "div[role='dialog'] button:has-text('Stay logged out')",
                "div[role='dialog'] button:has(svg)",
            ]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        print(f"    dismissed modal via {sel}", flush=True)
                        time.sleep(1)
                except Exception:
                    pass
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(1)
        ss(page, "after-modal-dismiss")

        if not is_logged_in(page):
            ss(page, "still-anon")
            sys.exit("❌ still logged out (anonymous) — refusing to capture anonymous session")

        # type a prompt into the composer and send
        print(f"[2] send prompt to trigger capture: {PROMPT!r}", flush=True)
        composer = None
        for sel in ["#prompt-textarea", "div[contenteditable='true']", "textarea"]:
            try:
                page.wait_for_selector(sel, timeout=15000)
                composer = page.locator(sel).first
                if composer.count() > 0:
                    break
            except Exception:
                continue
        if composer is None:
            ss(page, "no-composer")
            sys.exit("❌ composer not found")
        try:
            composer.click(timeout=8000)
        except Exception:
            try:
                composer.click(force=True, timeout=8000)
            except Exception:
                page.evaluate("() => { const e=document.querySelector('#prompt-textarea'); if(e) e.focus(); }")
        page.keyboard.type(PROMPT, delay=60)
        time.sleep(1)
        ss(page, "prompt-typed")
        # try send button, fallback Enter
        sent = False
        for sel in ["button[data-testid='send-button']", "button[aria-label*='Send']"]:
            try:
                b = page.locator(sel)
                if b.count() > 0 and b.first.is_enabled():
                    b.first.click()
                    sent = True
                    break
            except Exception:
                pass
        if not sent:
            page.keyboard.press("Enter")

        # wait for capture
        for _ in range(60):
            if captured["done"]:
                break
            time.sleep(1)
        ss(page, "after-send")

        if not captured["done"]:
            sys.exit("❌ never captured /backend-api/f/conversation POST")

        data = captured["data"]
        # sanity: must contain sentinel proof token + cookie
        h = {k.lower(): v for k, v in data["headers"].items()}
        if "openai-sentinel-proof-token" not in h:
            print("  ⚠ WARNING: openai-sentinel-proof-token missing from captured headers!", flush=True)
        if "cookie" not in h:
            print("  ⚠ WARNING: cookie missing from captured headers!", flush=True)

        users = {"chatgpt": {ZK_USER: {"username": ZK_USER, "parsedFetch": data, "sessions": []}}}
        os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
        with open(OUT_JSON, "w") as f:
            json.dump(users, f, indent=2)
        print(f"✅ wrote {OUT_JSON}", flush=True)
        print(f"   headers captured: {sorted(h.keys())}", flush=True)

        ctx.close()


if __name__ == "__main__":
    main()
