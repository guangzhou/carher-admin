"""
BuilderId 改密码 + 改 2FA — 复用 kiro_oneshot.drive_login() 状态机。

流程：
  1. 真 Chrome CDP → profile.aws.amazon.com（触发 Builder ID 登录）
  2. drive_login() 自动处理 email/pwd/captcha/WAF/MFA/consent
  3. 登录后 profile 页点「安全」→ credentials.signin.aws/#/security
  4. 改密码：点「更改密码」→ 填两次新密码 → 提交
  5. 改 2FA：选中设备 radio → 删除 → 注册新设备 → 提取 secret → 验证

用法：
  cd /tmp  # 避免 operator shadow
  EMAIL=... OLD_PWD=... MFA_SECRET=... python3 -I change_builderid_pwd_2fa.py

依赖 kiro_oneshot.py / kiro_common.py（同目录），与加号脚本共享登录逻辑，
AWS 改页面只需修一处。
"""
import json
import os
import random
import re
import string
import subprocess
import sys
import time
from pathlib import Path

import pyotp

# -I 模式下 PYTHONPATH 被忽略，手动加同目录
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

import kiro_common as K
import kiro_oneshot as O

# ============ 配置 ============

EMAIL = os.environ.get("EMAIL", "").strip()
OLD_PWD = os.environ.get("OLD_PWD", "").strip()
MFA_SECRET = os.environ.get("MFA_SECRET", "").strip()

DO_PWD = os.environ.get("DO_PWD", "1") == "1"
DO_2FA = os.environ.get("DO_2FA", "1") == "1"


# ============ 工具 ============

def gen_pwd(length=16):
    u = random.choice(string.ascii_uppercase)
    lo = random.choice(string.ascii_lowercase)
    d = random.choice(string.digits)
    s = random.choice("@#$%&")
    rest = "".join(random.choices(string.ascii_letters + string.digits + "@#$%&", k=length - 4))
    pwd = list(u + lo + d + s + rest)
    random.shuffle(pwd)
    return "".join(pwd)


def wait_for_body(page, min_len=30, retries=5, sleep_s=10, no_reload=False):
    """等 SPA 渲染出内容。no_reload=True 时只等不 reload（有状态 URL 不能刷新）。"""
    for i in range(retries):
        body = (page.locator("body").inner_text() or "").strip()
        if len(body) >= min_len:
            return body
        K.log(f"    SPA body empty (retry {i+1}/{retries}), waiting...")
        time.sleep(sleep_s)
        body = (page.locator("body").inner_text() or "").strip()
        if len(body) >= min_len:
            return body
        if no_reload:
            continue
        K.log(f"    still empty, reloading...")
        page.reload(wait_until="domcontentloaded", timeout=30000)
        time.sleep(sleep_s)
    return (page.locator("body").inner_text() or "").strip()


def _fetch_outlook_aws_code(binus_email, binus_pw):
    """子进程取 AWS 邮箱验证码（避免 Playwright 嵌套 asyncio 冲突）。"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "outlook_aws_code.py")
    if not os.path.exists(script):
        script = "/tmp/outlook_aws_code.py"
    if not os.path.exists(script):
        K.log("    outlook_aws_code.py not found")
        return None
    env = os.environ.copy()
    env["BINUS_EMAIL"] = binus_email
    env["BINUS_PW"] = binus_pw
    try:
        r = subprocess.run(
            [sys.executable, "-I", script],
            cwd="/tmp", env=env, capture_output=True, text=True, timeout=120)
        for line in r.stdout.splitlines():
            if line.startswith("CODE="):
                code = line.split("=", 1)[1].strip()
                K.log(f"    Outlook 取到验证码: {code}")
                return code
        K.log(f"    Outlook 子进程无码: {r.stdout[-200:]}")
        return None
    except Exception as e:
        K.log(f"    Outlook 子进程异常: {e}")
        return None


def wait_and_click(page, selectors, timeout=30, interval=3):
    """等按钮出现并点击，返回匹配的 selector 或 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            for node in page.locator(sel).all():
                try:
                    if node.is_visible():
                        node.click(timeout=5000)
                        return sel
                except Exception:
                    continue
        time.sleep(interval)
    return None


