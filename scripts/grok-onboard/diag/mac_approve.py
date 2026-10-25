#!/usr/bin/env python3
"""mac_approve.py -- run the xAI device capture on this Mac instead of 225.

Why: 225's Xvfb + patchright browser never gets a Castle request token (two
instrumented rounds: castle SDK is bundled and enabled -- enable_castle:true,
castlePk present -- yet never issues a token, and no cdn.castle.io request is
ever made; WebGL fix did not change it).  That is a fingerprint-level refusal,
so take the approve to the least bot-looking host we have: real display, real
Chrome (channel=chrome, real GPU), macOS fonts/canvas.  Egress still has to be
WARP -- this Mac has no clean path to x.ai -- so proxy through an ssh -L
tunnel to 225's socks5 by default.

Reuses xai_sso_device_oauth.py for every check (sso, subscription, device
code, token poll, x.ai verdict, bundle shape 0600) and replaces only the
browser approve and the api.x.ai probe's transport (this Mac needs the socks
tunnel for api.x.ai too, where 225 reached it directly).

Usage:
  ssh -L 40000:127.0.0.1:40000 cltx@10.68.13.225 -N -f -o ExitOnForwardFailure=yes
  python3 mac_approve.py --email <e> --sso-file <0600 file> --out <bundle.json> [--shots dir]
"""
import argparse, importlib.util, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)

ap = argparse.ArgumentParser()
ap.add_argument("--email", required=True)
ap.add_argument("--sso-file", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--shots", default=None)
ap.add_argument("--proxy", default="socks5://127.0.0.1:40000")
a = ap.parse_args()

spec = importlib.util.spec_from_file_location(
    "cap", os.path.join(PARENT, "xai_sso_device_oauth.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.PROXY = a.proxy          # _curl reads it at call time

def log(*m): print(*m, flush=True)

CASTLE_JS = ("() => { const i = document.querySelector("
             "\"input[name='castle_request_token'], #castle_request_token\");"
             " return i ? (i.value || '') : null; }")


def click_retry(pg, selector, tries=3):
    """Re-locating click: React re-renders detach the ElementHandle between
    query_selector and click (run 1 died exactly there)."""
    last = None
    for i in range(tries):
        try:
            pg.click(selector, timeout=5000)
            return True
        except Exception as e:
            last = e
            time.sleep(1)
    raise last

def browser_approve_mac(sso, verify_url, shots_dir=None):
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            br = p.chromium.launch(channel="chrome", headless=False,
                                   proxy={"server": a.proxy})
            log("  [browser] real Chrome (channel=chrome)")
        except Exception as e:
            log("  [browser] channel=chrome unavailable (%s) -- bundled chromium"
                % str(e)[:80])
            br = p.chromium.launch(headless=False, proxy={"server": a.proxy})
        try:
            ctx = br.new_context(locale="en-US",
                                 viewport={"width": 1400, "height": 950})
            ctx.add_cookies([{"name": n, "value": sso, "domain": ".x.ai",
                              "path": "/", "secure": True, "httpOnly": True,
                              "sameSite": "Lax"} for n in ("sso", "sso-rw")])
            pg = ctx.new_page()
            pg.goto(verify_url, wait_until="domcontentloaded", timeout=60000)
            for step in range(12):
                time.sleep(4)
                url = pg.url.split("?")[0]
                if shots_dir:
                    pg.screenshot(path=os.path.join(shots_dir, "approve-%d.png" % step))
                if url.endswith("/oauth2/device/done"):
                    log("  [browser] %s -- device authorized" % url)
                    return 200
                allow = pg.query_selector("button[value='allow']")
                if allow:
                    cv = ""
                    for _ in range(20):
                        try:
                            cv = pg.evaluate(CASTLE_JS) or ""
                        except Exception:
                            cv = ""
                        if cv:
                            break
                        time.sleep(2)
                    log("  [browser] consent: castle token %s"
                        % ("filled (%d chars)" % len(cv) if cv
                           else "STILL EMPTY after 40s"))
                    click_retry(pg, "button[value='allow']")
                    log("  [browser] %s -> clicked Allow" % url)
                    continue
                for label in ("Continue", "Confirm", "Next"):
                    btn = pg.query_selector("button:has-text('%s')" % label)
                    if btn:
                        click_retry(pg, "button:has-text('%s')" % label)
                        log("  [browser] %s -> clicked %s" % (url, label))
                        break
                else:
                    try:
                        body = pg.inner_text("body")[:140].replace("\n", " ")
                    except Exception:
                        body = "?"
                    log("  [browser] %s -- %s" % (url, body))
                    if "could not verify" in body.lower() or \
                            url.endswith("/oauth2/device/approve"):
                        pg.goto(verify_url, wait_until="domcontentloaded",
                                timeout=60000)
            raise SystemExit("browser approve never reached /device/done "
                             "(last url %s)" % pg.url)
        finally:
            br.close()

def xai_once_mac(access_token):
    """Same ruler as the module's _xai_models_once, through the socks tunnel
    (this Mac cannot reach api.x.ai directly)."""
    import json as _json, subprocess
    r = subprocess.run(
        ["curl", "-s", "--max-time", "30", "--socks5-hostname",
         a.proxy.split("//")[-1], "-o", "/dev/stdout", "-w", "\n%{http_code}",
         "https://api.x.ai/v1/models",
         "-H", "Authorization: Bearer " + access_token],
        capture_output=True, text=True)
    parts = r.stdout.rsplit("\n", 1)
    body, st = (parts[0], parts[-1]) if len(parts) == 2 else ("", "?")
    if st == "200":
        try:
            return "200 usable (%d models)" % len(_json.loads(body)["data"]), True
        except Exception:
            return "200 usable", True
    try:
        return "%s %s" % (st, _json.loads(body).get("code") or body[:80]), False
    except Exception:
        return "%s %s" % (st, body[:80]), False

mod.browser_approve = browser_approve_mac
mod._xai_models_once = xai_once_mac

if a.shots:
    os.makedirs(a.shots, mode=0o700, exist_ok=True)
sso = open(a.sso_file).read().strip()
ok = mod.one(a.email, sso, a.out, approve_mode="browser", shots_dir=a.shots)
log("bundle=%s ok=%s" % (a.out, bool(ok)))
sys.exit(0 if ok else 1)
