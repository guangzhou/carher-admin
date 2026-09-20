#!/usr/bin/env python3
"""github-signup-webkit — GitHub 注册串行半自动（Playwright WebKit + 人过验证码 + mail.com 自动取码）

为什么 WebKit：09-04 实测 188 出口(172.235.204.67) 上 Chromium/Firefox 打 /signup 被 DataDome 直接
"Access is temporarily restricted"；WebKit 只落到滑块 "Verification Required"，能进表单。

人只做两件事（脚本在这两处停下等，其余全自动）：
  1. DataDome 滑块（进 /signup 时）
  2. GitHub 自带的 Arkose 图形验证（提交表单后，有时不出）
通过 VNC 看画面操作：Mac 上 `ssh -N -L 5901:127.0.0.1:5901 cltx@10.68.13.188`，
然后 Finder ⌘K → vnc://127.0.0.1:5901（无密码）。

账号清单 ACCOUNTS_FILE（CSV，无表头）：acct,email,gh_username,gh_password,mail_password
每行串行处理，结果追加到 RESULT_FILE（jsonl）。凭据只从挂载文件读，不进日志。

188 上跑：
  docker run --rm --network host \
    -v /tmp/github-signup-webkit.py:/work/signup.py:ro \
    -v /tmp/gh_accounts.csv:/run/accounts.csv:ro \
    -v /tmp/gh_shots:/work/out \
    -e ACCOUNTS_FILE=/run/accounts.csv -e RESULT_FILE=/work/out/results.jsonl \
    -e OUT_DIR=/work/out -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    pw-python:1.49-vnc bash -c '
      Xvfb :99 -screen 0 1400x1000x24 >/dev/null 2>&1 &
      sleep 1; x11vnc -display :99 -rfbport 5901 -localhost -forever -shared -nopw -q >/dev/null 2>&1 &
      DISPLAY=:99 python3 /work/signup.py'
"""
from __future__ import annotations
import csv, json, os, re, sys, time, pathlib, traceback
from playwright.sync_api import sync_playwright, Page

ACCOUNTS = pathlib.Path(os.environ["ACCOUNTS_FILE"])
RESULT   = pathlib.Path(os.environ.get("RESULT_FILE", "/work/out/results.jsonl"))
OUT      = pathlib.Path(os.environ.get("OUT_DIR", "/work/out")); OUT.mkdir(parents=True, exist_ok=True)
COUNTRY  = os.environ.get("GH_COUNTRY", "Singapore")
HUMAN_WAIT = int(os.environ.get("HUMAN_WAIT_SEC", "900"))   # 等人过验证码的上限


def log(*a): print(time.strftime("%H:%M:%S"), "[signup]", *a, flush=True)


def snap(page: Page, name: str):
    p = OUT / f"{int(time.time())}_{name}.png"
    try: page.screenshot(path=str(p)); log("snap", p.name)
    except Exception as e: log("snap fail", e)


def text(page: Page) -> str:
    try: return page.locator("body").inner_text(timeout=3000) or ""
    except Exception: return ""


def wait_human(page: Page, until, what: str) -> bool:
    """停下等人：until(page)->bool 为真即继续。"""
    log(f"⏸  需要人工：{what}（VNC 里操作，最多等 {HUMAN_WAIT}s）")
    t0 = time.time(); n = 0
    while time.time() - t0 < HUMAN_WAIT:
        try:
            if until(page): log("▶  人工步骤已过"); return True
        except Exception: pass
        time.sleep(2); n += 1
        if n % 15 == 0: log(f"   …仍在等 {what}  url={page.url}")
    log("✗ 人工步骤超时"); return False


def on_form(page: Page) -> bool:
    return page.locator("input#email, input[name='user[email]']").count() > 0 and \
           page.locator("input#email, input[name='user[email]']").first.is_visible()


def blocked(page: Page) -> bool:
    return bool(re.search(r"Access is temporarily restricted", text(page)))


def fill(page: Page, sels, val, label) -> bool:
    for s in sels:
        loc = page.locator(s).first
        try:
            loc.wait_for(state="visible", timeout=4000); loc.click(); loc.fill(val)
            log(f"filled {label}"); return True
        except Exception: continue
    log(f"!! cannot fill {label}"); return False


def click(page: Page, sels, label, timeout=3000) -> bool:
    for s in sels:
        try:
            page.locator(s).first.wait_for(state="visible", timeout=timeout)
            page.locator(s).first.click(); log(f"clicked {label}"); return True
        except Exception: continue
    return False


