#!/usr/bin/env python3
"""castle_diag.py -- why does Castle never fill castle_request_token in the
patchright browser?  One instrumented run, everything kept for reading.

Mirrors browser_approve()'s launch exactly (same args, same proxy, same
cookies) and adds: console log, failed requests, all castle-related network,
consent-page script inventory + castle globals, 60s of castle_value sampling,
then clicks Allow anyway and keeps the approve-page HTML + screenshots.

Usage (225): DISPLAY=:80 PYTHONPATH=... PLAYWRIGHT_BROWSERS_PATH=... \
  python3 castle_diag.py --sso-file <0600 file> --out <dir>
"""
import argparse, json, os, sys, time

PROXY = "socks5://127.0.0.1:40000"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

ap = argparse.ArgumentParser()
ap.add_argument("--sso-file", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
os.chmod(a.out, 0o700)
sso = open(a.sso_file).read().strip()

def log(*m): print(*m, flush=True)

from patchright.sync_api import sync_playwright
console, failures, castle_net = [], [], []

with sync_playwright() as p:
    br = p.chromium.launch(headless=False, proxy={"server": PROXY},
                           args=["--no-sandbox", "--disable-dev-shm-usage",
                                 "--use-gl=angle", "--use-angle=swiftshader"])
    ctx = br.new_context(locale="en-US", user_agent=UA,
                         viewport={"width": 1400, "height": 950})
    ctx.add_cookies([{"name": n, "value": sso, "domain": ".x.ai", "path": "/",
                      "secure": True, "httpOnly": True, "sameSite": "Lax"}
                     for n in ("sso", "sso-rw")])
    pg = ctx.new_page()
    pg.on("console", lambda m: console.append("%s: %s" % (m.type, m.text[:300])))
    pg.on("requestfailed", lambda r: failures.append("%s %s :: %s"
          % (r.method, r.url[:160], r.failure)))
    def on_resp(r):
        u = r.url
        if any(k in u.lower() for k in ("castle", "fraud", "antibot", "sentinel")):
            castle_net.append("%s %s -> %s" % (r.request.method, u[:200], r.status))
    pg.on("response", on_resp)

    # device code the same way the real flow gets one: start at the device page
    # WITHOUT a user_code first? No -- we need a real user_code from auth.x.ai.
    # Do the HTTP device-code start exactly like device_code() does.
    import subprocess, urllib.parse
    def curl(url, post=None, headers=None):
        cmd = ["curl", "-s", "--max-time", "30", "-D", "/dev/stderr", "-o",
               "/dev/stdout", url]
        if headers: cmd += headers
        if post: cmd += ["--data", post]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout, r.stderr
    st_body, hdrs = curl("https://auth.x.ai/oauth2/device/code", post=urllib.parse.urlencode(
        {"client_id": "b1a00492-073a-47ea-816f-4c329264a828",
         "scope": "openid profile email offline_access grok-cli:access api:access"}))
    d = json.loads(st_body)
    log("user_code=%s" % d.get("user_code"))
    verify = d["verification_uri_complete"]
    open(os.path.join(a.out, "user_code.txt"), "w").write(d.get("user_code", ""))

    pg.goto(verify, wait_until="domcontentloaded", timeout=60000)
    for step in range(30):          # up to ~4 min of watching
        time.sleep(4)
        url = pg.url.split("?")[0]
        pg.screenshot(path=os.path.join(a.out, "d%02d.png" % step))
        cv = None
        try:
            cv = pg.evaluate("() => { const i = document.querySelector("
                "\"input[name='castle_request_token'], #castle_request_token\");"
                " return i ? (i.value || '') : null; }")
        except Exception as e:
            cv = "ERR:%s" % str(e)[:60]
        log("t+%02ds url=%s castle=%r" % ((step+1)*4, url, cv))
        if url.endswith("/oauth2/device/done"):
            log("REACHED DONE")
            break
        if cv:                       # token appeared
            open(os.path.join(a.out, "castle_token.txt"), "w").write(cv)
            log("CASTLE TOKEN APPEARED len=%d" % len(cv))
        allow = pg.query_selector("button[value='allow']")
        if allow:
            # consent page: inventory once
            inv = pg.evaluate("""() => ({
                scripts: [...document.querySelectorAll('script[src]')].map(s=>s.src),
                castleGlobals: Object.keys(window).filter(k=>/castle|fraud|sentinel/i.test(k)),
                inputs: [...document.querySelectorAll('input')].map(i=>({n:i.name,t:i.type,v:(i.value||'').length})),
                iframes: [...document.querySelectorAll('iframe')].map(f=>f.src),
            })""")
            open(os.path.join(a.out, "consent_inventory.json"), "w").write(json.dumps(inv, indent=1))
            log("inventory: scripts=%d castleGlobals=%s iframes=%s"
                % (len(inv["scripts"]), inv["castleGlobals"], inv["iframes"]))
            log("script srcs: %s" % inv["scripts"])
            html = pg.content()
            open(os.path.join(a.out, "consent.html"), "w").write(html)
            allow.click()
            log("clicked Allow")
            continue
        for label in ("Continue", "Confirm", "Next"):
            b = pg.query_selector("button:has-text('%s')" % label)
            if b:
                b.click(); log("clicked %s" % label); break
        else:
            try: body = pg.inner_text("body")[:200].replace("\n", " ")
            except Exception: body = "?"
            log("no button: %s" % body)
            if "could not verify" in body.lower():
                open(os.path.join(a.out, "approve_403.html"), "w").write(pg.content())
    open(os.path.join(a.out, "console.log"), "w").write("\n".join(console))
    open(os.path.join(a.out, "failures.log"), "w").write("\n".join(failures))
    open(os.path.join(a.out, "castle_net.log"), "w").write("\n".join(castle_net))
    log("\nconsole lines=%d failures=%d castle_net=%s"
        % (len(console), len(failures), castle_net or "-"))
    br.close()
log("OUT kept at %s" % a.out)
