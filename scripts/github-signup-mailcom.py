#!/usr/bin/env python3
"""github-signup-mailcom

GitHub 注册全自动（Playwright，在 188 上跑），邮箱侧为 mail.com（bulk-provision 账号池）。

用法（188）：
  umask 077
  printf %s "<GH_PASSWORD>"   > /tmp/gh_pw.txt
  printf %s "<MAIL_PASSWORD>" > /tmp/mail_pw.txt
  mkdir -p /tmp/gh_shots
  docker run --rm --network host \
    -v /tmp/github-signup-mailcom.py:/work/signup.py:ro \
    -v /tmp/gh_pw.txt:/run/gh_pw.txt:ro -v /tmp/mail_pw.txt:/run/mail_pw.txt:ro \
    -v /tmp/gh_shots:/work/screenshots \
    -e GH_EMAIL=xxx@mail.com -e GH_USERNAME=xxx -e GH_PASSWORD_FILE=/run/gh_pw.txt \
    -e MAIL_EMAIL=xxx@mail.com -e MAIL_PW_FILE=/run/mail_pw.txt \
    -e SCREEN_DIR=/work/screenshots -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    mcr.microsoft.com/playwright/python:v1.49.0-noble \
    bash -c 'pip install --quiet --root-user-action=ignore playwright==1.49.0 && python3 /work/signup.py'

退出码：0 成功（落到 github.com 已登录态）；20 CAPTCHA 卡住；21 邮箱取码失败；22 用户名/邮箱被拒；1 其它。
每一步都落截图 + html 到 SCREEN_DIR 便于复盘。
"""
from __future__ import annotations
import os, re, sys, time, json, pathlib, traceback
from playwright.sync_api import sync_playwright

GH_EMAIL   = os.environ["GH_EMAIL"].strip()
GH_USER    = os.environ["GH_USERNAME"].strip()
GH_PW      = pathlib.Path(os.environ["GH_PASSWORD_FILE"]).read_text().strip()
MAIL_EMAIL = os.environ.get("MAIL_EMAIL", GH_EMAIL).strip()
MAIL_PW    = pathlib.Path(os.environ["MAIL_PW_FILE"]).read_text().strip()
SCREEN     = pathlib.Path(os.environ.get("SCREEN_DIR", "/work/screenshots")); SCREEN.mkdir(parents=True, exist_ok=True)
COUNTRY    = os.environ.get("GH_COUNTRY", "Singapore")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
START_TS = time.time()


def log(*a): print("[signup]", *a, flush=True)


def snap(page, name):
    p = SCREEN / f"{int(time.time())}_{name}"
    try:
        page.screenshot(path=str(p) + ".png", full_page=True)
        (p.with_suffix(".html")).write_text(page.content())
        log("snap", p)
    except Exception as e:
        log("snap fail", e)


def body_text(page):
    try: return page.locator("body").inner_text(timeout=3000) or ""
    except Exception: return ""