# ---------------------------------------------------------------- mail.com launch code
def fetch_code(pw, email: str, mail_pw: str, since: float) -> str | None:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    p = b.new_context(viewport={"width": 1400, "height": 900}).new_page(); p.set_default_timeout(30000)
    try:
        p.goto("https://www.mail.com/", timeout=60000)
        for s in ["button:has-text('Accept')", "button:has-text('Agree')", "#onetrust-accept-btn-handler"]:
            try: p.locator(s).first.click(timeout=1500); break
            except Exception: pass
        try: p.get_by_role("link", name=re.compile("^Log ?in$", re.I)).first.click(timeout=8000)
        except Exception: p.get_by_role("button", name=re.compile("^Log ?in$", re.I)).first.click(timeout=8000)
        p.wait_for_load_state("networkidle", timeout=30000)
        p.locator("input[name='username'], input#login-email, input[type='email']").first.fill(email)
        p.locator("input[name='password'], input#login-password, input[type='password']").first.fill(mail_pw)
        btns = p.get_by_role("button", name=re.compile("Log ?in|Sign ?in", re.I)); done = False
        for i in range(btns.count()):
            box = btns.nth(i).bounding_box()
            if box and box["y"] > 50: btns.nth(i).click(); done = True; break
        if not done: btns.first.click()
        p.wait_for_load_state("networkidle", timeout=45000)
        if "navigator" not in p.url: log("mail: login failed", p.url); snap(p, "mail_login_fail"); return None
        fr = None
        for _ in range(20):
            fr = next((f for f in p.frames if f.name == "mail"), None)
            if fr: break
            time.sleep(1)
        if not fr: log("mail: no frame"); return None
        time.sleep(2)
        rows = [ln for ln in fr.evaluate("() => document.body.innerText").splitlines() if re.search(r"github", ln, re.I)]
        log("mail: github rows", len(rows), rows[:2])
        if not rows: return None
        try: fr.get_by_text(rows[0], exact=False).first.click(timeout=8000)
        except Exception: fr.locator(f"text={rows[0][:40]}").first.click(timeout=8000)
        time.sleep(3)
        blob = "\n".join(f.evaluate("() => document.body.innerText") for f in p.frames if True)
        m = re.search(r"(?<!\d)(\d{8})(?!\d)", blob) or re.search(r"(?<!\d)(\d{6})(?!\d)", blob)
        if m: log("mail: code found"); return m.group(1)
        log("mail: opened but no code"); snap(p, "mail_no_code"); return None
    except Exception as e:
        log("mail: exception", e); snap(p, "mail_err"); return None
    finally:
        b.close()