def js_click_exact_button(page, exact_texts, exclude_texts=None, timeout=15):
    """用 JS 精确匹配按钮文本并点击——绕过 Playwright :text-is() 的 CJK 问题。"""
    exclude_texts = exclude_texts or []
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.evaluate("""(args) => {
            const [targets, excludes] = args;
            const btns = [...document.querySelectorAll('button')];
            for (const btn of btns) {
                const t = btn.textContent.trim();
                if (!targets.includes(t)) continue;
                if (excludes.some(ex => t.includes(ex))) continue;
                if (btn.offsetParent === null) continue;  // hidden
                btn.click();
                return t;
            }
            return null;
        }""", [exact_texts, exclude_texts])
        if result:
            K.log(f"    JS clicked button: '{result}'")
            return result
        time.sleep(2)
    return None


def handle_reauth(page, password, mfa_secret):
    """处理 SPA 页面的重认证（密码 + MFA + CAPTCHA）。"""
    body = (page.locator("body").inner_text() or "")[:2000]
    if "sign in" not in body.lower() and "builder id" not in body.lower():
        return

    K.log("  Re-auth required...")
    pi = page.locator('input[type="password"]:visible')
    if pi.count() > 0:
        O.human_type(pi.first, password)
        O.click_submit(page)
        time.sleep(5)

        body2 = (page.locator("body").inner_text() or "")[:2000]
        if any(k in body2 for k in ("MFA", "authenticator", "额外验证", "多重身份验证")):
            K.log("    MFA challenge in re-auth...")
            totp = pyotp.TOTP(mfa_secret).now()
            O.fill_first(page,
                ['input[name="mfaCode"]', 'input[autocomplete="one-time-code"]',
                 'input[name="code"]', 'input[type="text"]'],
                totp)
            O.click_submit(page)
            time.sleep(8)

        # CAPTCHA
        frame = O.captcha_visible(page)
        if frame:
            code = O.solve_captcha(page, frame)
            if code:
                O.human_type(frame.locator('input[type="text"]').first, code)
                try:
                    frame.locator('button[type="submit"]').first.click(timeout=5000)
                except Exception:
                    page.keyboard.press("Enter")
                time.sleep(5)


