#!/usr/bin/env python3
"""Cases for subscription_of()'s ACTIVE pick + tier_of()'s mapping (2026-10-18).

Why: `credentials.subscription_tier` is what the sub2api UI shows in the
platform/type column and what tier-filtering scripts read.  acct 157 is a
heavy account that went in as `grok.lite`, because /rest/subscriptions lists
an account's whole history and the old code took subs[0] -- which for that
account was a lite plan that expired 10-12, with the live ultra entry third.
Every earlier account had exactly one entry, so nothing caught it.

Both faces, so neither a wrong pick nor a wrong mapping can come back:
  - several entries, one ACTIVE   -> the ACTIVE one wins regardless of order
  - exactly one entry (the old shape) -> unchanged behaviour
  - nothing ACTIVE                -> first entry, flagged, never silently
  - unmapped productId            -> empty tier + a warning, never passed through

Run: python3 test_subscription_tier_pick.py   (no network)
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ACTIVE = "SUBSCRIPTION_STATUS_ACTIVE"
DEAD = "SUBSCRIPTION_STATUS_INACTIVE"


def load():
    spec = importlib.util.spec_from_file_location(
        "cap", os.path.join(HERE, "xai_sso_device_oauth.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ent(product, status, store="google", expiry="2026-11-12T07:13:28Z"):
    return {store: {"productId": product, "basePlanId": "p1m",
                    "expiryTime": expiry}, "status": status}


def run():
    m = load()
    fails = []
    logs = []
    m.log = lambda *a: logs.append(" ".join(map(str, a)))

    def pick(entries):
        m._curl = lambda url, **kw: (
            200, "", json.dumps({"subscriptions": entries}))
        return m.subscription_of("sso")

    def case(name, entries, want_product, want_tier, want_warn=None):
        logs.clear()
        sub = pick(entries)
        got_product = (sub or {}).get("productId")
        got_tier = m.tier_of(sub)
        warned = any(w in " ".join(logs) for w in ([want_warn] if want_warn else []))
        ok = (got_product == want_product and got_tier == want_tier
              and (want_warn is None or warned))
        print("%-42s %-5s product=%-14s tier=%-16s picked=%s"
              % (name, "PASS" if ok else "FAIL", got_product, got_tier,
                 (sub or {}).get("picked")))
        if not ok:
            fails.append(name)

    # the acct 157 shape: history first, the live plan LAST
    case("1 acct157: lite/ultra dead, ultra live",
         [ent("grok.lite", DEAD, expiry="2026-10-12T07:13:42Z"),
          ent("grok.ultra", DEAD, expiry="2026-10-12T07:14:12Z"),
          ent("grok.ultra", ACTIVE)],
         "grok.ultra", "supergrok_heavy")
    # positive control: the old one-entry shape must not change
    case("2 single live ultra (every acct <=156)",
         [ent("grok.ultra", ACTIVE)], "grok.ultra", "supergrok_heavy")
    # a genuinely lite account must still read lite -- the fix must not just
    # always answer "heavy"
    case("3 single live lite stays lite",
         [ent("grok.lite", ACTIVE)], "grok.lite", "supergrok_lite")
    case("4 live entry first, dead after",
         [ent("grok.ultra", ACTIVE), ent("grok.lite", DEAD)],
         "grok.ultra", "supergrok_heavy")
    # nothing active: fall back, but say so
    case("5 none active -> first + flagged",
         [ent("grok.lite", DEAD), ent("grok.ultra", DEAD)],
         "grok.lite", "supergrok_lite", want_warn="no ACTIVE subscription")
    # an unmapped product must not be written as if it were a tier
    case("6 unknown product -> empty tier + warn",
         [ent("grok.supernova", ACTIVE)], "grok.supernova", "",
         want_warn="unknown productId")
    # non-google stores
    case("7 stripe store, active",
         [ent("grok.ultra", ACTIVE, store="stripe")],
         "grok.ultra", "supergrok_heavy")
    # INACTIVE must not match on substring "ACTIVE"
    logs.clear()
    sub = pick([ent("grok.lite", DEAD), ent("grok.ultra", ACTIVE)])
    ok = sub["status"] == ACTIVE and sub["picked"] == "active"
    print("%-42s %-5s" % ("8 INACTIVE not matched as ACTIVE", "PASS" if ok else "FAIL"))
    if not ok:
        fails.append("INACTIVE substring")
    # no subscriptions at all / non-200 must stay None, not crash
    logs.clear()
    m._curl = lambda url, **kw: (200, "", json.dumps({"subscriptions": []}))
    a = m.subscription_of("sso")
    m._curl = lambda url, **kw: (403, "", "<html>blocked</html>")
    b = m.subscription_of("sso")
    ok = a is None and b is None and m.tier_of(None) == ""
    print("%-42s %-5s" % ("9 empty / non-200 -> None, tier \"\"", "PASS" if ok else "FAIL"))
    if not ok:
        fails.append("empty handling")

    # 2026-10-18 second fault: a failed probe used to pass silently as "" and
    # the account went in with an empty tier (acct 155/156).  It must now be
    # retried, and said out loud if it never clears.
    m.time.sleep = lambda *_: None
    m.rotate_warp = lambda *a, **k: True
    logs.clear()
    calls = []

    def flaky(url, **kw):
        calls.append(url)
        if len(calls) < 3:
            return 500, "", "upstream error"
        return 200, "", json.dumps({"subscriptions": [ent("grok.ultra", ACTIVE)]})
    m._curl = flaky
    sub = m.subscription_of("sso")
    ok = (m.tier_of(sub) == "supergrok_heavy" and len(calls) == 3
          and any("retrying" in l for l in logs))
    print("%-42s %-5s calls=%d" % ("10 probe 500x2 then 200 -> heavy", "PASS" if ok else "FAIL",
                                   len(calls)))
    if not ok:
        fails.append("probe retry")

    logs.clear()
    calls.clear()
    m._curl = lambda url, **kw: (calls.append(url), (500, "", "down"))[1]
    sub = m.subscription_of("sso")
    ok = (sub is None and len(calls) == 3
          and any("will be EMPTY" in l for l in logs))
    print("%-42s %-5s calls=%d" % ("11 probe never 200 -> loud EMPTY", "PASS" if ok else "FAIL",
                                   len(calls)))
    if not ok:
        fails.append("probe failure must be loud")

    # 2026-10-19 acct 158~161: apple, grok.pro.monthly.30, dominantPlan supergrok,
    # no expiryTime (period end is top-level billingPeriodEnd).  Vendor called them heavy.
    def apple(product, plan, end="2027-01-19T03:59:07Z"):
        return {"subscriptions": [{"apple": {"productId": product, "autoRenewOn": True},
                                   "tier": "SUBSCRIPTION_TIER_GROK_PRO",
                                   "status": ACTIVE, "billingPeriodEnd": end,
                                   "activeOffer": {"providerOfferId": "sg_x"}}],
                "dominantPlan": {"plan": plan}}

    def doc_case(name, doc, want_tier, want_warn=None, want_expiry=None):
        logs.clear()
        m._curl = lambda url, **kw: (200, "", json.dumps(doc))
        sub = m.subscription_of("sso")
        got = m.tier_of(sub)
        ok = (got == want_tier
              and (want_warn is None or want_warn in " ".join(logs))
              and (want_expiry is None or (sub or {}).get("expiryTime") == want_expiry))
        print("%-42s %-5s tier=%-16s expiry=%s" % (name, "PASS" if ok else "FAIL", got,
                                                  (sub or {}).get("expiryTime")))
        if not ok:
            fails.append(name)

    doc_case("12 apple pro.monthly.30 -> supergrok", apple("grok.pro.monthly.30", "supergrok"),
             "supergrok", want_expiry="2027-01-19T03:59:07Z")
    # plan wins over an unknown / disagreeing SKU, and the disagreement is said
    doc_case("13 unknown SKU, plan known -> plan", apple("grok.new.sku", "supergrok"), "supergrok")
    doc_case("14 SKU says heavy, plan supergrok -> plan+warn",
             apple("grok.ultra", "supergrok"), "supergrok", want_warn="using the plan")
    # negative control: a real heavy plan must still read heavy
    doc_case("15 plan supergrok_heavy stays heavy", apple("grok.ultra", "supergrok_heavy"),
             "supergrok_heavy")
    doc_case("16 unknown SKU + unknown plan -> empty+warn", apple("grok.x", "mystery"), "",
             want_warn="unknown productId")

    import base64
    def tok(claims):
        b = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return "eyJhbGciOiJSUzI1NiJ9.%s.sig" % b
    ok = (m.jwt_tier_of(tok({"tier": 5})) == 5 and m.jwt_tier_of(tok({"tier": 1})) == 1
          and m.jwt_tier_of(tok({})) is None and m.jwt_tier_of("garbage") is None
          and m.JWT_TIER.get(1) == "supergrok" and m.JWT_TIER.get(5) == "supergrok_heavy")
    print("%-42s %-5s" % ("17 jwt tier claim decode", "PASS" if ok else "FAIL"))
    if not ok:
        fails.append("jwt tier")

    print("\n%d case(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
