#!/usr/bin/env python3
"""过 AWS WAF 的九宫格选图挑战，拿到 WAF token 后存盘复用。

机制（2026-10-19 实测）：
  kiro.dev/login?idp=Github → 302 Cognito/oauth2/authorize → 302
  kiro.dev/github/authorize → **AWS WAF 截下**（HTTP 405 + Human Verification 页）
  ⇒ 永远到不了 github.com，kiro_github_login.py 的填表逻辑一次都不触发，空转超时。

本脚本：点 Begin → 把 320×320 canvas 切成 3×3 存成单格图 → 等人/模型指认哪几格
→ 点选 → Confirm → 成功后导出 cookie（WAF token 在 `aws-waf-token`）。

用法：
  1) python3 waf_solve.py dump      # 开浏览器、点 Begin、切图落盘，然后挂住
  2) 看 ghprobe/cells/*.png 判出答案
  3) 在同一个挂住的进程里按提示输入格号（如 3,6,7,8,9）→ 自动点选+Confirm
     成功则把 cookie 写到 ghprobe/waf-cookies.json

🔴 WAF token 是 per-browser-profile 的 cookie，拿到后给后续登录复用，
   不用每个号都过一次挑战。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time

from playwright.sync_api import sync_playwright

KIRO_AUTH = "https://prod.us-east-1.auth.desktop.kiro.dev"
REDIRECT_URI = "kiro://kiro.kiroAgent/authenticate-success"
OUT = "/Users/Liuguoxian/codes/carher-admin/work-kiro-add-1019/ghprobe"
CELLS = f"{OUT}/cells"


def login_url() -> str:
    v = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return (f"{KIRO_AUTH}/login?idp=Github&redirect_uri={REDIRECT_URI}"
            f"&code_challenge={c}&code_challenge_method=S256&state={secrets.token_urlsafe(16)}")


def main() -> int:
    os.makedirs(CELLS, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        page = ctx.new_page()
        try:
            page.goto(login_url(), timeout=60000)
        except Exception:
            pass
        time.sleep(8)

        # 点 Begin
        for sel in ("#amzn-captcha-verify-button", "button:has-text('Begin')"):
            try:
                b = page.locator(sel)
                if b.count() and b.first.is_visible():
                    b.first.click(timeout=5000)
                    print(f"点了 Begin ({sel})")
                    break
            except Exception:
                continue
        time.sleep(6)

        prompt = ""
        try:
            prompt = page.locator("body").inner_text()[:200].replace("\n", " | ")
        except Exception:
            pass
        print(f"题目: {prompt}")

        cv = page.locator("canvas").first
        if not cv.count():
            print("没有 canvas，挑战形态变了"); browser.close(); return 2

        cv.screenshot(path=f"{OUT}/grid.png")
        box = cv.bounding_box()
        print(f"canvas bbox: {box}")

        # 切 3×3
        from PIL import Image
        img = Image.open(f"{OUT}/grid.png")
        W, H = img.size
        cw, ch = W // 3, H // 3
        for r in range(3):
            for c in range(3):
                n = r * 3 + c + 1
                img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)).save(f"{CELLS}/cell{n}.png")
        print(f"已切 9 格 -> {CELLS}/cell1..9.png（整图 {OUT}/grid.png）")

        ans = os.environ.get("WAF_CELLS", "").strip()
        if not ans:
            print("\n设 WAF_CELLS=3,6,7,8,9 再跑，或现在输入格号（逗号分隔）:")
            try:
                ans = input("> ").strip()
            except EOFError:
                print("非交互，退出。下次用 WAF_CELLS=... 传答案")
                browser.close(); return 3

        cells = [int(x) for x in ans.replace(" ", "").split(",") if x]
        print(f"点选: {cells}")
        for n in cells:
            r, c = (n - 1) // 3, (n - 1) % 3
            x = box["x"] + (c + 0.5) * box["width"] / 3
            y = box["y"] + (r + 0.5) * box["height"] / 3
            page.mouse.click(x, y)
            time.sleep(0.4)

        page.screenshot(path=f"{OUT}/after-select.png", full_page=True)
        for sel in ("#amzn-btn-verify-internal", "button:has-text('Confirm')"):
            try:
                b = page.locator(sel)
                if b.count() and b.first.is_visible():
                    b.first.click(timeout=5000)
                    print(f"点了 Confirm ({sel})")
                    break
            except Exception:
                continue

        for i in range(10):
            time.sleep(3)
            u = page.url
            print(f"  [{i}] {u[:110]}")
            if "github.com" in u:
                print("\n✅ WAF 过了，已跳到 github.com")
                break
            try:
                body = page.locator("body").inner_text()[:120].replace("\n", " | ")
                if "confirm you are human" not in body.lower():
                    print(f"  页面变了: {body}")
            except Exception:
                pass

        cookies = ctx.cookies()
        waf = [c for c in cookies if "waf" in c["name"].lower()]
        with open(f"{OUT}/waf-cookies.json", "w") as f:
            json.dump(cookies, f, indent=2)
        os.chmod(f"{OUT}/waf-cookies.json", 0o600)
        print(f"\ncookie 共 {len(cookies)} 条，WAF 相关 {len(waf)} 条: {[c['name'] for c in waf]}")
        print(f"已存 {OUT}/waf-cookies.json")
        page.screenshot(path=f"{OUT}/final.png", full_page=True)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
