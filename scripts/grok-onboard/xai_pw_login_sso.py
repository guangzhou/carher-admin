#!/usr/bin/env python3
"""xai_pw_login_sso.py — 只有「邮箱 + x.ai 密码」时，在 225 WARP 上用浏览器登录拿 grok `sso` cookie。

产出的 `email----pw----sso` 行直接喂 `grok-warp-onboard.py`（后面的设备码 / 订阅 / 判活 /
建号 / 回归全复用那条已加固的路，本脚本不碰 OAuth、不碰 198）。

登录步骤复用 `xai_device_capture.do_login()`（email → password → Turnstile → submit，
以及它那套「只认正面证据」的 NEEDS_OTP 判据），这里只多做两件事：
  1. 登录成功后打开 grok.com 让 `sso` cookie 落到 `.x.ai` / `grok.com`
  2. 从 context cookie 里取 `sso`（JWT，`eyJ` 开头），写 0600 输出

用法（在 225 上，要 Xvfb + patchright；一般由 Mac 侧 ssh 调用）:
    DISPLAY=:80 PROXY=socks5://127.0.0.1:40000 \\
      PYTHONPATH=... PLAYWRIGHT_BROWSERS_PATH=... \\
      python3 xai_pw_login_sso.py --creds <0600 file> --out <0600 file> --shots <dir>

凭据行：`email----邮箱密码----x.ai密码`（取最后一段当 x.ai 密码）。
输出行：`email----x.ai密码----<sso>`；失败的号打 `STOP <email>: <reason>`，不写输出。
⛔ 全程不打印 sso / 密码。
"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xai_device_capture as cap  # noqa: E402
from patchright.sync_api import sync_playwright  # noqa: E402

SIGNIN = "https://accounts.x.ai/sign-in?redirect=grok-com"


def log(*a):
    print(*a, flush=True)


def parse(path):
    out = []
    for n, raw in enumerate(open(path, encoding="utf-8-sig").read().splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        f = [x.strip() for x in re.split(r"-{4,}", raw)]
        if len(f) < 2 or "@" not in f[0]:
            sys.exit("line %d: not `email----...----xai_pw`" % n)
        out.append((f[0], f[-1]))
    return out


def sso_of(ctx):
    for c in ctx.cookies():
        if c["name"] == "sso" and c["value"].startswith("eyJ"):
            return c["value"]
    return ""


def one(p, launch_kw, email, pw, shots):
    cap.EMAIL, cap.PW, cap.TAG, cap.SS = email, pw, email.split("@")[0], shots
    os.makedirs(shots, mode=0o700, exist_ok=True)
    br = p.chromium.launch(**launch_kw)
    try:
        ctx = br.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
        pg = ctx.new_page()
        pg.goto(SIGNIN, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        bt = cap.bodytext(pg).lower()
        if "abusive traffic" in bt or "blocked due to" in bt:
            return None, "EGRESS_BLOCKED"
        cap.dismiss_consent(pg)
        if not pg.query_selector("input[type=email]"):
            cap.click_any(pg, testids=["continue-with-email"],
                          texts=["Login with email", "Continue with email", "Sign in with email"])
            time.sleep(2)
        state = cap.do_login(pg)
        log("  [login-result] %s" % state)
        if state in ("TS_BLOCKED", "BADPW", "NEEDS_OTP"):
            return None, state
        # sso is set on .x.ai during the post-login cookie chain; grok.com confirms it
        for _ in range(3):
            s = sso_of(ctx)
            if s:
                return s, "OK"
            try:
                pg.goto("https://grok.com/", wait_until="domcontentloaded", timeout=60000)
                cap.settle(pg)
            except Exception as e:
                log("  [grok.com-err] %s" % str(e)[:100])
            time.sleep(4)
        cap.shot(pg, cap.TAG + "-no-sso")
        return None, "NO_SSO_COOKIE (url=%s)" % (pg.url or "")[:80]
    finally:
        br.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--shots", required=True)
    a = ap.parse_args()

    launch_kw = dict(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    proxy = os.environ.get("PROXY", "")
    if proxy:
        launch_kw["proxy"] = {"server": proxy}
    ok = 0
    creds = parse(a.creds)
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh, sync_playwright() as p:
        for email, pw in creds:
            log("=== %s ===" % email)
            try:
                sso, why = one(p, launch_kw, email, pw,
                               os.path.join(a.shots, email.split("@")[0]))
            except Exception as e:
                sso, why = None, "EXC %s" % str(e)[:150]
            if sso:
                fh.write("%s----%s----%s\n" % (email, pw, sso))
                fh.flush()
                ok += 1
                log("  OK %s sso_len=%d" % (email, len(sso)))
            else:
                log("STOP %s: %s" % (email, why))
    log("\n=== %d/%d got sso ===" % (ok, len(creds)))
    return 0 if ok == len(creds) else 1


if __name__ == "__main__":
    sys.exit(main())
