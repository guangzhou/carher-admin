#!/usr/bin/env python3
"""AWS Builder ID 改密码。

用法：
  TARGET_EMAIL=x@binus.ac.id PW='<现密码>' NEW_PW='<新密码>' TOTP_SEED='<seed>' \
    PYTHONPATH=<此目录> ~/.local/kiro-auto/venv/bin/python aws_change_password.py

流程：profile.aws → 登录 → Security → Change password → 输旧密码 → 输新密码 → 提交
全程不涉及任何物理设备。

退出码：0 成功 / 3 失败 / 4 缺凭据
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import kiro_common as K
import kiro_oneshot as O
import aws_mfa_rotate as R


def change_password(email: str, passwords: list[str], totp_seed: str, new_pw: str) -> int:
    profile_dir = Path(f"/tmp/kiro-chrome-chpw-{os.getpid()}-{int(time.time())}")
    chrome = O.launch_chrome(profile_dir)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{O.CDP_PORT}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # STEP 1: 登录
            K.log("STEP 1  登录 profile.aws.amazon.com")
            try:
                page.goto("https://profile.aws.amazon.com", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            time.sleep(3)

            if not O.drive_login(
                page, email, passwords, totp_seed,
                stop=R.on_profile_page,
                deadline=time.time() + 300,
            ):
                raise RuntimeError("登录失败")
            K.log("  已进入管理页")
            R.warmup(page)

            # STEP 2: 导航到 Security
            K.log("STEP 2  导航到 Security tab")
            R.click_by_text_js(page, ("安全", "Security"), timeout=8)
            time.sleep(4)

            # STEP 3: 点 Change password
            K.log("STEP 3  点 Change password")
            if not R.click_by_text_js(page, ("Change password", "更改密码", "修改密码"), timeout=10):
                btns = R.dump_buttons(page)
                K.log(f"  !! 按钮: {btns[:10]}")
                body = O.body_snippet(page, 1000)
                K.log(f"  !! body[:200]: {body[:200]}")
                try:
                    page.screenshot(path="/tmp/aws-chpw-security.png")
                except Exception:
                    pass
                raise RuntimeError(f"没找到 Change password 按钮。按钮: {btns[:8]}")

            # 等新页面加载
            K.log("  等密码页加载...")
            for _ in range(15):
                time.sleep(2)
                body = O.body_snippet(page, 2000)
                if any(k in body for k in ("Password", "密码", "当前密码", "Current password", "Re-enter")):
                    break

            # STEP 4: 可能需要重新输入当前密码
            K.log("STEP 4  输入当前密码（如果要求）")
            body = O.body_snippet(page, 2000)
            btns = R.dump_buttons(page)
            K.log(f"  URL: {page.url[:100]}")
            K.log(f"  按钮: {btns[:8]}")
            K.log(f"  body[:200]: {body[:200]}")

            # 如果页面要求重新输入当前密码
            if any(k in body for k in ("Re-enter", "重新输入", "当前密码", "Current password", "Verify")):
                O.fill_first(
                    page,
                    ['input[type="password"]', 'input[name="password"]'],
                    passwords[0],  # 现有密码
                )
                R.click_by_text_js(page, ("Sign in", "登录", "Continue", "继续", "Verify", "验证"), timeout=8)
                time.sleep(4)

            # STEP 5: 输入新密码
            K.log("STEP 5  输入新密码")
            body = O.body_snippet(page, 2000)
            K.log(f"  body[:200]: {body[:200]}")
            try:
                page.screenshot(path="/tmp/aws-chpw-newpw.png")
            except Exception:
                pass

            # 找所有密码输入框
            pw_inputs = page.locator('input[type="password"]').all()
            visible_pws = [p for p in pw_inputs if p.is_visible()]
            K.log(f"  可见密码输入框: {len(visible_pws)} 个")

            if len(visible_pws) >= 2:
                # 新密码 + 确认密码
                O.human_type(visible_pws[0], new_pw)
                time.sleep(0.5)
                O.human_type(visible_pws[1], new_pw)
            elif len(visible_pws) == 1:
                O.human_type(visible_pws[0], new_pw)
            else:
                raise RuntimeError("没找到密码输入框")

            time.sleep(1)
            # 提交
            if not R.click_by_text_js(page, (
                "Change password", "更改密码", "Save", "保存", "Update", "更新",
                "Submit", "提交", "Confirm", "确认",
            ), timeout=8):
                O.click_submit(page)
            time.sleep(5)

            # STEP 6: 检查结果
            K.log("STEP 6  检查结果")
            body = O.body_snippet(page, 2000)
            btns = R.dump_buttons(page)
            K.log(f"  body[:300]: {body[:300]}")
            try:
                page.screenshot(path="/tmp/aws-chpw-result.png")
            except Exception:
                pass

            if any(k in body for k in ("error", "错误", "Error", "invalid", "无效", "incorrect", "不正确")):
                raise RuntimeError(f"改密码失败: {body[:200]}")

            K.log("STEP 6  ✅ 密码已更改（无报错）")
            return 0

    except Exception as exc:
        K.log(f"FAILED {email}: {exc}")
        return 3
    finally:
        try:
            chrome.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    target = os.environ.get("TARGET_EMAIL", "").strip()
    primary = os.environ.get("PW", "")
    alt = os.environ.get("ALT_PW", "")
    seed = os.environ.get("TOTP_SEED", "").strip()
    new_pw = os.environ.get("NEW_PW", "").strip()

    if not target or not primary or not seed or not new_pw:
        print("需要 TARGET_EMAIL + PW + TOTP_SEED + NEW_PW", file=sys.stderr)
        sys.exit(4)

    pw_list: list[str] = []
    for cand in (primary, alt):
        for v in (K.pw_variants(cand) if cand else []):
            if v not in pw_list:
                pw_list.append(v)

    sys.exit(change_password(target, pw_list, seed, new_pw))
