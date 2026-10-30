#!/usr/bin/env python3
"""Read muse.ai cookies from a logged-in CDP browser and import them into muse2api.

Usage: import_cookies.py <browser-cdp-ws-url> <muse2api-base> <label>
The muse2api key is read from env MUSE2API_KEY. Cookie values are never printed.
Normally called through add_acct.py; run it directly only to re-import an account by hand.
"""
import asyncio
import json
import os
import sys
import urllib.request

import websockets  # type: ignore

# All four are HttpOnly, so only CDP can read them. hatch_gw / hatch_vml appear only after
# a thread page has connected to the cloud VM (open muse.ai/thread/new and wait ~25s first).
ESSENTIAL = ("hatch_sess", "hatch_gw", "hatch_vml", "hatch_native_auth_device")


async def _read_cookies(cdp_ws):
    async with websockets.connect(cdp_ws, max_size=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                return msg["result"]["cookies"]


def collect(cdp_ws):
    """Return (jar, expires, missing) for the muse.ai cookies in the browser behind cdp_ws."""
    cookies = [c for c in asyncio.run(_read_cookies(cdp_ws)) if "muse.ai" in c.get("domain", "")]
    jar = {c["name"]: c["value"] for c in cookies}
    expires = {c["name"]: int(c["expires"]) for c in cookies if c.get("expires", -1) > 0}
    return jar, expires, [n for n in ESSENTIAL if n not in jar]


def main():
    cdp_ws, base, label = sys.argv[1], sys.argv[2].rstrip("/"), sys.argv[3]
    key = os.environ["MUSE2API_KEY"]
    jar, expires, missing = collect(cdp_ws)
    print(f"muse.ai cookies: {len(jar)}, essential missing: {missing or 'none'}")
    if missing:
        sys.exit(2)
    body = json.dumps({"label": label, "cookies": jar, "expires": expires}).encode()
    req = urllib.request.Request(
        f"{base}/admin/accounts", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    print(json.dumps({k: res.get(k) for k in ("count", "essential_missing", "warning")}, ensure_ascii=False))
    for a in res.get("added", []):
        print("added", a.get("id"), a.get("label"), "cookies", a.get("cookie_count"))


if __name__ == "__main__":
    main()
