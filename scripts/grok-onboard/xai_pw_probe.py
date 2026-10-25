#!/usr/bin/env python3
"""xai_pw_probe.py — 全新浏览器用「邮箱 + 密码」登录 accounts.x.ai，判这把密码还活不活。

改密码的回验量具，两发一组（缺任何一发都不算验过）：
  MODE=new ⇒ 必须 `state=LOGGED_IN sso_cookie=True`（新密码真能登）
  MODE=old ⇒ 必须 `state=BADPW`（**阴性对照**：证明旧密码真的失效了，
             而不是「这站对新旧都放行」或「我其实没改成」）

登录步骤复用 `xai_device_capture.do_login()`（email → password → Turnstile → submit），
不碰 sso cookie、不碰 OAuth，所以对正在服务的腿零影响。

🔴 `xai_device_capture` 在 **import 期**就 `os.makedirs("/work/ss-xai")` ⇒
cltx 用户下直接 PermissionError。必须在调用前把 `SCREENSHOT_DIR` / `OAUTH_OUTPUT`
指到可写目录（`grok-pw-rotate.py` 已经在远端命令里带上了）。

env: PW_FILE json {"old_pw","new_pw"}, EMAIL, MODE=new|old, SHOTS, PROXY
⛔ 不打印密码；`do_login` 的 dump 会回显页面文本，这里整个关掉。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("SCREENSHOT_DIR", os.environ.get("SHOTS", "."))
os.environ.setdefault("OAUTH_OUTPUT", os.path.join(os.environ.get("SHOTS", "."), "unused.json"))

import xai_device_capture as cap  # noqa: E402
from patchright.sync_api import sync_playwright  # noqa: E402

PW = json.load(open(os.environ["PW_FILE"], encoding="utf-8"))
MODE = os.environ.get("MODE", "new")
SIGNIN = "https://accounts.x.ai/sign-in?redirect=grok-com"

cap.EMAIL = os.environ["EMAIL"]
cap.PW = PW["new_pw"] if MODE == "new" else PW["old_pw"]
cap.TAG = "probe-" + MODE
cap.SS = os.environ["SHOTS"]
cap.dump = lambda pg, tag: None  # dump 会把页面文本整段打出来，关掉


def main():
    with sync_playwright() as p:
        br = p.chromium.launch(headless=False,
                               proxy={"server": os.environ.get("PROXY", "socks5://127.0.0.1:40000")},
                               args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = br.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
            pg = ctx.new_page()
            pg.goto(SIGNIN, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            cap.dismiss_consent(pg)
            if not pg.query_selector("input[type=email]"):
                cap.click_any(pg, testids=["continue-with-email"],
                              texts=["Login with email", "Continue with email",
                                     "Sign in with email"])
                time.sleep(2)
            state = cap.do_login(pg)
            time.sleep(3)
            pg.screenshot(path=os.path.join(cap.SS, cap.TAG + ".png"))
            got_sso = any(c["name"] == "sso" for c in ctx.cookies())
            body = pg.inner_text("body").lower()
            badpw = any(k in body for k in ("incorrect", "invalid", "wrong", "does not match"))
            print("RESULT which=%s state=%s sso_cookie=%s badpw_text=%s url=%s"
                  % (MODE, state, got_sso, badpw, pg.url.split("?")[0]), flush=True)
            if MODE == "new":
                return 0 if state == "LOGGED_IN" and got_sso else 4
            return 0 if state == "BADPW" else 4
        finally:
            br.close()


if __name__ == "__main__":
    raise SystemExit(main())
