#!/usr/bin/env python3
"""xai_device_capture.py — 阿里云新加坡 EIP 节点上跑 xAI/Grok 设备码 OAuth 捕获。

设计原则(对齐 CLAUDE.md 诊断纪律 + 用户"不要硬搞"):
  - **一次干净尝试**, 不做暴力重撞。Turnstile 若明确 block, 打印形态 + 截图后干净退出。
  - 全程截图 + DOM dump 落 /work, token 只写文件不打印明文。
  - egress 先自检: 必须是 EIP(47.236.200.98 / 47.84.85.100), 不是共享 NAT 47.84.112.136。

流程(grok-cli 设备码授权):
  1. POST auth.x.ai/oauth2/device/code → user_code + verification_uri_complete
  2. 浏览器开 verification_uri_complete → 未登录则走 accounts.x.ai/sign-in
     (email → submit → password → Turnstile → submit) → 设备授权页 Approve
  3. 轮询 auth.x.ai/oauth2/token(device_code grant) → access_token + refresh_token
  4. 写 /work/grok_oauth-<tag>.json(grok-proxy 需要的 bundle)

env:
  MAIL_USER      登录邮箱
  XAI_PW_FILE    密码文件路径(不落 argv)
  SCREENSHOT_DIR 截图目录(默认 /work/ss-xai)
  OAUTH_OUTPUT   token 输出路径(默认 /work/grok_oauth.json)
  TAG            产物标签
"""
import json, os, time, urllib.request, urllib.parse, urllib.error
from patchright.sync_api import sync_playwright

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
DEVICE_URL = "https://auth.x.ai/oauth2/device/code"

EMAIL = os.environ.get("MAIL_USER", "")
PW_FILE = os.environ.get("XAI_PW_FILE", "/run/xai_pw")
PW = ""
if os.path.exists(PW_FILE):
    with open(PW_FILE) as f:
        PW = f.read().strip()
SS = os.environ.get("SCREENSHOT_DIR", "/work/ss-xai")
OUT = os.environ.get("OAUTH_OUTPUT", "/work/grok_oauth.json")
TAG = os.environ.get("TAG", "xai")
os.makedirs(SS, exist_ok=True)


def log(*a):
    print(*a, flush=True)


def http_post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def device_code():
    st, body = http_post_form(DEVICE_URL,
        {"client_id": CLIENT_ID, "scope": SCOPE})
    log(f"[device/code] http={st}")
    return json.loads(body)


def bodytext(pg):
    try:
        return " ".join(pg.evaluate("()=>document.body.innerText").split())
    except Exception:
        return ""


def ts_token(pg):
    try:
        return pg.eval_on_selector("input[name=cf-turnstile-response]", "el=>el.value") or ""
    except Exception:
        return ""


def shot(pg, name):
    try:
        pg.screenshot(path=f"{SS}/{name}.png", full_page=True)
        log(f"  [shot] {name}.png")
    except Exception as e:
        log(f"  [shot-err] {name}: {e}")


def dump(pg, tag):
    log(f"\n===== DUMP {tag}  url={pg.url}")
    log("[text]", bodytext(pg)[:700])
    for sel in ["input", "button"]:
        for e in pg.query_selector_all(sel)[:15]:
            try:
                t = (e.inner_text() or "").strip()
                nm = e.get_attribute("name") or ""
                ty = e.get_attribute("type") or ""
                dt = e.get_attribute("data-testid") or ""
                if t or nm or ty or dt:
                    log(f"  <{sel}> text={t[:30]!r} name={nm!r} type={ty!r} testid={dt!r}")
            except Exception:
                pass
    shot(pg, tag)


def classify_turnstile(pg):
    """报告 Turnstile 形态: PASS_AUTO / INTERACTIVE / BLOCKED / NONE"""
    tok = ts_token(pg)
    if tok:
        return "PASS_AUTO", len(tok)
    bt = bodytext(pg).lower()
    if "verification failed" in bt or "could not verify" in bt:
        return "BLOCKED", 0
    frs = [f for f in pg.frames if "challenges.cloudflare.com" in (f.url or "")]
    if frs:
        return "INTERACTIVE", 0
    return "NONE", 0


