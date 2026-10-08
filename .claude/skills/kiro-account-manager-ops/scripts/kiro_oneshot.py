#!/usr/bin/env python3
"""BuilderId/IdC 单账户登录 —— 真 Chrome CDP 状态机。

用法：
  TARGET_EMAIL=x@binus.ac.id PW='...' TOTP_SEED='...' \
    ~/.local/kiro-auto/venv/bin/python kiro_oneshot.py

退出码：0 已保存 / 2 已存在跳过 / 3 登录失败 / 4 缺凭据

为什么必须真 Chrome（不是 Playwright Chromium）：AWS 的反自动化检测比 GitHub 严，
Chromium 指纹直接进验证码死循环。见 feedback_builderid_login_automation_pitfalls。
"""
from __future__ import annotations

import os
import random
import re as _re_mod
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import kiro_common as K

CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HEADFUL_WAIT = 300


def human_type(locator, text: str) -> None:
    """逐字符输入。一次性 fill() 会被 AWS threat-mitigation 判为机器人。"""
    locator.click()
    for ch in text:
        locator.type(ch, delay=0)
        time.sleep(random.uniform(0.08, 0.25))


def launch_chrome(profile_dir: Path):
    proc = subprocess.Popen(
        [
            CHROME,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if not K.port_free(CDP_PORT):
            return proc
        time.sleep(0.5)
    raise RuntimeError(f"Chrome CDP {CDP_PORT} 没起来")


def solve_captcha(page, frame) -> str | None:
    """ddddocr 识别验证码图片。返回识别出的字符串，失败返回 None。"""
    import ddddocr

    img = frame.locator('img[alt="captcha"]').first
    try:
        png = img.screenshot(timeout=10000)
    except Exception:
        return None
    ocr = ddddocr.DdddOcr(show_ad=False)
    return ocr.classification(png)


def body_snippet(page, limit: int = 4000) -> str:
    try:
        return (page.locator("body").inner_text() or "")[:limit]
    except Exception:
        return ""


def captcha_rejected(page) -> bool:
    """页面是否在抱怨验证码错（而不是密码错）。"""
    try:
        body = (page.locator("body").inner_text() or "")[:4000]
    except Exception:
        return False
    return any(k in body for k in ("验证码无效", "Invalid captcha", "incorrect characters"))


def click_to_verify(page) -> bool:
    """点击式 WAF 挑战（「Please click verify to start your security challenge」）。

    2026-10-08 新出现：这类挑战没有图片可 OCR，必须真点一下 Verify 按钮，
    点完 AWS 自己跑完挑战。不处理会一直停在密码页，表现成「密码一直错」。
    """
    for frame in page.frames:
        try:
            for node in frame.locator('button, [role="button"], input[type="button"]').all():
                if not node.is_visible():
                    continue
                label = (node.inner_text() or node.get_attribute("value") or "").strip()
                if label.lower() in ("verify", "验证"):
                    node.click(timeout=5000)
                    return True
        except Exception:
            continue
    return False


def captcha_visible(page):
    """坑 #2：过了验证码 iframe 仍留在 DOM，只是 img 变 hidden。
    判「还有验证码」必须看 img.is_visible()，不能看 iframe 存在性（那是恒真）。"""
    for frame in page.frames:
        try:
            img = frame.locator('img[alt="captcha"]')
            if img.count() > 0 and img.first.is_visible():
                return frame
        except Exception:
            continue
    return None


def drive_login(
    page,
    email: str,
    passwords: list[str],
    totp_seed: str | None,
    stop,
    deadline: float,
    log=K.log,
) -> bool:
    """驱动 AWS 登录页直到 `stop(page)` 为真或超时。返回是否达成。

    抽出来是为了让「登录拿 token」和「登录改 MFA」**共用同一份** WAF/验证码/MFA/
    consent 逻辑。这些分支是 2026-10-08 一个个踩出来的（见 SKILL.md 陷阱 11~15），
    复制一份必然漂移：改了一边忘另一边，下次又从头踩。
    `stop` 由调用方给：取 token 看 callback 到没到，改 MFA 看有没有进 profile 页。
    """
    pw_idx = 0
    pw_fail_streak = 0
    last_shape = ""

    while time.time() < deadline:
        if stop(page):
            return True
        time.sleep(1.2)

        # 点击式挑战优先：它和图片验证码可能同时在页上，
        # 但没点过 Verify 时图片那套提交了也不算过。
        if "security challenge" in body_snippet(page) and click_to_verify(page):
            log("  点击式 WAF 挑战 -> 已点 Verify")
            time.sleep(5)
            continue

        frame = captcha_visible(page)
        if frame is not None:
            code = solve_captcha(page, frame)
            if code:
                log(f"  captcha -> {len(code)} chars")
                box = frame.locator('input[type="text"]').first
                human_type(box, code)
                try:
                    frame.locator('button[type="submit"]').first.click(timeout=5000)
                except Exception:
                    page.keyboard.press("Enter")
                time.sleep(3)
                continue

        shape = page_shape(page)
        if shape != last_shape:
            log(f"  page -> {shape}")
            last_shape = shape

        if shape == "email":
            fill_first(page, ['input[name="email"]', 'input[type="email"]', "#username"], email)
            click_submit(page)
        elif shape == "password":
            fill_first(
                page,
                ['input[name="password"]', 'input[type="password"]', "#password"],
                passwords[min(pw_idx, len(passwords) - 1)],
            )
            click_submit(page)
            time.sleep(3)
            if page_shape(page) == "password":
                # 「还停在密码页」≠「密码错」：验证码答错也会停在这（陷阱 14）
                if captcha_rejected(page):
                    log("  密码页未过：验证码无效（不记密码失败）")
                else:
                    pw_fail_streak += 1
                    if pw_fail_streak >= 2 and pw_idx + 1 < len(passwords):
                        pw_idx += 1
                        pw_fail_streak = 0
                        log(f"  切备用密码 #{pw_idx + 1}")
            else:
                pw_fail_streak = 0
        elif shape == "mfa":
            if not totp_seed:
                log("  !! 需要 MFA 但没有 TOTP seed，人工接管")
                time.sleep(20)
                continue
            import pyotp

            fill_first(
                page,
                ['input[name="mfaCode"]', 'input[autocomplete="one-time-code"]',
                 'input[name="code"]', 'input[type="text"]'],
                pyotp.TOTP(totp_seed).now(),
            )
            click_submit(page)
            time.sleep(4)
        elif shape == "consent":
            # 陷阱 4：按钮文案是「允许访问」，必须 includes 匹配
            clicked = False
            for sel in ["button", 'input[type="submit"]', "a[role=button]"]:
                for node in page.locator(sel).all():
                    try:
                        label = (node.inner_text() or "").strip()
                    except Exception:
                        continue
                    if any(k in label for k in ("允许", "Allow", "Confirm", "同意")):
                        try:
                            node.click(timeout=5000)
                            clicked = True
                            break
                        except Exception:
                            continue
                if clicked:
                    break
            time.sleep(3)

    return stop(page)


def run(email: str, passwords: list[str], totp_seed: str | None) -> int:
    if K.account_exists(email):
        K.log(f"SKIP {email} 已在 accounts.json")
        return 2

    from playwright.sync_api import sync_playwright

    machine_id = str(uuid.uuid4())
    verifier, challenge = K.gen_pkce()
    state = secrets.token_urlsafe(16)

    K.log("STEP 1  注册 OIDC client")
    reg = K.register_client()

    K.log("STEP 2  起本地 callback server")
    server = K.CallbackServer()
    authorize_url = K.build_authorize_url(reg["clientId"], server.redirect_uri, state, challenge)

    profile_dir = Path(f"/tmp/kiro-chrome-{os.getpid()}-{int(time.time())}")
    chrome = launch_chrome(profile_dir)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            K.log("STEP 3  打开 AWS BuilderId authorize")
            page.goto(authorize_url, wait_until="domcontentloaded", timeout=60000)

            deadline = time.time() + HEADFUL_WAIT
            drive_login(
                page, email, passwords, totp_seed,
                stop=lambda _p: bool(server.httpd.oauth_result),  # type: ignore[attr-defined]
                deadline=deadline,
            )

            K.log("STEP 4  等 callback")
            result = server.wait(timeout=max(30, int(deadline - time.time()) + 60), expect_state=state)

            K.log("STEP 5  code -> token")
            token = K.create_token(
                reg["clientId"], reg["clientSecret"], result["code"], verifier, server.redirect_uri
            )

            # STEP 6 —— 配额，best-effort。
            # 2026-10-08 实测：getUsageLimits 现在**强制**要 profileArn（不带必 400
            # `Invalid profileArn.`），而 BuilderId/IdC 既拿不到自己的 ARN
            # （ListAvailableProfiles 回 400 AccessDenied），借 social 号的 ARN 又 403
            # `Invalid token`。所以 IdC 新号这步**预期取不到**，绝不能阻断保存：
            # token 本身是好的，池子用的也是 token，不是 usageData。
            K.log("STEP 6  getUsageLimits（IdC 预期拿不到，仅 best-effort）")
            usage = None
            try:
                arn = None
                try:
                    arn = K.list_available_profiles(token["accessToken"])
                except Exception:
                    pass
                usage = K.get_usage_limits(token["accessToken"], machine_id, profile_arn=arn)
                tier = ((usage.get("subscriptionInfo") or {}).get("subscriptionTitle")) or "?"
                K.log(f"  tier={tier}")
            except Exception as exc:
                K.log(f"  usage 未取到（不阻断保存）: {str(exc)[:110]}")

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


def page_shape(page) -> str:
    """按可见输入框判当前处于流程哪一步。纯形状判断，不依赖文案。"""
    try:
        url = page.url
        if "/oauth/callback" in url:
            return "callback"
        for sel, name in (
            ('input[autocomplete="one-time-code"]', "mfa"),
            ('input[name="mfaCode"]', "mfa"),
            ('input[type="password"]', "password"),
            ('input[type="email"]', "email"),
            ('input[name="email"]', "email"),
        ):
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return name
        body = (page.locator("body").inner_text() or "")[:4000]
        if any(k in body for k in ("允许访问", "Allow access", "授权请求")):
            return "consent"
        # 实测 (2026-10-08 kinara)：AWS MFA 页文案是「需要额外验证 / MFA 代码 / 身份验证器」，
        # 不含「验证码」；输入框是裸 input[type=text]，既无 name 也无 one-time-code。
        # 只按属性判会落 unknown 卡死，必须补文案分支。
        if _re_mod.search(
            r"验证码|verification code|MFA 代码|MFA code|身份验证器|authenticator|额外验证", body
        ):
            return "mfa"
    except Exception:
        pass
    return "unknown"


def fill_first(page, selectors: list[str], value: str) -> bool:
    """逐个候选节点找第一个**可见**的，不能只看 .first —— 见 click_submit 的注释。"""
    for sel in selectors:
        for node in page.locator(sel).all():
            try:
                if not node.is_visible():
                    continue
                node.fill("")
                human_type(node, value)
                return True
            except Exception:
                continue
    return False


def click_submit(page) -> None:
    """🔴 必须遍历所有匹配节点，禁 `.first`。

    实测 (2026-10-08)：AWS 页面 DOM 里第一个 `button[type=submit]` 是**隐藏的**
    Cookie 同意按钮 (`data-id=awsccc-cb-btn-accept`)，真正的「继续」排在它后面。
    `.first` + is_visible() 会永远停在那个隐藏按钮上，整个流程静默卡死。
    """
    for sel in ['button[type="submit"]', 'input[type="submit"]', "button"]:
        for node in page.locator(sel).all():
            try:
                if not node.is_visible():
                    continue
                label = (node.inner_text() or "").strip()
                if any(k in label for k in ("Cookie", "cookie")):
                    continue
                node.click(timeout=5000)
                return
            except Exception:
                continue
    page.keyboard.press("Enter")


if __name__ == "__main__":
    target = os.environ.get("TARGET_EMAIL", "").strip()
    primary = os.environ.get("PW", "")
    alt = os.environ.get("ALT_PW", "")
    seed = os.environ.get("TOTP_SEED", "").strip() or None

    if not target or not primary:
        print("需要 TARGET_EMAIL + PW 环境变量", file=sys.stderr)
        sys.exit(4)

    pw_list: list[str] = []
    for candidate in (primary, alt):
        for variant in K.pw_variants(candidate) if candidate else []:
            if variant not in pw_list:
                pw_list.append(variant)

    sys.exit(run(target, pw_list, seed))
