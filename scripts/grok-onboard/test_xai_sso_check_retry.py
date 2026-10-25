#!/usr/bin/env python3
"""Cases for check_sso's retry gate + the castle_request_token warning (2026-10-13).

Why: check_sso is a GATE -- "SSO DEAD" drops the account before a device code
is spent.  On 2026-10-13 grok.com/rest/auth/get-user answered
`499 {"message":"Timeout expired"}` to a good sso (and identically to a FAKE
one), and two orchestrator runs dropped what became acct 75.  So both faces:

  - transient 499 / 5xx / curl failure, then 200 -> ALIVE
  - a real 401/403 -> DEAD at once (no retry: it IS an answer about the cookie)
  - 499 forever -> not alive, and the error must say TRANSIENT, never just dead

Run: python3 test_xai_sso_check_retry.py   (no network)
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OK = (200, "", json.dumps({"userId": "u1", "email": "a@b.c"}))
T499 = (499, "", '{"code":1, "message":"Timeout expired", "details":[]}')
CF403 = (403, "", "<!DOCTYPE html><html><head><title>Attention Required! | "
                  "Cloudflare</title></head><body>Sorry, you have been "
                  "blocked</body></html>")


def load():
    spec = importlib.util.spec_from_file_location(
        "cap", os.path.join(HERE, "xai_sso_device_oauth.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.time.sleep = lambda *_: None
    m.log = lambda *a: None
    m.rotate_warp = lambda *a, **k: True  # no shelling out to warp-cli offline
    return m


def run():
    m = load()
    fails = []

    def case(name, replies, want_alive, want_in=None, want_calls=None):
        calls = []

        def fake(url, **kw):
            calls.append(url)
            r = replies[min(len(calls), len(replies)) - 1]
            if r == "CURLFAIL":
                raise SystemExit("curl failed (28): timeout")
            return r
        m._curl = fake
        u, err = m.check_sso("sso", attempts=6, pause=0)
        alive = u is not None
        ok = (alive == want_alive and (want_in is None or want_in in (err or ""))
              and (want_calls is None or len(calls) == want_calls))
        print("%-32s %-5s calls=%d err=%s" % (name, "PASS" if ok else "FAIL",
                                              len(calls), (err or "")[:70]))
        if not ok:
            fails.append(name)

    case("1 green first shot", [OK], True, want_calls=1)            # positive control
    case("2 the 10-13 shape 499x4 then 200", [T499] * 4 + [OK], True, want_calls=5)
    case("3 5xx then 200", [(502, "", "bad gateway"), OK], True, want_calls=2)
    case("4 curl failure then 200", ["CURLFAIL", OK], True, want_calls=2)
    # NEGATIVE CONTROLS: a real answer about the cookie must still kill it, at once
    case("5 401 dead, no retry", [(401, "", '{"code":16}')], False,
         want_in="http=401", want_calls=1)
    case("6 403 dead, no retry", [(403, "", "forbidden")], False,
         want_in="http=403", want_calls=1)
    # never clears: not alive, but must be labelled TRANSIENT, not a verdict
    case("7 499 forever = TRANSIENT", [T499], False, want_in="TRANSIENT", want_calls=6)
    # 2026-10-18: a Cloudflare 403 interstitial is the EGRESS, not the cookie
    # (a request with no cookie at all gets the same page), so it is retried
    # after rotating WARP.  Case 6 above is the negative control that keeps
    # this from swallowing a real 403 about the cookie.
    case("10 CF 403 then 200", [CF403, OK], True, want_calls=2)
    case("11 CF 403 forever = TRANSIENT", [CF403], False,
         want_in="TRANSIENT", want_calls=6)

    # castle_request_token warning on an empty JS-filled field
    seen = []
    m.log = lambda *a: seen.append(" ".join(map(str, a)))
    page = ('<input type="hidden" name="consent_token" value="abc">'
            '<input type="hidden" name="castle_request_token" value="">'
            '<button value="allow">Allow</button>')
    m._curl = lambda url, **kw: (200, "", page)
    m._die_on_html_403 = lambda *a: None
    m.consent_token("sso", "https://x/consent", None)
    ok = any("castle_request_token" in s for s in seen)
    print("%-32s %-5s" % ("8 empty castle token warns", "PASS" if ok else "FAIL"))
    if not ok:
        fails.append("castle warning missing")
    seen.clear()
    m._curl = lambda url, **kw: (200, "", page.replace('value=""', 'value="filled"'))
    m.consent_token("sso", "https://x/consent", None)
    ok = not any("castle_request_token" in s for s in seen)
    print("%-32s %-5s" % ("9 filled castle token is quiet", "PASS" if ok else "FAIL"))
    if not ok:
        fails.append("castle warning fired on a filled token")

    print("\n%d case(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
