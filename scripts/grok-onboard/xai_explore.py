#!/usr/bin/env python3
"""xai_explore.py — CF-clean recon of accounts.x.ai device-auth + login flow.
Runs inside the aliyun patchright/Xvfb job. Generates its own device code,
opens the verification page, and dumps DOM (text) at each step so we can
design the real completer. Enters email if provided to reveal step 2.
"""
import json, os, time, urllib.request, urllib.parse, urllib.error
from patchright.sync_api import sync_playwright

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
EMAIL = os.environ.get("MAIL_USER", "")
SS = "/work/ss-grok"
os.makedirs(SS, exist_ok=True)

def dc():
    data = urllib.parse.urlencode({"client_id": CLIENT_ID,
        "scope": "openid profile email offline_access grok-cli:access api:access"}).encode()
    req = urllib.request.Request("https://auth.x.ai/oauth2/device/code", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())

def dump(pg, tag):
    print(f"\n===== DUMP {tag} =====", flush=True)
    print("[url]", pg.url, flush=True)
    print("[title]", pg.title(), flush=True)
    try:
        print("[text]\n", pg.evaluate("()=>document.body.innerText")[:2000], flush=True)
    except Exception as e:
        print("[text-err]", e, flush=True)
    for sel in ["input", "button", "a"]:
        for e in pg.query_selector_all(sel)[:20]:
            try:
                t = (e.inner_text() or "").strip()
                ph = e.get_attribute("placeholder") or ""
                nm = e.get_attribute("name") or ""
                ty = e.get_attribute("type") or ""
                idv = e.get_attribute("id") or ""
                dt = e.get_attribute("data-testid") or ""
                if t or ph or nm or ty or dt:
                    print(f"  <{sel}> text={t[:40]!r} ph={ph!r} name={nm!r} type={ty!r} id={idv!r} testid={dt!r}", flush=True)
            except Exception:
                pass
    try:
        pg.screenshot(path=f"{SS}/{tag}.png", full_page=True)
    except Exception as e:
        print("[shot-err]", e, flush=True)

def main():
    d = dc()
    print("USER_CODE=%s  verify=%s  exp=%s" % (d["user_code"], d["verification_uri_complete"], d["expires_in"]), flush=True)
    with sync_playwright() as p:
        br = p.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = br.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
        pg = ctx.new_page()
        try:
            pg.goto("https://ipinfo.io/json", wait_until="domcontentloaded", timeout=45000)
            print("[egress]", pg.inner_text("body")[:200], flush=True)
        except Exception as e:
            print("[egress-err]", e, flush=True)
        pg.goto(d["verification_uri_complete"], wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(4000)
        dump(pg, "01-device")
        # Try to advance: look for an email/login entry
        if EMAIL:
            try:
                # common: an email input or a "sign in with email" button
                filled = False
                for q in ["input[type=email]", "input[name=email]", "input[placeholder*=mail i]", "input[type=text]"]:
                    loc = pg.query_selector(q)
                    if loc:
                        loc.fill(EMAIL); filled = True
                        print("[email-filled via]", q, flush=True); break
                if not filled:
                    # maybe need to click a provider/continue button first
                    for bt in ["Continue", "Sign in", "Log in", "Email"]:
                        b = pg.query_selector(f"button:has-text('{bt}'), a:has-text('{bt}')")
                        if b:
                            b.click(); pg.wait_for_timeout(3000)
                            print("[clicked]", bt, flush=True)
                            dump(pg, f"02-after-{bt}")
                            for q in ["input[type=email]", "input[name=email]", "input[type=text]"]:
                                loc = pg.query_selector(q)
                                if loc:
                                    loc.fill(EMAIL); filled = True
                                    print("[email-filled via]", q, flush=True); break
                            if filled: break
                if filled:
                    for bt in ["Continue", "Next", "Sign in", "Log in", "Submit"]:
                        b = pg.query_selector(f"button:has-text('{bt}')")
                        if b:
                            b.click(); print("[submit]", bt, flush=True); break
                    pg.wait_for_timeout(5000)
                    dump(pg, "03-after-email")
            except Exception as e:
                print("[email-step-err]", e, flush=True)
        print("EXPLORE_DONE", flush=True)
        br.close()

if __name__ == "__main__":
    main()