# ---------------------------------------------------------------- mail.com
def fetch_launch_code(pw, since_ts: float) -> str | None:
    """登录 mail.com，收件箱找 GitHub 邮件，抓 8 位 launch code（也兼容 6 位）。"""
    b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width": 1400, "height": 900}, user_agent=UA)
    p = ctx.new_page(); p.set_default_timeout(30000)
    try:
        p.goto("https://www.mail.com/", timeout=60000)
        for sel in ["button:has-text('Accept')", "button:has-text('Agree')", "#onetrust-accept-btn-handler"]:
            try: p.locator(sel).first.click(timeout=2000); break
            except Exception: pass
        try: p.get_by_role("link", name=re.compile("^Log ?in$", re.I)).first.click(timeout=8000)
        except Exception: p.get_by_role("button", name=re.compile("^Log ?in$", re.I)).first.click(timeout=8000)
        p.wait_for_load_state("networkidle", timeout=30000)
        p.locator("input[name='username'], input#login-email, input[type='email']").first.fill(MAIL_EMAIL)
        p.locator("input[name='password'], input#login-password, input[type='password']").first.fill(MAIL_PW)
        # 表单 submit 按钮（跳过 header 里 y<50 的 toggle）
        btns = p.get_by_role("button", name=re.compile("Log ?in|Sign ?in", re.I))
        clicked = False
        for i in range(btns.count()):
            box = btns.nth(i).bounding_box()
            if box and box["y"] > 50: btns.nth(i).click(); clicked = True; break
        if not clicked: btns.first.click()
        p.wait_for_load_state("networkidle", timeout=45000)
        if "navigator" not in p.url:
            log("mail: login failed url=", p.url); snap(p, "mail_login_fail"); return None
        fr = None
        for _ in range(20):
            fr = next((f for f in p.frames if f.name == "mail"), None)
            if fr: break
            time.sleep(1)
        if not fr:
            log("mail: no mail frame"); snap(p, "mail_no_frame"); return None
        time.sleep(2)
        text = fr.evaluate("() => document.body.innerText")
        lines = [ln for ln in text.splitlines() if re.search(r"github", ln, re.I)]
        log("mail: github rows:", lines[:5])
        if not lines:
            snap(p, "mail_no_github"); return None
        # 点最新一条（列表默认按时间倒序，第一行即最新）
        try:
            fr.get_by_text(lines[0], exact=False).first.click(timeout=8000)
        except Exception:
            fr.locator(f"text={lines[0][:40]}").first.click(timeout=8000)
        time.sleep(3)
        blobs = []
        for f in p.frames:
            try: blobs.append(f.evaluate("() => document.body.innerText"))
            except Exception: pass
        blob = "\n".join(blobs)
        m = re.search(r"(?<!\d)(\d{8})(?!\d)", blob) or re.search(r"(?<!\d)(\d{6})(?!\d)", blob)
        if m:
            log("mail: code", m.group(1)); return m.group(1)
        log("mail: no code in opened message"); snap(p, "mail_msg_no_code"); return None
    except Exception as e:
        log("mail: exception", e); traceback.print_exc(); snap(p, "mail_error"); return None
    finally:
        b.close()


# ---------------------------------------------------------------- github
def fill_first(page, selectors, value, label):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=4000)
            loc.click(); loc.fill(value); log(f"filled {label} via {sel}"); return True
        except Exception:
            continue
    log(f"!! could not fill {label}"); return False


def click_first(page, selectors, label, timeout=4000):
    for sel in selectors:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=timeout)
            page.locator(sel).first.click(); log(f"clicked {label} via {sel}"); return True
        except Exception:
            continue
    return False


