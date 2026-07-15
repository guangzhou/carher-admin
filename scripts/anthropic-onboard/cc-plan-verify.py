#!/usr/bin/env python3
"""
cc-plan-verify.py — 用 sessionKey cookie 注入登 claude.ai/settings 验证 plan 类型 + 多 buyer 检测。

WHY (2026-05-25):
  OAuth `sk-ant-oat` 直调 /v1/messages 对 Opus/Sonnet 一律返 429 (plan-agnostic
  allowlist),所以 API 响应**完全无法**区分 个人 Max / Team / Pro。唯一可靠的
  plan 验证是: 用 sk-ant-sid02- sessionKey cookie 注入 .claude.ai → 看 settings
  页面元素。

  详情见 [[claude-oauth-api-model-allowlist]] memory 和 [[anthropic-max-litellm]]
  skill §OAuth API model allowlist 章节。

USAGE:
  # 在本机 (会 ssh 到 188 跑 docker patchright):
  ./scripts/anthropic-onboard/cc-plan-verify.py acct-N

  # 输出:
  # - settings/billing 页 plan label (Max plan / Team plan / Pro / Free)
  # - 订阅渠道 (Android app / Stripe / Team admin)
  # - Account 页 Active sessions 列表 (检测多 buyer)
  # - 截图存在本地 /tmp/cc-plan-verify-acct-N/

INPUTS:
  - /Data/anthropic-auth/acct-N/.creds 必须有 session_key=sk-ant-sid02-... 字段
    (走 add-cc-account-sessionkey.sh 流程自动落)

EXIT CODE:
  0 = OK 拿到 plan label + active sessions
  1 = .creds 缺 session_key
  2 = patchright 跑挂 / 无法拿到页面
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
from pathlib import Path

SSH_188 = "cltx@10.68.13.188"
DOCKER_IMAGE = "mcr.microsoft.com/playwright/python:v1.60.0-noble"

PROBE_SCRIPT = r'''
import os, time, json, re
from patchright.sync_api import sync_playwright

SESSION_KEY = os.environ["SESSION_KEY"]
SS = "/work/screenshots"
os.makedirs(SS, exist_ok=True)


def wait_past_turnstile(page, max_wait=120):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        title = (page.title() or "").lower()
        body = page.content().lower()[:3000]
        if "just a moment" not in title and "performing security verification" not in body:
            return True
        for fr in page.frames:
            url = (fr.url or "").lower()
            if "challenges.cloudflare" in url or "turnstile" in url:
                try:
                    cb = fr.locator("input[type='checkbox']").first
                    if cb.count() > 0 and cb.is_visible():
                        print(f"  [turnstile] clicking", flush=True)
                        cb.click(force=True)
                        time.sleep(5)
                except Exception:
                    pass
        time.sleep(3)
    return False


def click_sidebar_tab(page, label):
    """点 claude.ai/settings 左侧导航 tab。SPA 直接 goto /settings/X 会 redirect 回
    /settings/general,必须用 in-page click。"""
    for sel in [
        f"nav a:has-text('{label}')",
        f"a[href*='/settings/'][role]:has-text('{label}')",
        f"a:has-text('{label}')",
    ]:
        try:
            cand = page.locator(sel)
            for i in range(cand.count()):
                el = cand.nth(i)
                if el.is_visible():
                    el.click()
                    return True
        except Exception:
            pass
    return False


with sync_playwright() as pw:
    br = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1366, "height": 900}, locale="en-US")
    expires = int(time.time()) + 30 * 86400
    ctx.add_cookies([{
        "name": "sessionKey", "value": SESSION_KEY, "domain": ".claude.ai",
        "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax",
        "expires": expires,
    }])

    page = ctx.new_page()
    result = {}

    # Step 1: 主入口 /settings (会被 SPA redirect 到 /settings/general)
    page.goto("https://claude.ai/settings", wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    if "moment" in (page.title() or "").lower():
        wait_past_turnstile(page, 120)
        time.sleep(5)

    # Step 2: 依次切 Billing / Account tab (in-page click 绕开 SPA URL redirect)
    for label in ["Billing", "Account"]:
        try:
            print(f"[{label}] clicking sidebar tab", flush=True)
            ok = click_sidebar_tab(page, label)
            if not ok:
                print(f"  ⚠️ click sidebar '{label}' failed", flush=True)
                result[label.lower()] = "ERROR: sidebar click failed"
                continue
            # 等 tab 主区域渲染 (text-based wait)
            wait_for_text = "Max plan" if label == "Billing" else "Log out of all devices"
            for _ in range(20):
                try:
                    if wait_for_text in page.inner_text("body"):
                        break
                except Exception:
                    pass
                time.sleep(1)
            time.sleep(2)
            page.screenshot(path=f"{SS}/{label.lower()}.png", full_page=True)
            body = page.inner_text("body")
            result[label.lower()] = body
            print(f"=== {label.lower()} ({page.url}) ===", flush=True)
            print(body[:3500], flush=True)
            print(f"=== /{label.lower()} ===\n", flush=True)
        except Exception as e:
            print(f"[{label}] err: {e}", flush=True)
            result[label.lower()] = f"ERROR: {e}"

    br.close()

    # Structured extraction
    summary = {"plan": None, "billing_via": None, "sessions": []}

    bill = result.get("billing", "")
    # 区分: plan label 在 nav 里和主区域都有 "Max plan", 要看主区域是否伴随配额描述
    if "20x more usage than Pro" in bill:
        summary["plan"] = "Max 20x ($200/月 个人)"
    elif "5x more usage than Pro" in bill:
        summary["plan"] = "Max 5x ($100/月 个人)"
    elif "Team plan" in bill or "Team Premium" in bill or "workspace" in bill.lower():
        summary["plan"] = "Team plan"
    elif ("Pro plan" in bill) or ("Subscribed" in bill and "Pro" in bill and "20x" not in bill and "5x" not in bill):
        summary["plan"] = "Pro ($17-20/月 个人)"
    elif "Upgrade to" in bill or "Free plan" in bill:
        summary["plan"] = "Free (无订阅, 残废号)"
    else:
        summary["plan"] = "UNKNOWN (看 billing.png)"

    if "Subscribed via Android app" in bill:
        summary["billing_via"] = "Android (Google Play)"
    elif "Subscribed via Apple" in bill or "App Store" in bill:
        summary["billing_via"] = "iOS (App Store)"
    elif "View invoices" in bill or "Manage subscription" in bill:
        summary["billing_via"] = "Stripe / 网页直购"

    # Active sessions parsing (Account 页)
    acc = result.get("account", "")
    m = re.search(r"Active sessions(.+?)(?:Get apps|$)", acc, re.DOTALL)
    if m:
        block = m.group(1)
        # 实测格式: "\nSafari (Mac OS X)\nCurrent\n\tChłmniec, Lesser Poland, PL\tMay 21, 2026, 8:31 AM\tMay 25, 2026, 8:51 AM\t"
        # device 行 + (optional Current) + (tab|newline) + location/created/updated 行
        for line_m in re.finditer(
            r"\n((?:Safari|Chrome|Firefox|Edge|Opera)[^\n]*(?:Mac OS X|Windows|iPhone|Android|Linux|iOS)[^\n]*?)(?:\nCurrent)?\n[\s\t]*([^\n\t]+?)\t(\w+ \d+,\s*\d{4}[^\t]*?)\t(\w+ \d+,\s*\d{4}[^\t]*)",
            block,
        ):
            summary["sessions"].append({
                "device": line_m.group(1).strip(),
                "location": line_m.group(2).strip(),
                "created": line_m.group(3).strip(),
                "updated": line_m.group(4).strip(),
            })

    print("=== SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    print("=== /SUMMARY ===", flush=True)
'''


def main() -> int:
    if len(sys.argv) != 2 or not re.match(r"^acct-\d+$", sys.argv[1]):
        print(f"用法: {sys.argv[0]} acct-N", file=sys.stderr)
        return 1
    acct = sys.argv[1]

    # 1) 读 session_key 从 .creds (188 上)
    print(f"==[1/3]== 读 /Data/anthropic-auth/{acct}/.creds 里的 session_key")
    out = subprocess.run(
        ["ssh", SSH_188, f"grep ^session_key= /Data/anthropic-auth/{acct}/.creds | cut -d= -f2-"],
        capture_output=True, text=True,
    )
    session_key = out.stdout.strip()
    if not session_key or not session_key.startswith("sk-ant-sid02-"):
        print(f"❌ acct={acct} .creds 缺 session_key= 或不是 sk-ant-sid02- 前缀", file=sys.stderr)
        print(f"   实际拿到: {session_key[:60]!r}", file=sys.stderr)
        return 1
    print(f"  ✅ session_key 长度={len(session_key)}")

    # 2) 在 188 跑 docker patchright + probe + 截图
    print(f"==[2/3]== 188 跑 docker patchright 注入 cookie + 截 settings/* (~1-2min)")
    probe_remote = f"/tmp/cc-plan-probe-{acct}.py"
    ss_remote = f"/tmp/cc-plan-verify-{acct}"

    subprocess.run(
        ["ssh", SSH_188, f"cat > {probe_remote} << 'PY_END'\n{PROBE_SCRIPT}\nPY_END"],
        check=True,
    )

    docker_cmd = f"""
        rm -rf {ss_remote} && mkdir -p {ss_remote}
        docker run --rm \
          -v {probe_remote}:/work/script.py:ro \
          -v {ss_remote}:/work/screenshots \
          -e SESSION_KEY='{session_key}' \
          -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright -e DISPLAY=:99 \
          {DOCKER_IMAGE} \
          bash -c 'Xvfb :99 -screen 0 1366x900x24 >/dev/null 2>&1 & sleep 1 && \
                   pip install patchright==1.60.0 -q --root-user-action=ignore 2>&1 | tail -1 && \
                   python3 /work/script.py' 2>&1 | tee /tmp/cc-plan-verify-{acct}-runner.log
    """
    proc = subprocess.run(["ssh", SSH_188, docker_cmd], capture_output=True, text=True)
    stdout = proc.stdout
    print(stdout)
    if "=== SUMMARY ===" not in stdout:
        print(f"❌ probe 未到 SUMMARY,可能 Turnstile 没过或 sessionKey 失效", file=sys.stderr)
        print(f"   截图位置: ssh {SSH_188} ls {ss_remote}", file=sys.stderr)
        return 2

    # 3) scp 截图回本地
    print(f"==[3/3]== scp 截图回本地 /tmp/cc-plan-verify-{acct}/")
    local_ss = Path(f"/tmp/cc-plan-verify-{acct}")
    local_ss.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["scp", "-q", f"{SSH_188}:{ss_remote}/*.png", str(local_ss) + "/"],
        check=False,
    )
    print(f"  ✅ 截图:")
    for f in sorted(local_ss.glob("*.png")):
        print(f"    {f} ({f.stat().st_size} bytes)")
    print()
    print(f"🎉 plan 验证完成, 详细看 stdout SUMMARY 块 + 截图")
    return 0


if __name__ == "__main__":
    sys.exit(main())
