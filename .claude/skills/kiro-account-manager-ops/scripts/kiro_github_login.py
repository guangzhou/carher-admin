#!/usr/bin/env python3
"""Kiro Account Manager —— GitHub OAuth 登录。

🔴 流程 = Kiro Desktop Auth social（实测这条路真的产生 code）：
  /login?idp=Github&redirect_uri=kiro://kiro.kiroAgent/authenticate-success
  → GitHub 登录（email→password→TOTP）→ 授权 → 重定向 kiro://...?code=xxx
  → POST /oauth/token 换 token → 写 accounts.json

实测证据（2026-10-09 muthi 调试）：GitHub 授权后浏览器发出
`kiro://kiro.kiroAgent/authenticate-success?code=7ee1634a-...`，
在 Playwright 里**同时**触发 request 和 requestfailed(ERR_ABORTED) 两个事件。
用 page.on("request") 主拦 + requestfailed 兜底，两个都挂。
❌ 别用 page.route（会 abort 掉请求干扰事件）。

🔴 **2026-10-19 起必须先过 AWS WAF**（否则到不了 github.com，空转 180s 超时）：
Kiro 在 `/github/authorize` 前加了 WAF 人机验证（九宫格选图 "Choose all the X"）。
先跑 `waf_solve.py` 过一次题拿 `aws-waf-token`（约 4 天有效、非 httpOnly），
再用 `WAF_COOKIES=<那个 json>` 把它注入进来，后续所有号复用同一个 token。

用法：
  WAF_COOKIES=/path/waf-cookies.json \
  TARGET_EMAIL=x@binus.ac.id PW='<GitHub 密码>' TOTP_SEED='<seed>' \
    PYTHONPATH=<此目录> ~/.local/kiro-auto/venv/bin/python kiro_github_login.py

退出码：0 成功 / 2 已存在 / 3 失败 / 4 缺凭据
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import kiro_common as K

KIRO_AUTH = "https://prod.us-east-1.auth.desktop.kiro.dev"
REDIRECT_URI = "kiro://kiro.kiroAgent/authenticate-success"
APP_VERSION = "0.6.18"


def _grab_code(url: str, box: dict) -> None:
    """从 kiro:// URL 里取 code，塞进 box。"""
    if url.startswith("kiro://") and not box["value"]:
        qs = parse_qs(urlparse(url).query)
        code = (qs.get("code") or [None])[0]
        if code:
            box["value"] = code


