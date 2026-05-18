#!/usr/bin/env python3
"""ChatGPT Pro OAuth device-code 自动绑定。

输入：ACCT 名（acct-N）+ /run/secrets.yaml（解密后的明文）+ 已经从外部
拿到的 user_code / verification_uri（litellm 容器侧已触发 device code）。

流程：
1. headless chromium 打开 verification_uri
2. 填 user_code → 登录 email + password
3. 若弹邮箱 OTP：IMAP 轮询 mail.com 收件箱 ≤90s 取 6 位码 → 回填
4. 等 OAuth 跳转完成（DOM 出现 "You may now return to your terminal"）

不写 auth.json —— 那是 litellm 容器侧 device-code poll 的责任。
本脚本只负责"让 device code 通过 OpenAI 浏览器侧验证"。
"""
from __future__ import annotations

import argparse
import email
import imaplib
import os
import re
import sys
import time
from pathlib import Path

import yaml
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

OTP_RE = re.compile(r"\b(\d{6})\b")
OTP_FROM_HINT = ("openai", "noreply", "auth")
SCREENSHOT_DIR = Path("/work/screenshots")


def log(msg: str) -> None:
    print(f"[onboard] {msg}", flush=True)


def load_secrets(path: str, acct: str) -> dict:
    with open(path) as f:
        all_secrets = yaml.safe_load(f)
    if acct not in all_secrets:
        sys.exit(f"ERROR: {acct} not in {path}")
    s = all_secrets[acct]
    required = ("openai_email", "openai_password",
                "mail_imap_host", "mail_imap_user", "mail_imap_password")
    missing = [k for k in required if not s.get(k)]
    if missing:
        sys.exit(f"ERROR: {acct} missing fields: {missing}")
    return s


def fetch_otp_from_imap(s: dict, since_ts: float, max_wait: int = 90) -> str:
    """轮询 IMAP，从 OpenAI 发来的邮件里取 6 位 OTP。"""
    deadline = time.time() + max_wait
    last_uid_seen = b"0"
    while time.time() < deadline:
        try:
            m = imaplib.IMAP4_SSL(s["mail_imap_host"], s.get("mail_imap_port", 993))
            m.login(s["mail_imap_user"], s["mail_imap_password"])
            m.select("INBOX")
            typ, data = m.uid("search", None, "ALL")
            if typ != "OK":
                m.logout()
                time.sleep(3)
                continue
            uids = data[0].split()
            for uid in reversed(uids[-30:]):
                if uid <= last_uid_seen:
                    continue
                typ, msg_data = m.uid("fetch", uid, "(RFC822)")
                if typ != "OK":
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                from_ = (msg.get("From") or "").lower()
                if not any(h in from_ for h in OTP_FROM_HINT):
                    continue
                date_hdr = email.utils.parsedate_to_datetime(msg.get("Date"))
                if date_hdr.timestamp() < since_ts - 30:
                    continue
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode(errors="ignore")
                else:
                    body = msg.get_payload(decode=True).decode(errors="ignore")
                m_otp = OTP_RE.search(body)
                if m_otp:
                    code = m_otp.group(1)
                    log(f"OTP captured from {from_}: {code}")
                    m.logout()
                    return code
            m.logout()
        except Exception as e:
            log(f"IMAP poll error (will retry): {e}")
        time.sleep(5)
    sys.exit("ERROR: OTP not received within timeout")


def screenshot(page: Page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    log(f"screenshot: {path}")


def fill_user_code(page: Page, user_code: str) -> None:
    log(f"step 1: fill user_code={user_code}")
    inp = page.wait_for_selector("input", timeout=15000)
    inp.fill(user_code)
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")
    screenshot(page, "01-after-code")


def fill_email_password(page: Page, email_addr: str, password: str) -> None:
    log("step 2: fill email")
    page.wait_for_selector("input[type='email'], input[name='email'], input[autocomplete='email']", timeout=15000)
    page.fill("input[type='email'], input[name='email'], input[autocomplete='email']", email_addr)
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    screenshot(page, "02-after-email")

    log("step 3: fill password")
    page.wait_for_selector("input[type='password']", timeout=15000)
    page.fill("input[type='password']", password)
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")
    time.sleep(3)
    screenshot(page, "03-after-password")


def maybe_fill_otp(page: Page, secrets: dict, since_ts: float) -> None:
    url = page.url.lower()
    content = page.content().lower()
    if not any(t in url + content for t in ("verify", "code", "verification", "one-time")):
        log("step 4: no OTP screen detected")
        return
    log("step 4: OTP screen detected, fetching from IMAP")
    code = fetch_otp_from_imap(secrets, since_ts)
    inputs = page.query_selector_all("input")
    if len(inputs) >= 6:
        for i, ch in enumerate(code):
            inputs[i].fill(ch)
    elif inputs:
        inputs[0].fill(code)
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")
    time.sleep(3)
    screenshot(page, "04-after-otp")


def wait_completion(page: Page) -> None:
    log("step 5: waiting for OAuth completion")
    deadline = time.time() + 60
    while time.time() < deadline:
        body = page.content().lower()
        if "may now return" in body or "device authorized" in body or "you can close" in body:
            screenshot(page, "05-success")
            log("OAuth flow finished — litellm container will write auth.json")
            return
        time.sleep(2)
    screenshot(page, "05-timeout")
    sys.exit("ERROR: did not see completion screen within 60s")


def run(acct: str, user_code: str, verify_url: str, secrets_path: str, headless: bool) -> None:
    secrets = load_secrets(secrets_path, acct)
    since_ts = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(verify_url, wait_until="networkidle")
            screenshot(page, "00-landing")
            fill_user_code(page, user_code)
            fill_email_password(page, secrets["openai_email"], secrets["openai_password"])
            maybe_fill_otp(page, secrets, since_ts)
            wait_completion(page)
        finally:
            browser.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--acct", required=True, help="acct-N")
    p.add_argument("--user-code", required=True, help="user_code from litellm device flow")
    p.add_argument("--verify-url", default="https://auth.openai.com/codex/device")
    p.add_argument("--secrets", default="/run/secrets.yaml")
    p.add_argument("--headed", action="store_true", help="run with visible browser (debug)")
    args = p.parse_args()
    run(args.acct, args.user_code, args.verify_url, args.secrets, headless=not args.headed)


if __name__ == "__main__":
    main()
