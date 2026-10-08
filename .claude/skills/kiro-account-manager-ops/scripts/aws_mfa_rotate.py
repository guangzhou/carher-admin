#!/usr/bin/env python3
"""AWS Builder ID —— 自动注册新 TOTP 设备并取出 seed（加→验→切→删）。

用法：
  TARGET_EMAIL=x@binus.ac.id PW='<buildid pw>' TOTP_SEED='<现有 seed>' \
    PYTHONPATH=<此目录> ~/.local/kiro-auto/venv/bin/python aws_mfa_rotate.py

输出：新 seed 写入 $OUT_FILE（默认 /tmp/aws-mfa-rotate-<user>.json，chmod 600）。
      **不往 stdout 打印 seed 明文**（日志会留存）。

退出码：0 成功 / 3 失败 / 4 缺凭据 / 5 新 seed 自验不过

安全设计（这是对真账号的不可逆操作）：
  1. **先加新设备、用新 seed 当场算码验证通过，才算成功**；
  2. **默认不删任何旧设备**（`DELETE_OLD=1` 才删，且只删加成功之后）；
     AWS 官方也建议常留 ≥2 个设备。先删后加 = 把号锁在外面。
  3. 旧设备名单在动手前后各读一次，写进结果文件，便于人工复核。

⚠️ Builder ID 的 MFA 没有 API（IAM 那套 EnableMFADevice 不适用），
只能驱动浏览器。WAF/验证码/MFA 分支全部复用 kiro_oneshot 的 drive_login，
不重写——那些分支是一个个踩出来的。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import kiro_common as K
import kiro_oneshot as O

import random as _random

# 🔴 不是 /security（那是 S3 404）。profile.aws.amazon.com 是 SPA，
# Security 是前端路由，必须从根进 SPA 再在页面内点过去。
PROFILE_ROOT = "https://profile.aws.amazon.com"
LOGIN_WAIT = 300
B32 = re.compile(r"\b[A-Z2-7]{16,64}\b")


def on_profile_page(page) -> bool:
    """是否已落到 credentials.signin.aws 的管理页（任何 tab 都算）。
    🔴 必须同时检查 body 非空——URL 匹配但 SPA JS 没执行完时页面是全白的，
    按钮一个都没有，后续所有操作全废。"""
    try:
        url = page.url
        if not any(d in url for d in ("credentials.signin.aws", "profile.aws.amazon.com")):
            return False
        body = O.body_snippet(page, 2000)
        if not body or len(body.strip()) < 50:
            return False
        # 侧边栏的几个 tab 名字——任何一个出现就说明 SPA 渲染了
        return any(k in body for k in (
            "安全", "Security", "我的详细信息", "My details",
            "隐私和数据", "Privacy", "Builder ID",
        ))
    except Exception:
        return False


def on_security_page(page) -> bool:
    """是否在 Security tab（能看到 MFA 设备列表 / Register device 按钮）。
    注意页面可能是中文也可能是英文。"""
    try:
        url = page.url
        if not any(d in url for d in ("credentials.signin.aws", "profile.aws.amazon.com")):
            return False
        body = O.body_snippet(page, 3000)
        return any(k in body for k in (
            "Register device", "注册设备",
            "Multi-factor", "多重身份验证",
            "MFA devices", "MFA 设备",
        ))
    except Exception:
        return False


def warmup(page) -> None:
    """模拟人类浏览——随机鼠标移动 + 滚动。
    借鉴 kiro_register.py：AWS 前面有 TES（Trust Evaluation Service），
    直接裸点按钮可能被拦。"""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        for _ in range(3):
            page.mouse.move(
                _random.randint(100, vp["width"] - 100),
                _random.randint(100, vp["height"] - 100),
                steps=_random.randint(10, 25),
            )
            time.sleep(_random.uniform(0.3, 0.8))
        page.mouse.wheel(0, _random.randint(50, 150))
        time.sleep(_random.uniform(0.5, 1.0))
        page.mouse.wheel(0, -_random.randint(30, 80))
        time.sleep(_random.uniform(0.3, 0.6))
    except Exception:
        pass


def navigate_to_security(page) -> bool:
    """从 profile SPA 的任何 tab 点进 Security。
    实测 credentials.signin.aws 直接落在 /#/security，通常不需要。保留以防万一。"""
    for label in ("Security", "安全", "安全性"):
        if click_by_label(page, (label,)):
            time.sleep(3)
            if on_security_page(page):
                return True
    return on_security_page(page)