# ---------------------------------------------------------------- one account
def signup_one(pw, acct: str, email: str, user: str, gh_pw: str, mail_pw: str) -> dict:
    res = {"acct": acct, "email": email, "username": user, "ok": False, "stage": "start", "ts": time.strftime("%F %T")}
    b = pw.webkit.launch(headless=False)
    ctx = b.new_context(locale="en-US", viewport={"width": 1380, "height": 940})
    p = ctx.new_page(); p.set_default_timeout(30000)
    try:
        log(f"=== {acct} {email} -> {user}")
        p.goto("https://github.com/", wait_until="domcontentloaded", timeout=60000); time.sleep(2)
        try: p.get_by_role("link", name="Sign up").first.click(timeout=8000)
        except Exception: p.goto("https://github.com/signup", wait_until="domcontentloaded")
        time.sleep(4); snap(p, f"{acct}_01_landing")
        if blocked(p): res["stage"] = "datadome_block"; return res
        if not on_form(p):
            res["stage"] = "datadome_slider"
            if not wait_human(p, lambda pg: on_form(pg) or blocked(pg), "DataDome 滑块"): return res
            if blocked(p): res["stage"] = "datadome_block"; return res
        snap(p, f"{acct}_02_form")

        # 表单（新版单页；旧版逐字段 Continue 也兼容）
        if not fill(p, ["input#email", "input[name='user[email]']"], email, "email"): res["stage"] = "no_email_field"; return res
        click(p, ["button[data-continue-to='password-container']"], "continue", 1500)
        fill(p, ["input#password", "input[name='user[password]']"], gh_pw, "password")
        click(p, ["button[data-continue-to='username-container']"], "continue", 1500)
        fill(p, ["input#login", "input[name='user[login]']"], user, "username")
        click(p, ["button[data-continue-to='opt-in-container']"], "continue", 1500)
        try:
            sel = p.locator("select#country, select[name='user[country_code]']").first
            if sel.count():
                try: sel.select_option(label=COUNTRY)
                except Exception: sel.select_option(label=re.compile(COUNTRY, re.I))
                log("country", COUNTRY)
        except Exception as e: log("country skip", e)
        time.sleep(2); snap(p, f"{acct}_03_filled")
        bad = re.search(r"(Username|Email|Password)[^\n]{0,80}(not available|already|taken|invalid|too short|is required)", text(p), re.I)
        if bad: res["stage"] = "form_rejected"; res["detail"] = bad.group(0); return res

        if not click(p, ["button[type='submit']:has-text('Create account')", "button:has-text('Create account')",
                         "button:has-text('Continue'):visible", "form button[type='submit']"], "Create account", 8000):
            res["stage"] = "no_submit"; return res
        submit_ts = time.time(); time.sleep(4); snap(p, f"{acct}_04_submitted")

        # 提交后：launch code 页 / Arkose 验证 / 表单报错
        def code_page(pg):
            t = text(pg); u = pg.url
            return bool(re.search(r"launch code|Enter the code|we sent|account_verifications", u + " " + t, re.I)) or \
                   pg.locator("input[name^='launch_code'], input[autocomplete='one-time-code']").count() > 0
        if not code_page(p):
            rej = re.search(r"(not available|already (been )?taken|already exists)", text(p), re.I)
            if rej: res["stage"] = "rejected_after_submit"; res["detail"] = rej.group(0); return res
            res["stage"] = "arkose_captcha"
            if not wait_human(p, code_page, "GitHub 图形验证（做完后如还停在表单页请再点一次 Create account）"): return res
        snap(p, f"{acct}_05_code_page")

        code = None
        for i in range(10):
            time.sleep(15 if i else 20)
            code = fetch_code(pw, email, mail_pw, submit_ts)
            if code: break
            log(f"code poll {i+1}/10 empty")
        if not code: res["stage"] = "no_launch_code"; return res
        inputs = p.locator("input[name^='launch_code'], input[id^='launch-code'], input[autocomplete='one-time-code']")
        n = inputs.count(); log("code inputs", n)
        if n >= len(code):
            for k, ch in enumerate(code): inputs.nth(k).fill(ch)
        elif n: inputs.first.fill(code)
        else: res["stage"] = "no_code_input"; return res
        time.sleep(1)
        click(p, ["button[type='submit']:visible", "button:has-text('Continue'):visible", "button:has-text('Verify'):visible"], "submit code")
        p.wait_for_load_state("networkidle", timeout=60000); time.sleep(2); snap(p, f"{acct}_06_after_code")

        for _ in range(4):
            if click(p, ["a:has-text('Skip personalization')", "button:has-text('Skip personalization')", "a:has-text('Skip')", "button:has-text('Skip')"], "skip onboarding", 3000):
                p.wait_for_load_state("networkidle", timeout=30000); time.sleep(1)
            else: break

        p.goto("https://github.com/settings/profile", wait_until="domcontentloaded", timeout=60000); time.sleep(3)
        snap(p, f"{acct}_07_profile")
        if "/login" in p.url: res["stage"] = "not_logged_in_after_signup"; return res
        res.update(ok=True, stage="done", final_url=p.url); return res
    except Exception as e:
        res["stage"] = "exception"; res["detail"] = repr(e)[:300]; traceback.print_exc(); snap(p, f"{acct}_err"); return res
    finally:
        try: b.close()
        except Exception: pass


def main():
    rows = [r for r in csv.reader(ACCOUNTS.open()) if r and not r[0].startswith("#")]
    log(f"{len(rows)} account(s), serial")
    summary = []
    for r in rows:
        acct, email, user, gh_pw, mail_pw = [x.strip() for x in r[:5]]
        res = signup_one(pw_global, acct, email, user, gh_pw, mail_pw)
        with RESULT.open("a") as f: f.write(json.dumps(res, ensure_ascii=False) + "\n")
        log("RESULT", json.dumps(res, ensure_ascii=False)); summary.append(res)
        time.sleep(5)
    ok = sum(1 for s in summary if s["ok"])
    log(f"=== done: {ok}/{len(summary)} ok")
    sys.exit(0 if ok == len(summary) else 3)


if __name__ == "__main__":
    with sync_playwright() as pw_global:
        main()
