#!/usr/bin/env python3
"""Cases for xai_models_verdict's retry gate (2026-10-01).

Why this file exists: the verdict is a GATE -- a non-200 drops the account
before anything is written to sub2api.  On 2026-10-01 one transient
`403 permission-denied` dropped `ekobqjdskw@outlook.com`, whose same token
answered 200 (13 models) on 3/3 probes minutes later; it had to be onboarded
by hand as acct 71.  So the retry must be proven on BOTH faces:

  - a flap (403 then 200) must end GREEN, and must still SAY it flapped
  - a real credit-dead account (403 every time) must still end RED

A test that only covered the flap would happily pass a ruler that never
rejects anything, which is exactly the failure this gate exists to prevent.

Run: python3 test_xai_verdict_retry.py   (no network, no sub2api)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location(
        "cap", os.path.join(HERE, "xai_sso_device_oauth.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run():
    m = load()
    m.time.sleep = lambda *_: None  # no real pauses in the cases
    fails = []
    lines = []

    def case(name, replies, want_green, want_in=None):
        it = iter(replies)
        m._xai_models_once = lambda _t: next(it)
        got = m.xai_models_verdict("tok", attempts=3, pause=0,
                                   log=lambda *a: lines.append(" ".join(map(str, a))))
        green = got.startswith("200")
        ok = (green == want_green) and (want_in is None or want_in in got)
        print("%-34s %-5s %s" % (name, "PASS" if ok else "FAIL", got))
        if not ok:
            fails.append("%s: got %r (green=%s, want green=%s, want_in=%r)"
                         % (name, got, green, want_green, want_in))

    # positive control: a clean green must stay green and must NOT claim a flap
    case("1 green first shot", [("200 usable (13 models)", True)], True)

    # the 10-01 shape: transient 403 then 200 -> green, and the flap is visible
    case("2 flap 403 then 200",
         [("403 permission-denied", False), ("200 usable (13 models)", True)],
         True, want_in="flapped")

    # flap on the LAST allowed attempt still counts
    case("3 flap 403,403 then 200",
         [("403 permission-denied", False), ("403 permission-denied", False),
          ("200 usable (13 models)", True)], True, want_in="flapped")

    # NEGATIVE CONTROL: credit-dead must still be rejected after all attempts
    case("4 credit dead 3x403",
         [("403 personal-team-blocked:spending-limit", False)] * 3,
         False, want_in="3/3 attempts")

    # a dead token (401) is equally rejected, and the trail is in the string
    case("5 token dead 3x401",
         [("401 invalid api key", False)] * 3, False, want_in="3/3 attempts")

    # curl itself failing (no http code) must not be read as usable
    case("6 curl failed 3x?", [("? ", False)] * 3, False, want_in="attempts")

    # attempts=1 keeps the old single-shot behaviour when a caller asks for it
    it = iter([("403 permission-denied", False)])
    m._xai_models_once = lambda _t: next(it)
    got = m.xai_models_verdict("tok", attempts=1, pause=0)
    ok = not got.startswith("200")
    print("%-34s %-5s %s" % ("7 attempts=1 single shot",
                             "PASS" if ok else "FAIL", got))
    if not ok:
        fails.append("attempts=1 should still reject: %r" % got)

    # the retry notice must actually reach the log (a silent retry would hide
    # a flapping account from whoever reads the capture output)
    ok = any("retrying" in l for l in lines)
    print("%-34s %-5s %s" % ("8 retry is announced in log",
                             "PASS" if ok else "FAIL",
                             "%d log line(s)" % len(lines)))
    if not ok:
        fails.append("retry was silent; nothing said 'retrying'")

    print("\n%d case(s) failed" % len(fails))
    for f in fails:
        print("  " + f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
