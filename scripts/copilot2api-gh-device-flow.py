#!/usr/bin/env python3
"""copilot2api-gh-device-flow

GitHub Device Flow 全自动授权（Playwright）。

用法（在 188 上跑）：
  ssh cltx@10.68.13.188
  umask 077
  printf %s "<GH_PASSWORD>" > /tmp/gh_pw.txt   # bulk-provision 邮箱常见 mail_pw==GH_pw
  printf %s "<MAIL_PASSWORD>" > /tmp/mail_pw.txt
  chmod 600 /tmp/gh_pw.txt /tmp/mail_pw.txt
  mkdir -p /tmp/gh_shots

  # 225 上先启动临时 auth unit 拿到 8 位 code：
  #   sudo systemd-run --unit=co-acct-N-auth --property=User=cltx ... /Data/copilot2api/bin/copilot2api
  #   journalctl -u co-acct-N-auth -n 20 --no-pager | grep code

  docker run --rm --network host \
    -v /tmp/copilot2api-gh-device-flow.py:/work/gh_device_flow.py:ro \
    -v /tmp/gh_pw.txt:/run/gh_pw.txt:ro \
    -v /tmp/mail_pw.txt:/run/mail_pw.txt:ro \
    -v /tmp/gh_totp.txt:/run/gh_totp.txt:ro \
    -v /tmp/gh_shots:/work/screenshots \
    -e GH_EMAIL=uxaylntbvsxfp@mail.com \
    -e GH_PASSWORD_FILE=/run/gh_pw.txt \
    -e GH_TOTP_FILE=/run/gh_totp.txt \
    -e GH_DEVICE_CODE=XXXX-XXXX \
    -e MAIL_EMAIL=uxaylntbvsxfp@mail.com \
    -e MAIL_PW_FILE=/run/mail_pw.txt \
    -e SCREEN_DIR=/work/screenshots \
    -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    mcr.microsoft.com/playwright/python:v1.49.0-noble \
    bash -c 'pip install --quiet --root-user-action=ignore playwright==1.49.0 && python3 /work/gh_device_flow.py'

跑完清理凭据：`shred -u /tmp/gh_pw.txt /tmp/mail_pw.txt; rm -rf /tmp/gh_shots`。

三个已知坑（09-03 实证）：
  1. `github.com/login/device` 未登录直接给 Sign-in 页，不是码页。
  2. 登录后 GH 弹 `/login/device/select_account`（"Device Activation, Signed in as X, Continue"）;
     "Continue" 是 `<input type=submit value="Continue" name="commit">`，不是 `<button>`；用
     `form input[type=submit]` 才匹配得到。
  3. 8 位码不是单 input，是 8 个分位 `input#user-code-{0..3,5..8}`，位 4 是只读连字符；
     按位 `fill()` + `form[action*='device/confirmation'] input[type='submit']` 提交，
     然后 role button "Authorize" 上钟到 `/login/device/success`。

失败信号（脚本内已处理）：
  - "You can't perform that action at this time"：select_account 页 CSRF 抖动；reload 刷新新 token 后重试。
  - authenticator-app 2FA（`/sessions/two-factor/app`，`input#app_totp`）：给 `GH_TOTP_FILE`
    放 base32 secret，脚本 stdlib 现算 TOTP；窗口剩余 <5s 会等到下一窗口再填。
  - 需邮箱 device-verification code：GH 会发到 mail.com，登进去 iframe[name=mail] 抓最新
    GitHub 邮件里的 6 位数字回填 `input[name='device-verification-code']`。
  - "Expired user code"：15 分钟窗口过期；停 auth unit 重开取新码，勿在旧码上死磕。
"""
from __future__ import annotations
import os, sys, time, re, pathlib, traceback
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


GH_EMAIL   = os.environ["GH_EMAIL"]
GH_PW      = pathlib.Path(os.environ["GH_PASSWORD_FILE"]).read_text().strip()
CODE       = os.environ["GH_DEVICE_CODE"].strip().upper()
MAIL_EMAIL = os.environ.get("MAIL_EMAIL", "")
MAIL_PW    = pathlib.Path(os.environ["MAIL_PW_FILE"]).read_text().strip() if os.environ.get("MAIL_PW_FILE") else ""
TOTP_SEC   = (pathlib.Path(os.environ["GH_TOTP_FILE"]).read_text().strip()
              if os.environ.get("GH_TOTP_FILE") else "").replace(" ", "").upper()
SCREEN     = pathlib.Path(os.environ.get("SCREEN_DIR", "/work/screenshots"))
SCREEN.mkdir(parents=True, exist_ok=True)


def log(*a): print("[gh]", *a, flush=True)