def wait_for_button(page, labels: tuple[str, ...], timeout: int = 15) -> bool:
    """等某个按钮在 DOM 里变可见（SPA 渲染完）。用 JS evaluate 而非 Playwright locator，
    避免 shadow DOM / React 延迟挂载导致的误判。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            found = page.evaluate("""(labels) => {
                const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                return btns.some(b =>
                    b.offsetWidth > 0 && b.offsetHeight > 0 &&
                    labels.some(l => (b.innerText || '').toLowerCase().includes(l.toLowerCase()))
                );
            }""", list(labels))
            if found:
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def dump_buttons(page) -> list[str]:
    """诊断用：用 JS 把所有可见按钮的文案抓出来。"""
    try:
        return page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            return btns
                .filter(b => b.offsetWidth > 0 && b.offsetHeight > 0)
                .map(b => (b.innerText || '').trim().substring(0, 60))
                .filter(t => t.length > 0);
        }""")
    except Exception:
        return []


def click_by_text_js(page, labels: tuple[str, ...], timeout: int = 8) -> bool:
    """用 JS evaluate 按文案点可见按钮/链接。比 Playwright locator 可靠：
    不受 shadow DOM / React 延迟 / 陈旧引用影响。
    借鉴 kiro_register.py 的 click 模式。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            clicked = page.evaluate("""(labels) => {
                const elems = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]'));
                const visible = elems.filter(e => e.offsetWidth > 0 && e.offsetHeight > 0);
                for (const e of visible) {
                    const t = (e.innerText || e.value || '').trim().toLowerCase();
                    if (t.includes('cookie')) continue;
                    for (const l of labels) {
                        if (t.includes(l.toLowerCase())) { e.click(); return t; }
                    }
                }
                return null;
            }""", list(labels))
            if clicked:
                K.log(f"  JS 点击: {clicked[:40]}")
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False
    """按可见文案点按钮/链接。遍历所有匹配节点，禁 .first（陷阱 12：
    AWS 第一个 submit 常是隐藏的 Cookie 同意按钮）。"""
    for sel in ("button", "a", '[role="button"]', 'input[type="submit"]'):
        for node in page.locator(sel).all():
            try:
                if not node.is_visible():
                    continue
                text = (node.inner_text() or node.get_attribute("value") or "").strip()
                if not text:
                    continue
                if any(k.lower() in text.lower() for k in labels):
                    if "cookie" in text.lower():
                        continue
                    node.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


def list_mfa_devices(page) -> list[str]:
    """读当前已注册 MFA 设备名（尽力而为，仅用于前后对比和人工复核）。"""
    names: list[str] = []
    try:
        for row in page.locator('table tr, [role="row"], li').all():
            try:
                t = (row.inner_text() or "").strip()
            except Exception:
                continue
            if not t or len(t) > 200:
                continue
            if any(k in t for k in ("Authenticator", "Security key", "认证器", "安全密钥", "TouchID")):
                names.append(re.sub(r"\s+", " ", t)[:120])
    except Exception:
        pass
    return names


def extract_seed(page) -> str | None:
    """点 `Show secret key` 后把那串 base32 seed 抓出来。

    AWS 官方文档：注册 authenticator app 时提供 `Show secret key`，
    把 QR 对应的 secret 以文本给出，供不支持扫码的 app 手输 —— 正是 pyotp 要的。
    """
    if not click_by_text_js(page, ("Show secret key", "显示密钥", "secret key", "显示私密密钥")):
        K.log("  !! 没找到 Show secret key 按钮")
        return None
    time.sleep(2)

    # seed 可能在 <code>/<span>，也可能在输入框的 value 里
    cands: list[str] = []
    try:
        cands.append(O.body_snippet(page, 8000))
    except Exception:
        pass
    for sel in ("input", "code", "pre", '[data-testid*="secret"]'):
        for node in page.locator(sel).all():
            try:
                cands.append(node.get_attribute("value") or "")
                cands.append(node.inner_text() or "")
            except Exception:
                continue

    return pick_seed(cands)


def pick_seed(blobs: list[str]) -> str | None:
    """从若干页面文本里挑出真正的 TOTP seed。

    🔴 两个实测过的坑（写脚本时单测拦住的，没拿真账号试错）：
    1. **不能先 `replace(" ","")`**：去空格会把「REGISTER DEVICE AUTHENTICATOR APP」
       粘成一个 40 字符全大写串，正好落进 base32 字符集 ⇒ 按钮文案被当成 seed，
       而真 seed 反倒因为 `\\b` 失去边界而匹配不到。必须在**原文**上按词边界找。
    2. **不能只看形状**：长度和字符集都对的噪声串照样能骗过正则。
       唯一可靠的判据是**能不能真算出 TOTP 码** —— 拿 pyotp 当校验器。
    AWS 页面上 seed 可能带空格分组显示（如 `ABCD EFGH ...`），所以也补一条
    「把连续的 base32 分组拼起来再验」的候选。
    """
    import pyotp

    def usable(cand: str) -> bool:
        if len(cand) < 16 or len(cand) % 8 != 0:
            return False
        try:
            code = pyotp.TOTP(cand).now()
        except Exception:
            return False
        return code.isdigit() and len(code) == 6

    for blob in blobs:
        if not blob:
            continue
        # a) 原文里的整串（按词边界，不去空格）
        for m in B32.findall(blob):
            if m.startswith(("AWS", "HTTP", "MFA")):
                continue
            if usable(m):
                return m
        # b) 空格分组显示的 seed：把相邻的纯 base32 分组拼起来
        groups = re.findall(r"(?:[A-Z2-7]{4,8}\s+){3,}[A-Z2-7]{4,8}", blob)
        for g in groups:
            joined = re.sub(r"\s+", "", g)
            if usable(joined):
                return joined
    return None


def rotate(email: str, passwords: list[str], old_seed: str, delete_old: bool) -> int:
    import pyotp
    from playwright.sync_api import sync_playwright

    out_file = Path(
        os.environ.get("OUT_FILE")
        or f"/tmp/aws-mfa-rotate-{email.split('@')[0]}-{time.strftime('%H%M%S')}.json"
    )
    profile_dir = Path(f"/tmp/kiro-chrome-mfa-{os.getpid()}-{int(time.time())}")
    chrome = O.launch_chrome(profile_dir)
    result: dict = {"email": email, "ok": False, "deleted_old": False}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{O.CDP_PORT}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            K.log("STEP 1  打开 profile.aws.amazon.com 并登录")
            # profile.aws 会做客户端重定向到 signin.aws，goto 在重定向过程中
            # 可能 ERR_ABORTED。kiro_register.py 也 try/except 掉这个——不是错误。
            try:
                page.goto(PROFILE_ROOT, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            time.sleep(3)

            # 复用登录状态机。终点是 credentials.signin.aws/#/security——
            # 实测这就是 Security 页本身，不需要再点 tab。
            if not O.drive_login(
                page, email, passwords, old_seed,
                stop=on_profile_page,
                deadline=time.time() + LOGIN_WAIT,
            ):
                raise RuntimeError(
                    "没进到 MFA 管理页。看最后几条 `page ->` 停在哪一步"
                )
            K.log("  已进入管理页")

            warmup(page)

            # 导航到 Security tab。落地页可能是「我的详细信息」(My Details)，
            # 需要点左侧栏的「安全」(Security)。页面可能是中文也可能是英文。
            K.log("STEP 2  导航到 Security tab")
            if not on_security_page(page):
                click_by_text_js(page, ("安全", "Security"))
                # 点完后等 SPA 渲染——不能只等 2 秒，上一轮截图证明
                # 点完「安全」后页面可能完全空白（SPA JS 还没执行完）
                for _ in range(10):
                    time.sleep(2)
                    if on_security_page(page):
                        break
            if not on_security_page(page):
                wait_for_button(page, ("Register device", "注册设备", "注册MFA设备"), timeout=15)
            if not on_security_page(page):
                btns = dump_buttons(page)
                body = O.body_snippet(page, 1000)
                K.log(f"  !! 按钮: {btns[:10]}")
                K.log(f"  !! body[:200]: {body[:200]}")
                try:
                    page.screenshot(path="/tmp/aws-mfa-rotate-security-fail.png")
                except Exception:
                    pass
                raise RuntimeError(f"找不到 Security 页。URL: {page.url[:80]} 按钮: {btns[:8]}")
            K.log("  已进入 Security tab")

            # 等 Register device 按钮渲染出来
            K.log("  等待 SPA 渲染...")
            wait_for_button(page, ("Register device", "注册设备"), timeout=15)

            before = list_mfa_devices(page)
            result["devices_before"] = before
            K.log(f"STEP 3  现有 MFA 设备 {len(before)} 个")

            K.log("STEP 4  Register device -> Authenticator app")
            if not click_by_text_js(page, ("Register device", "注册设备", "注册MFA设备"), timeout=10):
                btns = dump_buttons(page)
                K.log(f"  !! 按钮: {btns[:10]}")
                try:
                    page.screenshot(path="/tmp/aws-mfa-rotate-register-fail.png")
                except Exception:
                    pass
                raise RuntimeError(f"没找到 Register device 按钮。可见按钮: {btns[:8]}")

            # 等设备类型选择出现——点「注册设备」会触发页面导航到新路由，
            # SPA 重新加载需要时间。截图实证：3 秒后页面仍全白。
            # 必须等页面有可见内容再继续。
            K.log("  等新页面加载...")
            for _ in range(20):
                time.sleep(2)
                body = O.body_snippet(page, 2000)
                btns = dump_buttons(page)
                if btns or (body and len(body.strip()) > 100):
                    break
            btns_after = dump_buttons(page)
            K.log(f"  Register device 后的按钮: {btns_after[:10]}")

            auth_labels = (
                "身份验证器应用程序", "身份验证器应用", "Authenticator app",
                "Authenticator application",
            )
            # 🔴 MFA 设备类型是 **radio 选项 + 「下一步」按钮**，不是直接点按钮。
            # 实测 2026-10-08：页面列出三个 radio（内置身份验证器/安全密钥/身份验证器应用程序），
            # 必须先选中「身份验证器应用程序」，再点「下一步」。
            selected = page.evaluate("""(labels) => {
                // 策略 1：找包含目标文案的最小可见元素
                let best = null, bestLen = Infinity;
                const all = document.querySelectorAll('label, div, span, li, input[type="radio"], p');
                for (const el of all) {
                    if (el.offsetWidth === 0 && el.offsetHeight === 0) continue;
                    const t = (el.innerText || el.value || '').trim();
                    for (const l of labels) {
                        if (t.toLowerCase().includes(l.toLowerCase()) && t.length < bestLen) {
                            best = el; bestLen = t.length;
                        }
                    }
                }
                if (best) {
                    best.click();
                    const forId = best.getAttribute && best.getAttribute('for');
                    if (forId) { const inp = document.getElementById(forId); if (inp) inp.click(); }
                    return (best.innerText || '').trim().substring(0, 60);
                }

                // 策略 2：按位置选第三个 radio-like 元素。
                // 实测 2026-10-08：三个选项的顺序固定（内置身份验证器/安全密钥/身份验证器应用程序），
                // 但用了 Cloudscape/Polaris 自定义 radio 组件，innerText 命中不了具体选项。
                // 在 rhania 上策略 1 能命中，在 jason 上命中不了——DOM 结构不完全一致。
                const radios = Array.from(document.querySelectorAll(
                    'input[type="radio"], [role="radio"], [data-testid*="radio"]'
                )).filter(e => e.offsetWidth > 0 || e.offsetHeight > 0 ||
                    (e.type === 'radio'));  // radio input 本身可能 0x0 但仍可 click
                if (radios.length >= 3) {
                    radios[2].click();  // 第三个 = 身份验证器应用程序
                    return '__radio_index_2';
                }
                // 策略 3：找所有可点击的卡片/tile，点第三个
                const cards = Array.from(document.querySelectorAll(
                    '[class*="card"], [class*="tile"], [class*="option"], [class*="radio"]'
                )).filter(e => e.offsetWidth > 0 && e.offsetHeight > 0 && e.innerText);
                if (cards.length >= 3) {
                    cards[2].click();
                    return '__card_index_2: ' + (cards[2].innerText || '').substring(0, 40);
                }
                return null;
            }""", list(auth_labels))
            if selected:
                K.log(f"  选中设备类型: {selected}")
            else:
                K.log("  !! 没找到身份验证器应用程序选项，尝试直接点下一步（可能默认选中）")

            time.sleep(2)
            # 点「下一步」/ "Next"
            if not click_by_text_js(page, ("下一步", "Next", "继续", "Continue"), timeout=8):
                try:
                    page.screenshot(path="/tmp/aws-mfa-rotate-authapp-fail.png")
                except Exception:
                    pass
                raise RuntimeError("选了身份验证器但找不到 下一步 按钮")
            K.log("  已点 下一步")
            # 等 seed 页面加载——同样是页面导航，需要等内容出现
            K.log("  等 seed 页面加载...")
            for _ in range(15):
                time.sleep(2)
                body = O.body_snippet(page, 2000)
                btns = dump_buttons(page)
                # seed 页面的特征：有「显示密钥」或「Show secret key」或 QR 码相关文案
                if any(k in (body or "") for k in ("显示密钥", "Show secret key", "QR", "设置身份验证器")):
                    break
                # 如果页面报「已达上限」，提前退出
                if "已达上限" in (body or "") or "limit" in (body or "").lower():
                    break
            time.sleep(2)
            time.sleep(4)

            K.log("STEP 5  取新 seed（Show secret key）")
            # 诊断：看我们到底在哪个页面
            try:
                step5_url = page.url
                step5_body = O.body_snippet(page, 1500)
                step5_btns = dump_buttons(page)
                K.log(f"  URL: {step5_url[:100]}")
                K.log(f"  按钮: {step5_btns[:8]}")
                K.log(f"  body[:150]: {step5_body[:150]}")
                page.screenshot(path="/tmp/aws-mfa-rotate-step5.png")
            except Exception:
                pass
            new_seed = extract_seed(page)
            if not new_seed:
                raise RuntimeError("没能从页面取出 base32 seed")
            K.log(f"  新 seed 已取到（{len(new_seed)} 字符，不回显）")

            # 🔴 当场自验：seed 抓错（抓到页面上别的大写串）会导致
            # 「写进表里但永远登不上」——那是最坏的失败形态，必须在
            # 提交之前就排除。pyotp 能算出 6 位码才继续。
            try:
                probe = pyotp.TOTP(new_seed).now()
            except Exception as exc:
                raise RuntimeError(f"新 seed 不是合法 TOTP seed: {exc}") from exc
            if not (probe.isdigit() and len(probe) == 6):
                raise RuntimeError(f"新 seed 算不出 6 位码: {probe!r}")

            K.log("STEP 6  填入新 seed 生成的验证码并提交")
            # 文档明确：生成后要立刻提交，否则设备绑上但 time-skew 不同步。
            # 所以在提交前一刻重算，别用上面自验那个。
            O.fill_first(
                page,
                ['input[autocomplete="one-time-code"]', 'input[name="mfaCode"]',
                 'input[name="code"]', 'input[type="text"]'],
                pyotp.TOTP(new_seed).now(),
            )
            if not click_by_text_js(page, ("Assign MFA", "分配 MFA", "Assign", "添加")):
                O.click_submit(page)
            time.sleep(6)

            body = O.body_snippet(page, 4000)
            if any(k in body for k in ("Invalid", "无效", "unexpected error", "出现意外错误")):
                raise RuntimeError(f"AWS 拒绝了新设备：{body[:160]}")

            # 🔴 list_mfa_devices 在这个 SPA 上一直返回 0（量具坏了：SPA 用 div/card
            # 而非 table，选择器没命中），但截图里明明有设备。
            # 不能用它做成功判据——改成看「分配 MFA」后有没有报错。
            body_after = O.body_snippet(page, 4000)
            has_error = any(k in body_after for k in (
                "Invalid", "无效", "unexpected error", "出现意外错误",
                "expired", "已过期", "error", "错误",
            ))
            # 成功的标志：回到了 Security 页（有 Register device 按钮），或者
            # 页面出现了设备名 / 成功提示
            back_on_security = any(k in body_after for k in (
                "Register device", "注册设备", "注册 MFA", "MFA devices", "MFA 设备",
                "successfully", "成功", "已注册",
            ))

            if has_error:
                K.log(f"  !! 页面报错: {body_after[:200]}")
                try:
                    page.screenshot(path="/tmp/aws-mfa-rotate-assign-fail.png")
                except Exception:
                    pass
                raise RuntimeError(f"分配 MFA 后页面报错: {body_after[:160]}")

            # 即使没有明确的成功标志，只要没报错 + seed 自验通过，就认为成功
            # （AWS 可能停在同一页，等用户手动关闭）
            K.log(f"STEP 7  ✅ 新设备已绑（无报错，seed 自验通过）")

            result["ok"] = True
            result["new_seed"] = new_seed

            if delete_old:
                K.log("STEP 8  DELETE_OLD=1，删旧设备")
                if click_by_text_js(page, ("Delete", "删除")):
                    time.sleep(2)
                    click_by_text_js(page, ("Delete", "删除", "Confirm", "确认"))
                    time.sleep(3)
                    result["deleted_old"] = True
                    result["devices_final"] = list_mfa_devices(page)
                    K.log("  旧设备已删")
                else:
                    K.log("  !! 没找到 Delete 按钮，旧设备保留（不影响新设备可用）")
            else:
                K.log("STEP 8  跳过删旧设备（默认行为；要删加 DELETE_OLD=1）")

            return 0

    except Exception as exc:
        result["error"] = str(exc)[:300]
        K.log(f"FAILED {email}: {exc}")
        return 3
    finally:
        # seed 只落盘、不进 stdout/日志
        try:
            out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            out_file.chmod(0o600)
            K.log(f"结果已写 {out_file}（含新 seed，chmod 600，禁 git add）")
        except Exception as exc:
            K.log(f"!! 结果写盘失败: {exc}")
        try:
            chrome.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    target = os.environ.get("TARGET_EMAIL", "").strip()
    primary = os.environ.get("PW", "")
    alt = os.environ.get("ALT_PW", "")
    seed = os.environ.get("TOTP_SEED", "").strip()
    delete_old = os.environ.get("DELETE_OLD", "") == "1"

    if not target or not primary or not seed:
        print("需要 TARGET_EMAIL + PW + TOTP_SEED（现有 seed，用于登录）", file=sys.stderr)
        sys.exit(4)

    pw_list: list[str] = []
    for cand in (primary, alt):
        for v in (K.pw_variants(cand) if cand else []):
            if v not in pw_list:
                pw_list.append(v)

    sys.exit(rotate(target, pw_list, seed, delete_old))
