#!/usr/bin/env python3
"""mail.com webmail OTP 抓取 v3 —— 不再用 selector 猜，全部 innerText 驱动。

策略：
1. 登 webmail
2. 进 frame[name='mail'] —— 邮件列表
3. evaluate innerText 拿 visible 文本，找含 'OpenAI' 或 'ChatGPT' 的"行块"
4. 用 page.locator(text=...).first.click() 点开最新一封
5. 等正文 frame 渲染（subject + body 都展开）
6. 从展开后的整页 innerText 取 6 位数（这次范围窄 → 不撞 ad）

不依赖 mail.com 的具体 DOM —— 只依赖 "OpenAI/ChatGPT 文本可见、点击邮件能展开" 这两个稳定行为。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

EMAIL = os.environ["MAIL_USER"]
PW_FILE = os.environ.get("MAIL_LOGIN_PW_FILE", "/run/login_pw.txt")
LOGIN_PW = Path(PW_FILE).read_text().rstrip("\n\r")
SENDER_HINTS_RE = re.compile(r"openai|chatgpt|noreply", re.I)
OTP_RE = re.compile(r"\b(\d{6})\b")
SHOTS = Path("/work/screenshots")
SHOTS.mkdir(exist_ok=True)


def shoot(target, name: str) -> None:
    p = SHOTS / f"v3-{name}.png"
    target.screenshot(path=str(p), full_page=True)
    print(f"  shot: {p}")


def login(page) -> None:
    print(f"login (pw len={len(LOGIN_PW)})")
    page.goto("https://www.mail.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    page.locator("a:has-text('Log in')").first.click()
    time.sleep(2)
    page.locator("input[placeholder='Email address']").first.fill(EMAIL)
    page.locator("input[placeholder='Password']").first.fill(LOGIN_PW)
    btns = page.locator("button:has-text('Log in')")
    for i in range(btns.count()):
        if btns.nth(i).is_visible():
            box = btns.nth(i).bounding_box()
            if box and box["y"] > 50:
                btns.nth(i).click()
                break
    page.wait_for_load_state("domcontentloaded", timeout=20000)
    time.sleep(10)
    if "navigator" not in page.url:
        sys.exit("login failed")
    shoot(page, "01-inbox")


def get_mail_frame(page):
    deadline = time.time() + 20
    while time.time() < deadline:
        for fr in page.frames:
            if fr.name == "mail":
                return fr
        time.sleep(2)
    return None


def find_and_open_openai_message(page, fr) -> dict:
    """In mail frame innerText, locate the first row containing OpenAI/ChatGPT,
    click that text, return diagnostics."""
    text = fr.evaluate("() => document.body.innerText")
    print("=== mail frame innerText (first 2KB) ===")
    print(text[:2000])
    print("=== end ===")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    target_line = None
    for i, ln in enumerate(lines):
        if SENDER_HINTS_RE.search(ln):
            target_line = ln
            print(f"hint match at line {i}: {ln!r}")
            break
    if not target_line:
        sys.exit("no OpenAI/ChatGPT row visible")

    print(f"clicking text: {target_line[:60]!r}")
    fr.get_by_text(target_line, exact=False).first.click()
    time.sleep(5)
    shoot(page, "02-message-opened")

    # body may render in same frame or another; collect all
    out = {"frames": [], "otp": None, "candidates": []}
    for f in page.frames:
        try:
            t = f.evaluate("() => document.body.innerText")
        except Exception:
            continue
        out["frames"].append({"name": f.name, "len": len(t), "first200": t[:200]})
        # only digits inside this frame
        for m in OTP_RE.finditer(t):
            d = m.group(1)
            # contextualize: 60 chars around match
            i = m.start()
            ctx = t[max(0, i - 60): i + 60]
            out["candidates"].append({"frame": f.name, "digit": d, "ctx": ctx})

    # heuristic: pick a candidate where ctx contains 'code' / 'OpenAI' / 'verify'
    keywords = ("code", "openai", "verify", "代码", "登录", "verification")
    for c in out["candidates"]:
        if any(k in c["ctx"].lower() for k in keywords):
            out["otp"] = c["digit"]
            out["matched_via"] = c
            break
    return out


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="en-US")
        page = ctx.new_page()
        try:
            login(page)
            fr = get_mail_frame(page)
            if not fr:
                sys.exit("no mail frame")
            result = find_and_open_openai_message(page, fr)
            print(f"\nframes after open: {len(result['frames'])}")
            for f in result["frames"][:6]:
                print(f"  {f['name']!r} len={f['len']} first200={f['first200'][:120]!r}")
            print(f"\n6-digit candidates: {len(result['candidates'])}")
            for c in result["candidates"][:8]:
                print(f"  digit={c['digit']} frame={c['frame']!r} ctx={c['ctx']!r}")
            if result.get("otp"):
                print(f"\n✅ OTP: {result['otp']}  (matched ctx: {result['matched_via']['ctx']!r})")
            else:
                print("\n❌ no OTP picked (no candidate matched code/openai/verify keywords)")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