def totp_now(secret: str) -> tuple[str, int]:
    """RFC6238 TOTP (SHA1/30s/6 digits) — stdlib only, no pyotp in the PW image.
    Returns (code, seconds_left_in_window)."""
    import hmac, hashlib, struct, base64
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    now = int(time.time())
    d = hmac.new(key, struct.pack(">Q", now // 30), hashlib.sha1).digest()
    o = d[-1] & 0x0F
    code = str((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 1000000).zfill(6)
    return code, 30 - (now % 30)


def handle_totp(page) -> bool:
    """GitHub authenticator-app 2FA page (`/sessions/two-factor/app`, `input#app_totp`).
    Distinct from the email device-verification page handled by fetch_mail_otp():
    detect on URL / `#app_totp` only — `input[autocomplete=one-time-code]` matches both."""
    if not TOTP_SEC:
        return False
    if "two-factor" not in page.url and page.locator("input#app_totp").count() == 0:
        return False
    log("2FA app page:", page.url)
    snap(page, "021_totp_page")
    code, left = totp_now(TOTP_SEC)
    if left < 5:                      # don't submit a code about to roll over
        log(f"totp window {left}s left, waiting for next"); time.sleep(left + 1)
        code, left = totp_now(TOTP_SEC)
    log(f"totp {code} ({left}s left)")
    box = page.locator("input#app_totp, input[name='app_otp']").first
    box.click(); box.fill(code)
    # GH auto-submits on the 6th digit; click only if the form is still there.
    try:
        page.get_by_role("button", name=re.compile("Verify|Continue|Submit|Sign in", re.I)).first.click(timeout=4000)
    except Exception:
        pass
    page.wait_for_load_state("networkidle", timeout=45000)
    snap(page, "022_after_totp")
    log("after totp url", page.url)
    return True


def snap(page, name):
    p = SCREEN / f"{int(time.time())}_{name}.png"
    try: page.screenshot(path=str(p), full_page=True); log("snap", p)
    except Exception as e: log("snap fail", e)


def dump_html(page, name):
    p = SCREEN / f"{int(time.time())}_{name}.html"
    try: p.write_text(page.content()); log("html", p)
    except Exception as e: log("html fail", e)


def fetch_mail_otp():
    """log into mail.com, iframe[name=mail], grab newest GitHub 6-digit code."""
    if not MAIL_EMAIL or not MAIL_PW:
        log("mail: no creds"); return None
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        p = ctx.new_page()
        try:
            p.goto("https://www.mail.com/", timeout=60000)
            for sel in ["button:has-text('Accept')", "button:has-text('Agree')", "#onetrust-accept-btn-handler"]:
                try: p.locator(sel).first.click(timeout=2000); break
                except Exception: pass
            try:
                p.get_by_role("link", name=re.compile("^Log ?in$", re.I)).first.click(timeout=8000)
            except Exception:
                p.get_by_role("button", name=re.compile("^Log ?in$", re.I)).first.click(timeout=8000)
            p.wait_for_load_state("networkidle", timeout=30000)
            p.locator("input[name='username'], input#login-email, input[type='email']").first.fill(MAIL_EMAIL)
            p.locator("input[name='password'], input#login-password, input[type='password']").first.fill(MAIL_PW)
            p.get_by_role("button", name=re.compile("Log ?in|Sign ?in", re.I)).first.click()
            p.wait_for_load_state("networkidle", timeout=45000)
            fr = None
            for _ in range(20):
                for f in p.frames:
                    if f.name == "mail": fr = f; break
                if fr: break
                time.sleep(1)
            target = fr if fr else p
            html = target.content() if hasattr(target, "content") else p.content()
            gh_idx = re.search(r"GitHub|verification code|device verification", html, re.I)
            snippet = html[max(0, gh_idx.start() - 500):gh_idx.start() + 3000] if gh_idx else html
            m = re.search(r"(?<!\d)(\d{6})(?!\d)", snippet)
            if m: log("mail: otp", m.group(1)); return m.group(1)
            log("mail: no OTP visible"); snap(p, "mail_no_otp"); return None
        except Exception as e:
            log("mail: exception", e); traceback.print_exc()
            snap(p, "mail_error"); return None
        finally:
            b.close()


def run():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width": 1400, "height": 900},
                            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
        p = ctx.new_page()
        p.set_default_timeout(30000)

        log("goto device page")
        p.goto("https://github.com/login/device", timeout=60000)
        p.wait_for_load_state("networkidle", timeout=30000)
        snap(p, "01_device_landing")
        log("landing url", p.url)

        if "/login" in p.url or p.locator("input#login_field").count() > 0:
            log("logging in")
            p.locator("input#login_field").fill(GH_EMAIL)
            p.locator("input#password").fill(GH_PW)
            p.get_by_role("button", name=re.compile("^Sign in$", re.I)).first.click()
            p.wait_for_load_state("networkidle", timeout=45000)
            snap(p, "02_after_signin")

            handle_totp(p)   # authenticator-app 2FA, before select_account

            # select_account intermediate page
            if "/login/device/select_account" in p.url or "Device Activation" in (
                p.locator("body").inner_text(timeout=2000) or ""
            ):
                log("select_account page")
                try:
                    err = p.locator(".flash-error, .js-flash-alert").inner_text(timeout=1500).strip()
                except Exception:
                    err = ""
                if err:
                    log("reload to shed flash-error:", err[:200])
                    p.goto(p.url, timeout=30000)
                    p.wait_for_load_state("networkidle", timeout=20000)
                    time.sleep(1)
                snap(p, "025_select_account")
                clicked = False
                for sel in [
                    "form[action*='device'] input[type='submit']",
                    "form input[type='submit']",
                    "input[value='Continue']",
                    "button:has-text('Continue')",
                ]:
                    try:
                        p.locator(sel).first.wait_for(state="visible", timeout=5000)
                        p.locator(sel).first.click(); clicked = True; log("continue via", sel); break
                    except Exception:
                        continue
                if not clicked:
                    try:
                        p.evaluate("document.querySelector('form').submit()"); clicked = True
                    except Exception as e:
                        log("form.submit fail", e)
                p.wait_for_load_state("networkidle", timeout=30000)

            # 2FA / device-verification email
            handle_totp(p)   # 2FA can also land here, after select_account
            body = ""
            try: body = p.locator("body").inner_text(timeout=3000) or ""
            except Exception: pass
            if re.search(r"verification code|Device verification|Verify your identity|check your email|two-factor", body, re.I):
                log("device verification page")
                snap(p, "03_otp_page"); dump_html(p, "03_otp_page")
                otp = None
                for i in range(8):
                    time.sleep(15)
                    otp = fetch_mail_otp()
                    if otp: break
                    log(f"otp poll {i+1}/8 empty")
                if not otp:
                    log("no OTP obtained, aborting"); sys.exit(11)
                otp_in = p.locator("input#otp, input[name='otp'], input[autocomplete='one-time-code'], input[name='device-verification-code']").first
                otp_in.fill(otp)
                try: p.get_by_role("button", name=re.compile("Verify|Continue|Submit", re.I)).first.click(timeout=5000)
                except Exception: p.keyboard.press("Enter")
                p.wait_for_load_state("networkidle", timeout=45000)

        # code entry — 8 split inputs
        snap(p, "05_code_page")
        code_chars = CODE.replace("-", "")
        if len(code_chars) != 8:
            log("bad code:", CODE); sys.exit(13)
        try:
            p.locator("input#user-code-0").wait_for(state="visible", timeout=8000)
        except Exception as e:
            dump_html(p, "05_no_code_input"); log("no code input:", e); sys.exit(12)
        for i, pos in enumerate([0, 1, 2, 3, 5, 6, 7, 8]):
            inp = p.locator(f"input#user-code-{pos}")
            inp.click(); inp.fill(code_chars[i])
        snap(p, "06_code_typed")
        try:
            p.locator("form[action*='device/confirmation'] input[type='submit']").first.click(timeout=8000)
        except Exception:
            try: p.locator("input[name='commit'][value='Continue']").first.click(timeout=5000)
            except Exception: p.keyboard.press("Enter")
        p.wait_for_load_state("networkidle", timeout=30000)
        snap(p, "07_after_code_submit")

        # authorize
        clicked = False
        for label in ["Authorize", "Continue", "Connect", "Confirm", "Allow", "Grant access"]:
            try:
                btn = p.get_by_role("button", name=re.compile(f"^{label}$", re.I)).first
                btn.wait_for(state="visible", timeout=8000)
                btn.click(); log("clicked:", label); clicked = True; break
            except Exception:
                continue
        if not clicked:
            try: p.locator("button:has-text('Authorize'), button:has-text('Continue')").first.click(timeout=5000)
            except Exception as e: log("no grant button:", e)
        p.wait_for_load_state("networkidle", timeout=30000)
        snap(p, "08_final")
        body = ""
        try: body = p.locator("body").inner_text(timeout=3000) or ""
        except Exception: pass
        log("final url", p.url)
        log("final body:", (body[:600]).replace("\n", " | "))
        b.close()


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        print("[gh] FATAL", e); traceback.print_exc(); sys.exit(1)