def run():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--lang=en-US"])
        ctx = b.new_context(viewport={"width": 1400, "height": 1000}, user_agent=UA, locale="en-US")
        p = ctx.new_page(); p.set_default_timeout(30000)

        log("goto signup")
        p.goto("https://github.com/signup", timeout=60000)
        p.wait_for_load_state("networkidle", timeout=30000)
        snap(p, "01_signup_landing"); log("url", p.url)

        # 新版单页表单；旧版是逐字段 "Continue" 的多步表单，两种都兼容
        ok_email = fill_first(p, ["input#email", "input[name='user[email]']", "input[type='email']"], GH_EMAIL, "email")
        if not ok_email: snap(p, "err_no_email_field"); sys.exit(1)
        click_first(p, ["button[data-continue-to='password-container']", "button:has-text('Continue'):visible"], "continue-after-email", 2000)
        fill_first(p, ["input#password", "input[name='user[password]']"], GH_PW, "password")
        click_first(p, ["button[data-continue-to='username-container']", "button:has-text('Continue'):visible"], "continue-after-password", 2000)
        fill_first(p, ["input#login", "input[name='user[login]']"], GH_USER, "username")
        click_first(p, ["button[data-continue-to='opt-in-container']", "button:has-text('Continue'):visible"], "continue-after-username", 2000)
        # country/region
        try:
            sel = p.locator("select#country, select[name='user[country_code]'], select").first
            if sel.count() and sel.is_visible():
                try: sel.select_option(label=COUNTRY)
                except Exception: sel.select_option(label=re.compile(COUNTRY, re.I))
                log("country set", COUNTRY)
        except Exception as e:
            log("country skip", e)
        time.sleep(1.5)
        snap(p, "02_form_filled")

        # 前端校验错误（用户名占用 / 邮箱已注册 / 密码弱）
        errs = body_text(p)
        bad = re.search(r"(Username|Email|Password)[^\n]*(is not available|already|taken|invalid|too short|is required)", errs, re.I)
        if bad:
            log("form rejected:", bad.group(0)); snap(p, "err_form_rejected"); sys.exit(22)

        if not click_first(p, ["button[type='submit']:has-text('Create account')", "button:has-text('Create account')",
                               "button:has-text('Continue'):visible", "form button[type='submit']"], "create-account", 8000):
            snap(p, "err_no_submit"); sys.exit(1)
        submit_ts = time.time()

        # CAPTCHA / 验证页 / launch code 页 —— 轮询判断落在哪
        stage = None
        for i in range(40):
            time.sleep(3)
            url = p.url; txt = body_text(p)
            if re.search(r"launch code|Enter the code|we sent (a|the) code|verify your email|account_verifications", url + " " + txt, re.I):
                stage = "code"; break
            if re.search(r"Verify your account|octocaptcha|Please solve|puzzle|Unable to verify|Visual puzzle", txt, re.I) or p.locator("iframe[src*='octocaptcha'], iframe[title*='captcha' i], iframe[src*='arkoselabs']").count():
                # 有些情况下 captcha 自动 verified 后需要再点一次 Continue/Create account
                if click_first(p, ["button:has-text('Create account'):visible", "button:has-text('Continue'):visible"], "post-captcha-continue", 1500):
                    continue
                stage = stage or "captcha"
                if i % 5 == 0: snap(p, f"03_captcha_{i}")
                continue
            if re.search(r"(is not available|already (been )?taken|already exists|invalid)", txt, re.I):
                log("rejected after submit:", txt[:400]); snap(p, "err_rejected_after_submit"); sys.exit(22)
            if i % 5 == 0: log("waiting… url", url); snap(p, f"03_wait_{i}")
        snap(p, f"04_stage_{stage}")
        if stage == "captcha":
            log("stuck on CAPTCHA; body:", body_text(p)[:600]); sys.exit(20)
        if stage != "code":
            log("unknown stage; url", p.url, "body:", body_text(p)[:600]); sys.exit(1)

        # 取 launch code
        code = None
        for i in range(10):
            time.sleep(12 if i else 20)
            code = fetch_launch_code(pw, submit_ts)
            if code: break
            log(f"code poll {i+1}/10 empty")
        if not code: log("no launch code"); sys.exit(21)

        # 8 个分位 input 或单 input
        digits = p.locator("input[name^='launch_code'], input[id^='launch-code'], input[autocomplete='one-time-code']")
        n = digits.count(); log("code inputs", n)
        if n >= len(code):
            for k, ch in enumerate(code): digits.nth(k).fill(ch)
        elif n >= 1:
            digits.first.fill(code)
        else:
            snap(p, "err_no_code_input"); sys.exit(1)
        time.sleep(1)
        click_first(p, ["button[type='submit']:visible", "button:has-text('Continue'):visible", "button:has-text('Verify'):visible"], "submit-code", 3000)
        p.wait_for_load_state("networkidle", timeout=60000)
        snap(p, "05_after_code"); log("url", p.url)

        # onboarding 问卷可跳过
        for _ in range(4):
            if click_first(p, ["a:has-text('Skip personalization')", "button:has-text('Skip personalization')",
                               "a:has-text('Skip')", "button:has-text('Skip')"], "skip-onboarding", 3000):
                p.wait_for_load_state("networkidle", timeout=30000); time.sleep(1)
            else: break
        snap(p, "06_final"); log("final url", p.url)

        # 判据：登录态 —— 访问 github.com/settings/profile 不被弹回 login
        p.goto("https://github.com/settings/profile", timeout=60000)
        p.wait_for_load_state("networkidle", timeout=30000)
        snap(p, "07_settings_profile")
        if "/login" in p.url:
            log("NOT logged in after signup"); sys.exit(1)
        txt = body_text(p)
        m = re.search(r"github\.com/([A-Za-z0-9-]+)", txt)
        print(json.dumps({"ok": True, "email": GH_EMAIL, "username": GH_USER, "final_url": p.url,
                          "profile_hint": m.group(1) if m else None}), flush=True)
        b.close()


if __name__ == "__main__":
    try: run()
    except SystemExit: raise
    except Exception:
        traceback.print_exc(); sys.exit(1)
