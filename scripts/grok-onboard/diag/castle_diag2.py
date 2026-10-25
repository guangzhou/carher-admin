#!/usr/bin/env python3
"""castle_diag2.py -- round 2: WebGL flag + cookie-banner accept.

Round 1 evidence: no castle.io request ever fired, no castle globals, empty
castle_request_token; console full of "Automatic fallback to software WebGL
has been deprecated" => WebGL context creation FAILS under forced
--use-gl=angle --use-angle=swiftshader without --enable-unsafe-swiftshader.
Castle's SDK fingerprints WebGL first thing; if that throws, it dies before
loading its CDN script.  Round 2 adds the flag and a OneTrust accept click,
and timestamps every castle.io request against each step.
"""
import argparse, json, os, subprocess, time, urllib.parse

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
T0 = time.time()
def log(*m): print("[+%04.1fs]" % (time.time()-T0), *m, flush=True)

from patchright.sync_api import sync_playwright
castle_net, console = [], []

with sync_playwright() as p:
    br = p.chromium.launch(headless=False, proxy={"server": PROXY},
                           args=["--no-sandbox", "--disable-dev-shm-usage",
                                 "--use-gl=angle", "--use-angle=swiftshader",
                                 "--enable-unsafe-swiftshader"])
    ctx = br.new_context(locale="en-US", user_agent=UA,
                         viewport={"width": 1400, "height": 950})
    ctx.add_cookies([{"name": n, "value": sso, "domain": ".x.ai", "path": "/",
                      "secure": True, "httpOnly": True, "sameSite": "Lax"}
                     for n in ("sso", "sso-rw")])
    pg = ctx.new_page()
    pg.on("console", lambda m: console.append("%s: %s" % (m.type, m.text[:200])))
    pg.on("response", lambda r: castle_net.append(
        "[+%04.1fs] %s %s -> %s" % (time.time()-T0, r.request.method, r.url[:150], r.status))
        if any(k in r.url.lower() for k in ("castle",)) else None)

    def curl(url, post=None):
        cmd = ["curl", "-s", "--max-time", "30", url]
        if post: cmd += ["--data", post]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout
    d = json.loads(curl("https://auth.x.ai/oauth2/device/code", post=urllib.parse.urlencode(
        {"client_id": "b1a00492-073a-47ea-816f-4c329264a828",
         "scope": "openid profile email offline_access grok-cli:access api:access"})))
    log("user_code=%s" % d.get("user_code"))
    verify = d["verification_uri_complete"]

    pg.goto(verify, wait_until="domcontentloaded", timeout=60000)
    for step in range(40):
        time.sleep(3)
        url = pg.url.split("?")[0]
        try:
            state = pg.evaluate("""() => {
              const c = document.createElement('canvas');
              const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
              const i = document.querySelector(
                "input[name='castle_request_token'], #castle_request_token");
              const ob = document.getElementById('onetrust-consent-sdk');
              const acc = document.getElementById('onetrust-accept-btn-handler');
              return { webgl: gl ? String(gl.getParameter(gl.RENDERER)).slice(0,60) : 'NULL',
                       castle: i ? (i.value || '') : null,
                       banner: ob ? (ob.style.display !== 'none' && !!ob.offsetParent !== false ? 'visible-or-pending' : 'hidden') : 'absent',
                       acceptBtn: !!acc };
            }""")
        except Exception as e:
            state = {"err": str(e)[:80]}
        log("url=%s %s" % (url.split("x.ai")[-1][:50], state))
        pg.screenshot(path=os.path.join(a.out, "d%02d.png" % step))
        if url.endswith("/oauth2/device/done"):
            log("REACHED DONE ***")
            break
        if state.get("acceptBtn"):
            try:
                pg.click("#onetrust-accept-btn-handler", timeout=3000)
                log("clicked OneTrust Accept All")
                time.sleep(2)
            except Exception:
                pass
        if state.get("castle"):
            open(os.path.join(a.out, "castle_token.txt"), "w").write(state["castle"])
            log("CASTLE TOKEN len=%d" % len(state["castle"]))
        allow = pg.query_selector("button[value='allow']")
        if allow:
            allow.click(); log("clicked Allow"); time.sleep(1); continue
        for label in ("Continue", "Confirm", "Next"):
            b = pg.query_selector("button:has-text('%s')" % label)
            if b:
                b.click(); log("clicked %s" % label); break
        else:
            try: body = pg.inner_text("body")[:120].replace("\n", " ")
            except Exception: body = "?"
            if "could not verify" in body.lower():
                log("approve 403 page -- restarting chain")
                pg.goto(verify, wait_until="domcontentloaded", timeout=60000)
    open(os.path.join(a.out, "castle_net.log"), "w").write("\n".join(castle_net))
    open(os.path.join(a.out, "console.log"), "w").write("\n".join(console))
    log("castle_net requests: %d" % len(castle_net))
    for l in castle_net[:10]: log("  " + l)
    br.close()