def run(email: str, password: str, totp_seed: str | None) -> int:
    if K.account_exists(email):
        K.log(f"SKIP {email} 已在 accounts.json")
        return 2

    import pyotp
    from playwright.sync_api import sync_playwright

    machine_id = str(uuid.uuid4())
    verifier, challenge = K.gen_pkce()
    state = secrets.token_urlsafe(16)
    auth_code = {"value": None}

    login_url = (
        f"{KIRO_AUTH}/login?idp=Github"
        f"&redirect_uri={REDIRECT_URI}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
        f"&state={state}"
    )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800}, locale="en-US"
            )

            # 🔴 2026-10-19 起 Kiro 在 social 登录链上加了 **AWS WAF 人机验证**：
            #   /login?idp=Github → 302 Cognito/oauth2/authorize → 302
            #   /github/authorize ⇒ 这里被 WAF 截下（HTTP 405 + "Let's confirm you are human"
            #   九宫格选图题），**永远到不了 github.com** ⇒ 下面那段填表逻辑（只在 url 含
            #   github.com 时才跑）一次都不触发，空转满 180s 报「没收到 authorization code」。
            # 解法：先用 `waf_solve.py` 过一次题拿 `aws-waf-token`（有效期约 4 天、非
            #   httpOnly），存盘后在这里注入，后续所有号直接复用，不用每号做一道题。
            waf_file = os.environ.get("WAF_COOKIES", "").strip()
            if waf_file and os.path.exists(waf_file):
                try:
                    with open(waf_file) as f:
                        cookies = json.load(f)
                    keep = [c for c in cookies
                            if "waf" in c["name"].lower() or "kiro.dev" in c.get("domain", "")]
                    ctx.add_cookies(keep)
                    K.log(f"  已注入 {len(keep)} 条 WAF/kiro cookie（{waf_file}）")
                except Exception as exc:
                    K.log(f"  !! 注入 WAF cookie 失败: {str(exc)[:120]}")
            else:
                K.log("  ⚠️ 未提供 WAF_COOKIES ⇒ 会撞 AWS WAF 选图挑战并超时，先跑 waf_solve.py")

            page = ctx.new_page()

            # 🔴 三处都挂上拦截，kiro:// code 在哪个事件冒出来都能抓到
            page.on("request", lambda r: _grab_code(r.url, auth_code))
            page.on("requestfailed", lambda r: _grab_code(r.url, auth_code))
            page.on("framenavigated", lambda f: _grab_code(f.url, auth_code))

            K.log("STEP 1  打开 Kiro Auth → GitHub")
            try:
                page.goto(login_url, timeout=60000)
            except Exception:
                pass  # kiro:// 跳转可能让 goto 抛错，无妨
            time.sleep(3)

            K.log("STEP 2  GitHub 登录")
            deadline = time.time() + 180
            last_url = ""
            while time.time() < deadline:
                if auth_code["value"]:
                    break
                try:
                    url = page.url
                except Exception:
                    time.sleep(1); continue

                if url != last_url:
                    K.log(f"  URL -> {url[:70]}")
                    last_url = url

                _grab_code(url, auth_code)
                if auth_code["value"]:
                    break

                try:
                    if "github.com" in url:
                        lf = page.locator('#login_field')
                        if lf.count() > 0 and lf.is_visible() and not lf.input_value():
                            lf.fill(email)
                        pf = page.locator('#password')
                        if pf.count() > 0 and pf.is_visible() and not pf.input_value():
                            pf.fill(password)
                            time.sleep(0.3)
                            page.locator('input[type="submit"]').first.click(timeout=5000)
                            time.sleep(3); continue
                        of = page.locator('#app_totp')
                        if of.count() > 0 and of.is_visible() and not of.input_value() and totp_seed:
                            of.fill(pyotp.TOTP(totp_seed).now())
                            time.sleep(3); continue
                        # authorize 页：已授权的号自动跳 kiro://，没授权的点按钮
                        if "/authorize" in url or "/oauth" in url:
                            for sel in ['button[type="submit"]', 'input[type="submit"]', 'button']:
                                for btn in page.locator(sel).all():
                                    try:
                                        t = (btn.inner_text() or btn.get_attribute("value") or "").lower()
                                        if ("authorize" in t or "allow" in t or "continue" in t) and btn.is_visible():
                                            btn.click(timeout=5000)
                                            time.sleep(2)
                                            break
                                    except Exception:
                                        pass
                except Exception:
                    pass

                time.sleep(2)

            browser.close()

        if not auth_code["value"]:
            raise RuntimeError("没收到 authorization code")
        K.log(f"  ✅ code 已取到")

        # token exchange（走 Kiro Desktop Auth /oauth/token，不是 AWS OIDC）
        K.log("STEP 3  code → token")
        ua = f"KiroIDE-{APP_VERSION}-{machine_id}"
        resp = httpx.post(
            f"{KIRO_AUTH}/oauth/token",
            json={"code": auth_code["value"], "code_verifier": verifier, "redirect_uri": REDIRECT_URI},
            headers={"Content-Type": "application/json", "Accept": "application/json", "user-agent": ua},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"token exchange {resp.status_code}: {resp.text[:200]}")
        token = resp.json()

        # usage（best-effort）
        usage = None
        try:
            arn = None
            try:
                arn = K.list_available_profiles(token["accessToken"])
            except Exception:
                pass
            usage = K.get_usage_limits(token["accessToken"], machine_id, profile_arn=arn)
        except Exception:
            pass

        entry = {
            "id": str(uuid.uuid4()), "email": email, "password": None,
            "label": "Kiro Github 账号", "status": "active", "addedAt": K.now_str(),
            "accessToken": token["accessToken"], "refreshToken": token["refreshToken"],
            "expiresAt": time.strftime("%Y/%m/%d %H:%M:%S",
                                       time.localtime(time.time() + int(token.get("expiresIn") or 3600))),
            "provider": "Github", "userId": None, "authMethod": "social",
            "clientId": None, "clientSecret": None, "region": None,
            "clientIdHash": None, "ssoSessionId": None, "idToken": token.get("idToken"),
            "startUrl": None, "profileArn": None, "usageData": usage,
            "groupId": None, "tagLinks": [], "machineId": machine_id,
            "availableModelsCache": None, "failureCount": 0, "lastFailureAt": None,
            "disabledReason": None, "successCount": 0, "enabled": True,
        }
        K.log(f"STEP 4  写 accounts.json —— {K.append_account(entry)}")
        K.log(f"SAVED {email}")
        return 0

    except Exception as exc:
        K.log(f"FAILED {email}: {exc}")
        return 3


if __name__ == "__main__":
    target = os.environ.get("TARGET_EMAIL", "").strip()
    pw = os.environ.get("PW", "")
    seed = os.environ.get("TOTP_SEED", "").strip() or None
    if not target or not pw:
        print("需要 TARGET_EMAIL + PW", file=sys.stderr)
        sys.exit(4)
    sys.exit(run(target, pw, seed))