def navigate_to_security(page):
    """从 profile 页导航到安全页（credentials.signin.aws/#/security）。"""
    K.log("  Navigating to Security page...")
    # 先确保在 profile 页
    if "profile.aws.amazon.com" not in page.url:
        page.goto("https://profile.aws.amazon.com/",
                  wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        wait_for_body(page, no_reload=True)

    # 点「安全」tab（链接实际指向 credentials.signin.aws）
    sec = page.locator('a:has-text("Security"), a:has-text("安全")')
    for i in range(sec.count()):
        node = sec.nth(i)
        if node.is_visible():
            node.click()
            time.sleep(15)
            break
    else:
        # fallback: 直接导航
        page.goto("https://us-east-1.credentials.signin.aws/#/security",
                  wait_until="domcontentloaded", timeout=30000)
        time.sleep(8)

    wait_for_body(page, no_reload=True)

    # 安全页可能触发 SSO 重认证（尤其删了 MFA 后），检测并处理
    if "signin.aws" in page.url and "workflowStateHandle" in page.url:
        K.log("    SSO re-auth detected, driving login...")
        passwords = K.pw_variants(os.environ.get("OLD_PWD", ""))
        deadline = time.time() + 120
        def on_security(p):
            return "credentials.signin.aws" in p.url and "security" in p.url

        # 无 MFA 时走 Outlook Web 取邮箱验证码
        otp_cb = None
        binus_pw = os.environ.get("BINUS_PW", "")
        acct_email = os.environ.get("EMAIL", "")
        if binus_pw and acct_email:
            otp_cb = lambda: _fetch_outlook_aws_code(acct_email, binus_pw)

        O.drive_login(page, acct_email, passwords, None,
                      stop=on_security, deadline=deadline,
                      otp_callback=otp_cb)
        time.sleep(5)
        wait_for_body(page, no_reload=True)

    body = (page.locator("body").inner_text() or "")[:500]
    K.log(f"  URL: {page.url[:100]}")
    K.log(f"  Body[:200]: {body[:200]}")
    return "MFA" in body or "多重身份验证" in body or "security" in page.url.lower()


# ============ 改密码 ============


def change_password(page, email, old_pwd, mfa_secret):
    """登录后改密码。返回 (success, new_pwd)。"""
    new_pwd = gen_pwd()
    K.log(f"=== Change password -> {new_pwd} ===")

    # 导航到安全页（「更改密码」按钮在这里）
    if not navigate_to_security(page):
        K.log("  FAIL: could not reach security page")
        return False, new_pwd

    K.log("  Step 3: Click change password")
    clicked = wait_and_click(page, [
        'button:has-text("更改密码")', 'button:has-text("Change password")',
        'a:has-text("更改密码")', 'a:has-text("Change password")',
    ])
    if not clicked:
        body = (page.locator("body").inner_text() or "")[:300]
        K.log(f"  FAIL: button not found. URL={page.url} Body={body}")
        return False, new_pwd
    time.sleep(5)

    # 等改密码表单（可能需要 re-auth）
    K.log("  Step 4: Wait for password form")
    reauthed = False
    for _wait in range(20):
        time.sleep(2)
        pwd_inputs = page.locator('input[type="password"]:visible')
        cnt = pwd_inputs.count()
        if cnt >= 2:
            break
        if cnt == 1 and not reauthed:
            handle_reauth(page, old_pwd, mfa_secret)
            reauthed = True
            continue

    pwd_inputs = page.locator('input[type="password"]:visible')
    cnt = pwd_inputs.count()
    K.log(f"    password inputs: {cnt}")

    if cnt < 2:
        body = (page.locator("body").inner_text() or "")[:300]
        K.log(f"  FAIL: not enough password inputs. URL={page.url} Body={body}")
        return False, new_pwd

    O.human_type(pwd_inputs.nth(0), new_pwd)
    O.human_type(pwd_inputs.nth(1), new_pwd)
    time.sleep(1)

    clicked = wait_and_click(page, [
        'button:has-text("更改密码")', 'button:has-text("Change password")',
        'button:has-text("Save")',
    ], timeout=10)
    time.sleep(8)

    body = (page.locator("body").inner_text() or "")[:300]
    if "成功" in body or "success" in body.lower() or "changed" in body.lower():
        K.log(f"  OK password changed: {new_pwd}")
        return True, new_pwd
    K.log(f"  Result uncertain, body: {body[:200]}")
    return True, new_pwd

# ============ 改 2FA ============


def change_2fa(page, email, password, old_mfa_secret):
    """改 2FA：导航到安全页 → 选中设备 → 删除 → 注册新设备 → 提取 secret → 验证。"""
    K.log("=== Change 2FA ===")

    # Step 1: 导航到安全页
    if not navigate_to_security(page):
        K.log("  FAIL: could not reach security page")
        return False, None

    SHOT_DIR = Path("/tmp/mfa-change-shots")
    SHOT_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(SHOT_DIR / "2fa_01_security.png"), full_page=True)

    # Step 2: 选中现有 MFA 设备（radio button）再删除
    K.log("  Step 2: Select and delete old MFA device")
    # 页面上 MFA 设备表有 radio 按钮，必须先选中才能点删除
    radio = page.locator('input[type="radio"]:visible')
    if radio.count() > 0:
        radio.first.click()
        time.sleep(1)
        K.log(f"    selected radio (count={radio.count()})")

    # 精确匹配「删除」按钮（JS 绕过 Playwright :text-is CJK 问题）
    clicked = js_click_exact_button(page, ["删除", "Delete"], exclude_texts=["所有", "all"], timeout=15)
    if clicked:
        time.sleep(5)
        page.screenshot(path=str(SHOT_DIR / "2fa_02_delete_modal.png"), full_page=True)

        # MFA 删除确认弹窗 — 可能要输入确认文字或 TOTP
        body = (page.locator("body").inner_text() or "")[:1500]
        K.log(f"    modal body[:300]: {body[:300]}")

        code_input = page.locator('input[type="text"]:visible, input[inputmode="numeric"]:visible')
        if code_input.count() > 0:
            placeholder = (code_input.first.get_attribute("placeholder") or "").strip()
            K.log(f"    input placeholder: '{placeholder}'")

            # 判断：文字确认 vs TOTP
            # AWS 删除 MFA 弹窗：placeholder 含 "delete" 或弹窗提示输入 "delete all"
            is_text_confirm = False
            confirm_text = ""
            if "delete" in placeholder.lower():
                is_text_confirm = True
                confirm_text = placeholder  # placeholder 本身就是要输入的文字
            else:
                # 扫描弹窗文字：找 "type 'delete all'" / "输入 delete all" 之类
                import re as _re
                m = _re.search(r"['\"]?(delete\s*(?:all)?)['\"]?", body.lower())
                if m:
                    is_text_confirm = True
                    confirm_text = m.group(1).strip()

            if is_text_confirm:
                if not confirm_text:
                    confirm_text = "delete all"
                K.log(f"    typing confirmation text: '{confirm_text}'")
                O.human_type(code_input.first, confirm_text)
            else:
                remaining = 30 - (int(time.time()) % 30)
                if remaining < 5:
                    time.sleep(remaining + 1)
                totp = pyotp.TOTP(old_mfa_secret).now()
                K.log(f"    typing TOTP: {totp}")
                O.human_type(code_input.first, totp)
            time.sleep(2)

        # 确认删除 — 限定在弹窗内找按钮，避免点到工具栏同名"删除"
        page.evaluate("""(targets) => {
            const dlg = document.querySelector('[role="dialog"], [class*="modal"], [class*="Modal"]');
            const scope = dlg || document;
            const btns = [...scope.querySelectorAll('button')];
            for (const btn of btns) {
                const t = btn.textContent.trim();
                if (targets.includes(t) && btn.offsetParent !== null) { btn.click(); return t; }
            }
            // fallback: 从后往前找（弹窗按钮一般在 DOM 尾部）
            const all = [...document.querySelectorAll('button')].reverse();
            for (const btn of all) {
                const t = btn.textContent.trim();
                if (targets.includes(t) && btn.offsetParent !== null) { btn.click(); return t; }
            }
            return null;
        }""", ["删除", "Delete", "确认", "Confirm"])
        time.sleep(2)
        time.sleep(8)
        page.screenshot(path=str(SHOT_DIR / "2fa_03_after_delete.png"), full_page=True)
        K.log("    old device deleted")

        # 删除 MFA 后 AWS 可能强制登出 — 检测并重新登录
        time.sleep(3)
        body_after = (page.locator("body").inner_text() or "")[:500]
        url_after = page.url
        K.log(f"    after delete URL: {url_after[:80]}")
        if ("signin" in url_after or "login" in url_after
                or "Get started" in body_after or len(body_after.strip()) < 30):
            K.log("    Session lost after MFA delete — re-login (no TOTP needed)")
            page.goto("https://profile.aws.amazon.com/",
                      wait_until="domcontentloaded", timeout=60000)
            passwords = K.pw_variants(password)
            deadline = time.time() + O.HEADFUL_WAIT
            def relogged(p):
                return "profile.aws.amazon.com" in p.url and "#/profile" in p.url
            O.drive_login(page, email, passwords, None,
                          stop=relogged, deadline=deadline)
            time.sleep(3)
            page.screenshot(path=str(SHOT_DIR / "2fa_03b_relogin.png"), full_page=True)
            if not relogged(page):
                K.log("    FAIL: re-login failed")
                return False, None
            K.log("    re-login OK")
            # 重新导航到安全页
            if not navigate_to_security(page):
                K.log("    FAIL: could not reach security page after re-login")
                return False, None
            page.screenshot(path=str(SHOT_DIR / "2fa_03c_security_again.png"), full_page=True)
    else:
        K.log("    no delete button / no device to delete")

    # Step 3: 注册新 MFA 设备
    K.log("  Step 3: Register new MFA device")
    clicked = wait_and_click(page, [
        'button:has-text("注册设备")', 'button:has-text("Register device")',
        'button:has-text("Register")',
    ], timeout=20)
    if not clicked:
        K.log("  FAIL: Register device button not found")
        page.screenshot(path=str(SHOT_DIR / "2fa_03_no_register.png"), full_page=True)
        return False, None
    time.sleep(10)
    wait_for_body(page, min_len=20, retries=8, sleep_s=8, no_reload=True)
    page.screenshot(path=str(SHOT_DIR / "2fa_04_register_type.png"), full_page=True)
    body = (page.locator("body").inner_text() or "")[:500]
    K.log(f"    register page body[:200]: {body[:200]}")

    # 选 Authenticator app（文档：选设备类型页）
    K.log("  Step 4: Choose Authenticator app")
    for sel in [
        'text=Authenticator app', 'text=身份验证器应用程序',
        'text=身份验证器', 'label:has-text("Authenticator")',
        'input[value="TOTP"]',
    ]:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                K.log(f"    clicked: {sel}")
                time.sleep(3)
                break
        except Exception:
            continue

    # 可能有「下一步」按钮
    wait_and_click(page, [
        'button:has-text("Next")', 'button:has-text("下一步")',
        'button:has-text("Continue")', 'button:has-text("继续")',
    ], timeout=8)
    time.sleep(8)
    wait_for_body(page, min_len=20, retries=8, sleep_s=8, no_reload=True)
    page.screenshot(path=str(SHOT_DIR / "2fa_05_setup_authenticator.png"), full_page=True)
    body = (page.locator("body").inner_text() or "")[:500]
    K.log(f"    authenticator setup body[:200]: {body[:200]}")

    # Step 5: 显示 secret key
    K.log("  Step 5: Show secret key")
    for sel in [
        'button:has-text("Show secret key")', 'button:has-text("显示密钥")',
        'a:has-text("Show secret key")', 'a:has-text("show secret key")',
        'a:has-text("显示密钥")', 'text=Show secret key',
        'button:has-text("Show QR code")', 'button:has-text("显示 QR 码")',
        'a:has-text("Can\'t scan")', 'a:has-text("无法扫描")',
    ]:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                K.log(f"    clicked: {sel}")
                time.sleep(5)
                break
        except Exception:
            continue
    time.sleep(5)
    wait_for_body(page, min_len=20, retries=5, sleep_s=5, no_reload=True)
    page.screenshot(path=str(SHOT_DIR / "2fa_06_secret_shown.png"), full_page=True)

    # Step 6: 提取 secret（三种方法）
    K.log("  Step 6: Extract secret")
    page_text = page.locator("body").inner_text() or ""
    page_html = page.evaluate("document.body.innerHTML") or ""

    new_secret = None

    # 方法 1: otpauth URI 里的 secret 参数
    match = re.search(r"secret=([A-Z2-7]{16,64})", page_html, re.IGNORECASE)
    if match:
        new_secret = match.group(1).upper()
        K.log(f"    found via otpauth URI: {new_secret}")

    # 方法 2: 页面文本中的 Base32 串（排除旧 secret）
    if not new_secret:
        for m in re.findall(r"\b([A-Z2-7]{16,64})\b", page_text):
            if m != old_mfa_secret:
                new_secret = m
                K.log(f"    found via text scan: {new_secret}")
                break

    # 方法 3: DOM code/kbd/secret 元素
    if not new_secret:
        new_secret = page.evaluate("""() => {
            for (const el of document.querySelectorAll('code, kbd, .text-mono, [class*="secret"], [data-testid*="secret"]')) {
                const t = el.textContent.trim().replace(/\\s/g, '');
                if (/^[A-Z2-7]{16,64}$/.test(t)) return t;
            }
            return null;
        }""")
        if new_secret:
            K.log(f"    found via DOM element: {new_secret}")

    if not new_secret:
        K.log("  FAIL: could not extract TOTP secret")
        K.log(f"  Page text[:500]: {page_text[:500]}")
        page.screenshot(path=str(SHOT_DIR / "2fa_06_no_secret.png"), full_page=True)
        return False, None

    # Step 7: 验证新 TOTP
    K.log(f"  Step 7: Verify with new TOTP (secret={new_secret})")
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)

    verify_code = pyotp.TOTP(new_secret).now()
    K.log(f"    code: {verify_code}")

    O.fill_first(page, [
        'input[type="text"]:visible', 'input[inputmode="numeric"]:visible',
    ], verify_code)

    clicked = wait_and_click(page, [
        'button:has-text("Assign MFA")', 'button:has-text("分配 MFA")',
        'button:has-text("Assign")', 'button:has-text("分配")',
        'button:has-text("Verify")', 'button:has-text("验证")',
        'button:has-text("Submit")', 'button:has-text("提交")',
    ], timeout=10)
    time.sleep(5)
    page.screenshot(path=str(SHOT_DIR / "2fa_07_result.png"), full_page=True)

    body = (page.locator("body").inner_text() or "")[:500]
    K.log(f"  Result body[:200]: {body[:200]}")

    # 检查是否回到了安全页（说明注册成功）
    if any(k in body for k in ("MFA", "多重身份验证", "设备", "success", "成功")):
        K.log(f"  OK 2FA changed: {new_secret}")
        return True, new_secret

    K.log(f"  Result uncertain")
    return True, new_secret


