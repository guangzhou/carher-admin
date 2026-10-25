#!/usr/bin/env python3
"""Offline cases for the 10-22 authorization-code fallback in xai_sso_device_oauth.one().

No network, no browser: every outward call is stubbed.  What is being pinned:
  1 browser mode + castle 403            -> falls back to authcode, onboarding succeeds
  2 browser mode + a NON-castle failure  -> must NOT fall back (negative control:
                                            a fallback that swallows every error
                                            would hide real breakage)
  3 authcode mode                        -> never touches the device flow at all
  4 browser mode + approve ok            -> unchanged old path, polls the device code
  5 authcode itself fails                -> the failure surfaces, not a fake success
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xai_sso_device_oauth as m  # noqa: E402

TOK = {"access_token": "eyJ" + "a" * 60, "refresh_token": "r" * 40,
       "expires_in": 21600, "token_type": "Bearer"}
calls = []


def reset(browser_exc=None, authcode_exc=None):
    calls.clear()
    m.log = lambda *a: None
    m.check_sso = lambda sso, **k: ({"email": "x@y.com", "userId": "u1"}, None)
    m.subscription_of = lambda sso: {"plan": "supergrok.heavy"}
    m.tier_of = lambda sub, log=None: "supergrok_heavy"
    m.jwt_tier_of = lambda at: 5
    m.xai_models_verdict = lambda at, log=None: "200 usable (14 models)"

    def device_code(jar=None):
        calls.append("device_code")
        return {"user_code": "AAAA-BBBB", "device_code": "dc",
                "verification_uri_complete": "https://x/dev", "interval": 1}

    def browser_approve(sso, url, shots=None):
        calls.append("browser_approve")
        if browser_exc:
            raise SystemExit(browser_exc)
        return 200

    def authcode_grant(sso, shots=None):
        calls.append("authcode_grant")
        if authcode_exc:
            raise SystemExit(authcode_exc)
        return dict(TOK)

    def poll_token(dc, interval, budget_s=120):
        calls.append("poll_token")
        return dict(TOK)

    m.device_code, m.browser_approve = device_code, browser_approve
    m.authcode_grant, m.poll_token = authcode_grant, poll_token


def run(mode):
    out = tempfile.mkstemp(prefix=".t-bundle-")[1]
    try:
        ok = m.one("x@y.com", "eyJsso", out, approve_mode=mode)
        b = json.load(open(out)) if ok and os.path.getsize(out) else None
        return ok, b, None
    except SystemExit as e:
        return False, None, str(e)
    finally:
        os.unlink(out)


CASTLE = "browser approve rejected (castle) -- do NOT add a castle wait"
results = []


def case(name, cond):
    results.append(cond)
    print("%-48s %s" % (name, "PASS" if cond else "FAIL  calls=%s" % calls))


reset(browser_exc=CASTLE)
ok, b, err = run("browser")
case("1 castle 403 -> authcode fallback -> success",
     ok and b and calls == ["device_code", "browser_approve", "authcode_grant"]
     and "poll_token" not in calls)

reset(browser_exc="browser approve never saw the Allow button (last url /x)")
ok, b, err = run("browser")
case("2 non-castle failure does NOT fall back",
     not ok and "authcode_grant" not in calls and "Allow button" in (err or ""))

reset()
ok, b, err = run("authcode")
case("3 authcode mode skips the device flow",
     ok and calls == ["authcode_grant"])

reset()
ok, b, err = run("browser")
case("4 approve ok -> old path polls the device code",
     ok and calls == ["device_code", "browser_approve", "poll_token"])

reset(browser_exc=CASTLE, authcode_exc="authcode: no Allow button on /x")
ok, b, err = run("browser")
case("5 fallback failing surfaces, no fake success",
     not ok and "no Allow button" in (err or ""))

reset()
ok, b, err = run("authcode")
case("6 bundle carries the token and the tier",
     ok and b["credentials"]["access_token"] == TOK["access_token"]
     and b["credentials"].get("subscription_tier") == "supergrok_heavy")

print("\n%d case(s) failed" % results.count(False))
sys.exit(1 if False in results else 0)