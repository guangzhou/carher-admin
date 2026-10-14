"""Log in to gemini.google.com (same flow as google_login.py) and dump what the account's
Tools menu offers (Create music / Videos / Deep Research / Canvas ...) plus any limit notices.
Read-only: never sends a prompt. Runs on 188 under Xvfb + patchright."""
import json
import sys
import time

from patchright.sync_api import sync_playwright

sys.path.insert(0, "/work")
import google_login as gl  # noqa: E402

NAME = sys.argv[1]
gl.NAME = NAME
gl.ACCT = json.load(open(f"/acct/{NAME}.json"))
gl.OUT = f"/out/{NAME}.tools"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.goto("https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp&hl=en")
        page.locator('#identifierId, input[type="email"]').first.fill(gl.ACCT["email"]); page.keyboard.press("Enter")
        page.wait_for_selector('input[type="password"]:visible', timeout=30000)
        page.fill('input[type="password"]:visible', gl.ACCT["password"]); page.keyboard.press("Enter")
        deadline = time.time() + 90
        while time.time() < deadline and not page.url.startswith("https://gemini.google.com/"):
            page.wait_for_timeout(2500)
            if page.locator('input[name="totpPin"]').count():
                page.locator('input[name="totpPin"]').first.fill(gl.totp(gl.ACCT["totp_secret"])); page.keyboard.press("Enter")
                page.wait_for_timeout(4000)
            for label in ("Not now", "Skip"):
                b = page.get_by_role("button", name=label)
                if b.count():
                    b.first.click()
        page.wait_for_timeout(6000)
        print("URL", page.url, flush=True)
        for label in ("Tools", "Open tools", "Toolbox", "Upload and tools"):
            b = page.get_by_role("button", name=label)
            if b.count():
                b.first.click(); page.wait_for_timeout(2500); print("opened menu via", label); break
        page.screenshot(path=f"/out/{NAME}.tools-menu.png")
        items = page.locator('[role="menuitem"], [role="menuitemcheckbox"], [role="option"]').all_inner_texts()
        print("MENU", json.dumps([i.replace("\n", " | ")[:80] for i in items], ensure_ascii=False))
        print("PAGE", gl.visible_text(page, 900))
        browser.close()


main()
