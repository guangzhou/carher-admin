#!/usr/bin/env python3
"""BuilderId 登录——MFA 走邮箱验证码(Graph API 取码,无需 TOTP seed)。

用法：
  TARGET_EMAIL=x@outlook.com PW='<AWS密码>' GRAPH_RT='<refresh-token>' GRAPH_CID='<client-id>' \
    PYTHONPATH=<此目录> ~/.local/kiro-auto/venv/bin/python kiro_builderid_email_mfa.py

退出码：0 成功 / 2 已存在 / 3 失败 / 4 缺凭据
"""
from __future__ import annotations

import os
import secrets
import sys
import time
import uuid
from pathlib import Path

import kiro_common as K
import kiro_oneshot as O


def run(email: str, passwords: list[str], graph_rt: str, graph_cid: str) -> int:
    if K.account_exists(email):
        K.log(f"SKIP {email} 已在 accounts.json")
        return 2

    from playwright.sync_api import sync_playwright

    machine_id = str(uuid.uuid4())
    verifier, challenge = K.gen_pkce()
    state = secrets.token_urlsafe(16)

    K.log("STEP 1  注册 OIDC client")
    reg = K.register_client()

    K.log("STEP 2  起 callback server")
    server = K.CallbackServer()
    authorize_url = K.build_authorize_url(reg["clientId"], server.redirect_uri, state, challenge)

    profile_dir = Path(f"/tmp/kiro-chrome-emailmfa-{os.getpid()}-{int(time.time())}")
    chrome = O.launch_chrome(profile_dir)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{O.CDP_PORT}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            K.log("STEP 3  打开 AWS BuilderId authorize")
            page.goto(authorize_url, wait_until="domcontentloaded", timeout=60000)

            # 记住 MFA 开始前的时间戳——只取之后的邮件，排除旧码
            mfa_start_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            used_codes: set[str] = set()

            deadline = time.time() + O.HEADFUL_WAIT
            O.drive_login(
                page, email, passwords, None,  # 无 TOTP seed
                stop=lambda _p: bool(server.httpd.oauth_result),
                deadline=deadline,
                otp_callback=lambda: K.graph_fetch_aws_otp(
                    graph_rt, graph_cid, timeout=60,
                    after_ts=mfa_start_ts, used_codes=used_codes,
                ),
            )

            K.log("STEP 4  等 callback")
            result = server.wait(timeout=max(30, int(deadline - time.time()) + 60), expect_state=state)

            K.log("STEP 5  code -> token")
            token = K.create_token(
                reg["clientId"], reg["clientSecret"], result["code"], verifier, server.redirect_uri
            )

            K.log("STEP 6  getUsageLimits（best-effort）")
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

            entry = K.build_idc_entry(email, token, reg, usage, machine_id)
            K.log(f"STEP 7  写 accounts.json —— {K.append_account(entry)}")
            K.log(f"SAVED {email}")
            return 0

    except Exception as exc:
        K.log(f"FAILED {email}: {exc}")
        return 3
    finally:
        server.close()
        try:
            chrome.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    target = os.environ.get("TARGET_EMAIL", "").strip()
    primary = os.environ.get("PW", "")
    graph_rt = os.environ.get("GRAPH_RT", "").strip()
    graph_cid = os.environ.get("GRAPH_CID", "").strip()

    if not target or not primary or not graph_rt or not graph_cid:
        print("需要 TARGET_EMAIL + PW + GRAPH_RT + GRAPH_CID", file=sys.stderr)
        sys.exit(4)

    sys.exit(run(target, [primary] + K.pw_variants(primary), graph_rt, graph_cid))