# ============ 主入口 ============


def main():
    mfa_deleted = MFA_SECRET.upper() in ("NONE", "DELETED", "")
    if not EMAIL or not OLD_PWD or (not MFA_SECRET and not mfa_deleted):
        print("需要环境变量: EMAIL, OLD_PWD, MFA_SECRET (MFA_SECRET=NONE 表示已删除)", file=sys.stderr)
        sys.exit(1)

    K.log(f"Target: {EMAIL}")
    K.log(f"Operations: pwd={DO_PWD}, 2fa={DO_2FA}")

    # 清理旧 Chrome
    O.cleanup_chrome() if hasattr(O, "cleanup_chrome") else subprocess.run(
        ["pkill", "-9", "-f", f"remote-debugging-port={O.CDP_PORT}"], capture_output=True)
    time.sleep(2)

    profile_dir = Path(f"/tmp/kiro-chrome-chpwd-{os.getpid()}-{int(time.time())}")
    chrome = O.launch_chrome(profile_dir)

    results = {"email": EMAIL, "time": time.strftime("%Y-%m-%d %H:%M:%S")}

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{O.CDP_PORT}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 登录：复用 drive_login 状态机
            # profile.aws.amazon.com 触发 Builder ID 登录
            K.log("=== Login via profile.aws.amazon.com ===")
            page.goto("https://profile.aws.amazon.com/",
                      wait_until="domcontentloaded", timeout=60000)

            passwords = K.pw_variants(OLD_PWD)
            deadline = time.time() + O.HEADFUL_WAIT

            def logged_in(p):
                url = p.url
                return "profile.aws.amazon.com" in url and "#/profile" in url

            otp_cb = None
            binus_pw = os.environ.get("BINUS_PW", "")
            if binus_pw and EMAIL:
                otp_cb = lambda: _fetch_outlook_aws_code(EMAIL, binus_pw)
            login_seed = None if mfa_deleted else MFA_SECRET
            O.drive_login(page, EMAIL, passwords, login_seed,
                          stop=logged_in, deadline=deadline,
                          otp_callback=otp_cb)

            if not logged_in(page):
                K.log("FAIL: login did not reach profile page")
                sys.exit(3)
            K.log("  Logged in!")
            time.sleep(3)

            current_pwd = OLD_PWD
            current_mfa = MFA_SECRET

            if DO_PWD:
                pwd_ok, new_pwd = change_password(page, EMAIL, current_pwd, current_mfa)
                results["pwd_ok"] = pwd_ok
                results["new_pwd"] = new_pwd
                if pwd_ok:
                    current_pwd = new_pwd
                else:
                    K.log("  Password change failed")

            if DO_2FA:
                tfa_ok, new_secret = change_2fa(page, EMAIL, current_pwd, current_mfa)
                results["2fa_ok"] = tfa_ok
                results["new_2fa"] = new_secret

    except Exception as e:
        K.log(f"ERROR: {str(e)[:300]}")
        results["error"] = str(e)[:300]
    finally:
        try:
            chrome.terminate()
        except Exception:
            pass
        subprocess.run(["pkill", "-9", "-f", f"remote-debugging-port={O.CDP_PORT}"],
                       capture_output=True)

    # 写结果
    safe_name = EMAIL.split("@")[0].replace(".", "_")
    result_file = f"/tmp/kiro-pwd-2fa-{safe_name}.json"
    with open(result_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    os.chmod(result_file, 0o600)

    K.log(f"\n{'=' * 50}")
    K.log(f"Results saved to {result_file}")
    if results.get("pwd_ok"):
        K.log(f"  New password: {results['new_pwd']}")
    if results.get("new_2fa"):
        K.log(f"  New 2FA: {results['new_2fa']}")
    K.log(f"{'=' * 50}")

    if DO_PWD and not results.get("pwd_ok"):
        sys.exit(4)
    if DO_2FA and not results.get("2fa_ok"):
        sys.exit(5)


if __name__ == "__main__":
    main()