def try_click_turnstile(pg):
    """一次干净点击尝试(非暴力), 优先 iframe 内 checkbox, 回退鼠标坐标"""
    try:
        fl = pg.frame_locator("iframe[src*='challenges.cloudflare.com']")
        for sel in ["input[type=checkbox]", "label", "body"]:
            try:
                loc = fl.locator(sel).first
                loc.click(timeout=4000)
                log(f"  [ts-click] iframe {sel}")
                return True
            except Exception:
                continue
    except Exception:
        pass
    # 鼠标坐标回退
    try:
        ifr = pg.query_selector("iframe[src*='challenges.cloudflare.com']")
        if ifr:
            b = ifr.bounding_box()
            cx, cy = b["x"] + 30, b["y"] + b["height"] / 2
            pg.mouse.move(cx - 60, cy - 30, steps=10); time.sleep(0.4)
            pg.mouse.move(cx, cy, steps=15); time.sleep(0.4)
            pg.mouse.click(cx, cy, delay=110)
            log(f"  [ts-click] mouse {int(cx)},{int(cy)}")
            return True
    except Exception as e:
        log("  [ts-click-err]", e)
    return False


def wait_turnstile(pg, budget_s=40):
    """等 token 出现; 首个 INTERACTIVE 出现时点一次; BLOCKED 立即返回(不重撞)"""
    clicked = False
    deadline = time.time() + budget_s
    while time.time() < deadline:
        kind, ln = classify_turnstile(pg)
        if kind == "PASS_AUTO":
            log(f"[TS] PASS_AUTO token_len={ln}")
            return "TOKEN"
        if kind == "BLOCKED":
            log("[TS] BLOCKED (verification failed) — 干净退出, 不重撞")
            shot(pg, f"{TAG}-ts-blocked")
            return "BLOCKED"
        if kind == "INTERACTIVE" and not clicked:
            log("[TS] INTERACTIVE checkbox — 单次点击")
            try_click_turnstile(pg)
            clicked = True
        time.sleep(2)
    return "NOTOKEN"


def do_login(pg):
    """accounts.x.ai sign-in: email → password → turnstile → submit"""
    pg.wait_for_selector("input[type=email]", timeout=25000)
    pg.query_selector("input[type=email]").fill(EMAIL)
    dump(pg, f"{TAG}-01-email")
    click_any(pg, testids=["sign-in-submit"], texts=["Continue", "Next", "Sign in", "Submit"])
    pg.wait_for_selector("input[type=password]", timeout=25000)
    pg.query_selector("input[type=password]").fill(PW)
    time.sleep(2)
    dump(pg, f"{TAG}-02-password")
    ts = wait_turnstile(pg, 45)
    if ts == "BLOCKED":
        return "TS_BLOCKED"
    # 提交登录
    click_any(pg, testids=["sign-in-submit"], texts=["Sign in", "Continue", "Log in", "Submit"])
    log("[login] submitted")
    time.sleep(5)
    dump(pg, f"{TAG}-03-postlogin")
    bt = bodytext(pg).lower()
    for k in ["incorrect", "invalid", "wrong", "does not match", "too many", "couldn"]:
        if k in bt:
            return "BADPW"
    if "verification code" in bt or "enter the code" in bt or "one-time code" in bt:
        return "NEEDS_OTP"
    return "LOGGED_IN"


def click_any(pg, testids=(), texts=()):
    """按 testid 优先, 再按可见文字点第一个匹配按钮。返回是否点到。"""
    for t in testids:
        b = pg.query_selector(f"button[data-testid='{t}'], [data-testid='{t}']")
        if b:
            try:
                b.click(timeout=8000); log(f"  [click] testid={t}"); return True
            except Exception as e:
                log(f"  [click-err] testid={t}: {str(e)[:80]}")
    for t in texts:
        b = pg.query_selector(f"button:has-text('{t}')")
        if b:
            try:
                b.click(timeout=8000); log(f"  [click] text={t!r}"); return True
            except Exception as e:
                log(f"  [click-err] text={t}: {str(e)[:80]}")
    return False


def dismiss_consent(pg):
    """OneTrust cookie 横幅可能拦点击 — 有就 Allow All / Accept 掉。"""
    for sel in ["#onetrust-accept-btn-handler", "button:has-text('Allow All')",
                "button:has-text('Accept All')", "button:has-text('Accept')"]:
        b = pg.query_selector(sel)
        if b:
            try:
                b.click(timeout=4000); log(f"  [consent] dismissed via {sel}"); time.sleep(1); return
            except Exception:
                pass


def do_approve(pg, tag):
    """设备确认 / 授权页: 找 Continue/Approve/Authorize 点掉。"""
    time.sleep(2)
    dump(pg, tag)
    clicked = click_any(pg, testids=["confirm", "approve", "authorize"],
                        texts=["Continue", "Approve", "Authorize", "Allow", "Confirm", "Yes"])
    if clicked:
        time.sleep(4)
    else:
        log("[approve] 未找到按钮 (可能已自动授权)")
    return clicked


