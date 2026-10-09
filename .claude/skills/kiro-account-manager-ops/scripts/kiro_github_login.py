#!/usr/bin/env python3
"""Kiro Account Manager —— GitHub OAuth 登录，添加账户。

用法：
  TARGET_EMAIL=x@binus.ac.id PW='<GitHub 密码>' TOTP_SEED='<seed>' \
    PYTHONPATH=<此目录> ~/.local/kiro-auto/venv/bin/python kiro_github_login.py

流程：
  OIDC client 注册 → app.kiro.dev/signin → 点 GitHub → GitHub 登录
  → OIDC authorize → callback → token exchange → getUsageLimits → 写 accounts.json

退出码：0 成功 / 2 已存在 / 3 失败 / 4 缺凭据

参考实现：kiro-register-en 的 kiro_login.py（/tmp/kiro_login.py）
"""
from __future__ import annotations

import os
import random
import secrets
import sys
import time
from pathlib import Path

import kiro_common as K

KIRO_SIGNIN = "https://app.kiro.dev/signin"
CALLBACK_PORT = 3128
CALLBACK_URI = f"http://127.0.0.1:{CALLBACK_PORT}"


def run(email: str, password: str, totp_seed: str | None) -> int:
    if K.account_exists(email):
        K.log(f"SKIP {email} 已在 accounts.json")
        return 2

    import pyotp
    from playwright.sync_api import sync_playwright

    machine_id = str(__import__("uuid").uuid4())
    verifier, challenge = K.gen_pkce()
    state = secrets.token_urlsafe(16)

    # STEP 1: OIDC client 注册（跟 BuilderId 一样的端点）
    K.log("STEP 1  注册 OIDC client")
    reg = K.register_client()

    # STEP 2: 本地 callback server
    K.log("STEP 2  起 callback server (端口 3128)")
    server = K.CallbackServer.__new__(K.CallbackServer)
    # GitHub 流程用固定端口 3128（kiro_login.py 的约定）
    from http.server import HTTPServer
    import threading
    from urllib.parse import parse_qs, urlparse

    class _H(K._CallbackHandler):
        pass

    httpd = HTTPServer(("127.0.0.1", CALLBACK_PORT), _H)
    httpd.oauth_result = None
    server.httpd = httpd
    server.port = CALLBACK_PORT
    server.redirect_uri = CALLBACK_URI
    server._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server._thread.start()

    try:
        with sync_playwright() as pw:
            # GitHub 用 Playwright Chromium 就行（不需要真 Chrome，反检测没 AWS 严）
            browser = pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-first-run", "--disable-features=Translate"],
            )
            ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
            page = ctx.new_page()

            # STEP 3: 打开 kiro.dev/signin
            K.log("STEP 3  打开 kiro.dev/signin")
            signin_url = (
                f"{KIRO_SIGNIN}?state={state}"
                f"&code_challenge={challenge}&code_challenge_method=S256"
                f"&redirect_uri={CALLBACK_URI}&redirect_from=KiroIDE"
            )
            page.goto(signin_url, timeout=60000)
            time.sleep(3)

            # STEP 4: 点 GitHub 按钮
            K.log("STEP 4  点 GitHub 登录")
            github_clicked = False
            for _ in range(10):
                for node in page.locator("button, a").all():
                    try:
                        if not node.is_visible():
                            continue
                        text = (node.inner_text() or "").strip().lower()
                        if "github" in text:
                            node.click(timeout=5000)
                            github_clicked = True
                            break
                    except Exception:
                        continue
                if github_clicked:
                    break
                time.sleep(1)

            if not github_clicked:
                raise RuntimeError("没找到 GitHub 登录按钮")
            time.sleep(3)

            # 等 signin callback（kiro.dev → 中间回调 → OIDC authorize）
            K.log("  等 signin callback...")
            for _ in range(20):
                if server.httpd.oauth_result or "github.com" in page.url:
                    break
                time.sleep(1)

            # 如果到了 GitHub 登录页，自动填写
            K.log("STEP 5  GitHub 登录")
            deadline = time.time() + 180
            while time.time() < deadline:
                if server.httpd.oauth_result:
                    break
                url = page.url

                # 检查回调
                if f"127.0.0.1:{CALLBACK_PORT}" in url or "localhost:3128" in url:
                    qs = parse_qs(urlparse(url).query)
                    code = (qs.get("code") or [None])[0]
                    if code and server.httpd.oauth_result is None:
                        server.httpd.oauth_result = {"code": code, "state": (qs.get("state") or [None])[0]}
                    break

                # GitHub 登录页
                if "github.com" in url:
                    # 填 email/username
                    login_field = page.locator('#login_field')
                    if login_field.count() > 0 and login_field.is_visible():
                        try:
                            if not login_field.input_value():
                                login_field.fill(email)
                                time.sleep(0.5)
                        except Exception:
                            pass

                    # 填密码
                    pw_field = page.locator('#password')
                    if pw_field.count() > 0 and pw_field.is_visible():
                        try:
                            if not pw_field.input_value():
                                pw_field.fill(password)
                                time.sleep(0.5)
                                # 点 Sign in
                                page.locator('input[type="submit"], button[type="submit"]').first.click(timeout=5000)
                                time.sleep(3)
                        except Exception:
                            pass

                    # TOTP
                    otp_field = page.locator('#app_totp')
                    if otp_field.count() > 0 and otp_field.is_visible() and totp_seed:
                        try:
                            if not otp_field.input_value():
                                otp = pyotp.TOTP(totp_seed).now()
                                otp_field.fill(otp)
                                time.sleep(1)
                                # GitHub 填完 TOTP 自动提交
                        except Exception:
                            pass

                    # 授权页面（Authorize）
                    if "/authorize" in url or "oauth" in url:
                        try:
                            for btn in page.locator("button").all():
                                text = (btn.inner_text() or "").lower()
                                if "authorize" in text or "allow" in text:
                                    btn.click(timeout=5000)
                                    break
                        except Exception:
                            pass

                # AWS consent 页
                if "awsapps.com" in url:
                    try:
                        page.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('button'));
                            for (const b of btns) {
                                const t = (b.innerText || '').toLowerCase();
                                if (t.includes('allow') || t.includes('authorize') || t.includes('confirm')) {
                                    b.click(); return;
                                }
                            }
                        }""")
                    except Exception:
                        pass

                time.sleep(2)

            browser.close()

        # STEP 6: token exchange
        result = server.httpd.oauth_result
        if not result or not result.get("code"):
            raise RuntimeError("没收到 authorization code")

        K.log("STEP 6  code → token")
        token = K.create_token(
            reg["clientId"], reg["clientSecret"],
            result["code"], verifier, CALLBACK_URI,
        )

        # STEP 7: usage（best-effort）
        K.log("STEP 7  getUsageLimits（best-effort）")
        usage = None
        try:
            usage = K.get_usage_limits(token["accessToken"], machine_id)
        except Exception:
            pass

        # STEP 8: 写 accounts.json
        entry = {
            "id": str(__import__("uuid").uuid4()),
            "email": email,
            "password": None,
            "label": "Kiro Github 账号",
            "status": "active",
            "addedAt": K.now_str(),
            "accessToken": token["accessToken"],
            "refreshToken": token["refreshToken"],
            "expiresAt": time.strftime(
                "%Y/%m/%d %H:%M:%S",
                time.localtime(time.time() + int(token.get("expiresIn") or 0)),
            ),
            "provider": "Github",
            "userId": None,
            "authMethod": "social",
            "clientId": None,
            "clientSecret": None,
            "region": None,
            "clientIdHash": None,
            "ssoSessionId": None,
            "idToken": token.get("idToken"),
            "startUrl": None,
            "profileArn": None,
            "usageData": usage,
            "groupId": None,
            "tagLinks": [],
            "machineId": machine_id,
            "availableModelsCache": None,
            "failureCount": 0,
            "lastFailureAt": None,
            "disabledReason": None,
            "successCount": 0,
            "enabled": True,
        }
        K.log(f"STEP 8  写 accounts.json —— {K.append_account(entry)}")
        K.log(f"SAVED {email}")
        return 0

    except Exception as exc:
        K.log(f"FAILED {email}: {exc}")
        return 3
    finally:
        server.close()


if __name__ == "__main__":
    target = os.environ.get("TARGET_EMAIL", "").strip()
    pw = os.environ.get("PW", "")
    seed = os.environ.get("TOTP_SEED", "").strip() or None

    if not target or not pw:
        print("需要 TARGET_EMAIL + PW（GitHub 密码）", file=sys.stderr)
        sys.exit(4)

    sys.exit(run(target, pw, seed))
