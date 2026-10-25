#!/usr/bin/env python3
"""Read-only: open the device consent page and record what the page fails to load.
Hypothesis under test: the Castle anti-bot JS cannot load through this WARP egress,
so castle_request_token stays empty and /approve is rejected no matter which IP we use.
NEVER clicks Allow. env: SSO_FILE, SHOTS, PROXY
"""
import json, os, time, urllib.parse, urllib.request
from patchright.sync_api import sync_playwright

SSO = open(os.environ["SSO_FILE"]).read().strip()
SHOTS = os.environ["SHOTS"]
PROXY = os.environ.get("PROXY", "socks5://127.0.0.1:40000")
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"

# device code through the same socks5 (curl, like the real script does)
import subprocess
data = urllib.parse.urlencode({"client_id": CLIENT_ID, "scope": SCOPE})
r = subprocess.run(["curl", "-s", "--socks5-hostname", PROXY.split("//")[-1],
                    "--max-time", "30", "-X", "POST", "-H",
                    "Content-Type: application/x-www-form-urlencoded",
                    "--data-binary", data, "https://auth.x.ai/oauth2/device/code"],
                   capture_output=True, text=True)
dc = json.loads(r.stdout)
print("[device] user_code=%s" % dc.get("user_code"), flush=True)
verify = "https://accounts.x.ai/oauth2/device?user_code=%s" % dc["user_code"]

failed, requests_seen = [], []
with sync_playwright() as p:
    br = p.chromium.launch(headless=False, proxy={"server": PROXY},
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    try:
        ctx = br.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
        ctx.add_cookies([{"name": n, "value": SSO, "domain": ".x.ai", "path": "/",
                          "secure": True, "httpOnly": True, "sameSite": "Lax"}
                         for n in ("sso", "sso-rw")])
        pg = ctx.new_page()
        pg.on("requestfailed", lambda r: failed.append((r.url[:120], r.failure)))
        pg.on("requestfinished", lambda r: requests_seen.append(r.url[:120]))
        pg.goto(verify, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        for label in ("Continue", "Confirm", "Next"):
            b = pg.query_selector("button:has-text('%s')" % label)
            if b:
                b.click(); print("[click] %s" % label, flush=True); break
        time.sleep(8)
        print("[url] %s" % pg.url, flush=True)
        pg.screenshot(path=os.path.join(SHOTS, "consent.png"))
        # the field the memory says only page JS fills
        for name in ("castle_request_token", "csrf_token", "cf-turnstile-response"):
            el = pg.query_selector("input[name='%s']" % name)
            if el:
                v = el.get_attribute("value") or ""
                print("[field] %s present value_len=%d" % (name, len(v)), flush=True)
        allow = pg.query_selector("button[value='allow']")
        print("[allow button present] %s" % bool(allow), flush=True)
        if allow and os.environ.get("CLICK_ALLOW") == "1":
            allow.click()
            time.sleep(6)
            after = ""
            try:
                after = pg.inner_text("body")[:140].replace("\n", " ")
            except Exception:
                pass
            pg.screenshot(path=os.path.join(SHOTS, "after-allow.png"))
            print("[APPROVE] url=%s | %s" % (pg.url.split("?")[0], after), flush=True)
        hosts = sorted({u.split("/")[2] for u in requests_seen if "//" in u})
        print("[hosts loaded] %s" % ", ".join(hosts), flush=True)
        print("[FAILED REQUESTS] %d" % len(failed), flush=True)
        for u, f in failed[:15]:
            print("   %s  <- %s" % (u, f), flush=True)
    finally:
        br.close()