def poll_token(device_code_val, interval, expires):
    log(f"[token] 轮询 (interval={interval}s expires={expires}s)")
    deadline = time.time() + min(expires, 180)
    while time.time() < deadline:
        st, body = http_post_form(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code_val, "client_id": CLIENT_ID})
        try:
            j = json.loads(body)
        except Exception:
            j = {"raw": body[:200]}
        if st == 200 and "access_token" in j:
            log("[token] SUCCESS")
            return j
        err = j.get("error", "")
        log(f"[token] http={st} error={err}")
        if err not in ("authorization_pending", "slow_down"):
            log(f"[token] 终止: {body[:200]}")
            return None
        time.sleep(interval + (5 if err == "slow_down" else 0))
    log("[token] 超时")
    return None


def main():
    if not EMAIL or not PW:
        log(f"FATAL: MAIL_USER='{EMAIL}' PW_present={bool(PW)}")
        return
    d = device_code()
    log(f"USER_CODE={d.get('user_code')}  verify={d.get('verification_uri_complete')}")
    interval = int(d.get("interval", 5))
    expires = int(d.get("expires_in", 600))

    proxy = os.environ.get("PROXY", "")  # e.g. socks5://127.0.0.1:40000 (WARP)
    launch_kw = dict(headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--use-gl=angle", "--use-angle=swiftshader"])
    if proxy:
        launch_kw["proxy"] = {"server": proxy}
        log(f"[proxy] browser via {proxy}")
    with sync_playwright() as p:
        br = p.chromium.launch(**launch_kw)
        ctx = br.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
        pg = ctx.new_page()
        # egress 自检
        try:
            pg.goto("https://ipinfo.io/json", wait_until="domcontentloaded", timeout=45000)
            log("[egress]", bodytext(pg)[:200])
        except Exception as e:
            log("[egress-err]", e)

        try:
            pg.goto(d["verification_uri_complete"], wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
            dump(pg, f"{TAG}-00-landing")
        except Exception as e:
            log("[goto-err]", e)
            shot(pg, f"{TAG}-goto-err")
            br.close(); return

        bt = bodytext(pg).lower()
        if "abusive traffic" in bt or "blocked due to" in bt:
            log("[BLOCK] 'abusive traffic' — 此出口被 x.ai 硬封, 干净退出")
            br.close(); return

        # ── Step 1: 设备码确认页 (Sign in to Grok / Enter the code / Continue) ──
        dismiss_consent(pg)
        click_any(pg, testids=["confirm", "continue"], texts=["Continue", "Sign in"])
        time.sleep(3)
        dump(pg, f"{TAG}-10-after-device-confirm")

        # ── Step 2: 登录方式选择页 → Login with email ──
        dismiss_consent(pg)
        if pg.query_selector("[data-testid='continue-with-email']") or "login with email" in bodytext(pg).lower():
            click_any(pg, testids=["continue-with-email"], texts=["Login with email", "Continue with email"])
            time.sleep(2)
            dump(pg, f"{TAG}-11-email-form")

        # ── Step 3-5: email → password → turnstile → submit ──
        state = "?"
        if pg.query_selector("input[type=email]"):
            state = do_login(pg)
            log("[login-result]", state)
            if state in ("TS_BLOCKED", "BADPW", "NEEDS_OTP"):
                log(f"STOP: {state} — 见截图, 不继续")
                br.close(); return
        else:
            log("[warn] 未见 email 输入框, 直接尝试授权页")

        # ── Step 6: 登录后回到设备授权页, 确认授权 ──
        do_approve(pg, f"{TAG}-20-approve")
        # 有些流程需二次确认
        if "/oauth2/device" in pg.url or "authorize" in bodytext(pg).lower():
            do_approve(pg, f"{TAG}-21-approve2")

        tok = poll_token(d["device_code"], interval, expires)
        if tok:
            bundle = {
                "access_token": tok.get("access_token"),
                "refresh_token": tok.get("refresh_token"),
                "token_type": tok.get("token_type", "Bearer"),
                "scope": tok.get("scope", SCOPE),
                "expires_in": tok.get("expires_in"),
                "client_id": CLIENT_ID,
                "obtained_at": int(time.time()),
            }
            with open(OUT, "w") as f:
                json.dump(bundle, f, indent=2)
            os.chmod(OUT, 0o600)
            log(f"OAUTH_CAPTURED → {OUT}  (access_token_len={len(bundle['access_token'] or '')}, "
                f"has_refresh={bool(bundle['refresh_token'])})")
        else:
            log("OAUTH_FAILED — token 未获取")
        br.close()


if __name__ == "__main__":
    main()
