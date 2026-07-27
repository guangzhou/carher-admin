#!/usr/bin/env python3
"""Score the SHIPPED refusal detector against the corpus offline (milliseconds
instead of 40s network round-trips).

2026-07-27: this file used to carry its own COPY of the detector, "kept in sync
with the shipped bridge implementation" by hand. It was verified byte-identical
across all 172 lines of the shared span -- i.e. it had zero value as an
independent implementation, and every edit had to be written twice. That is not a
baseline, it is a second thing to forget: a fix landing only in the bridge (which
is exactly what happened while fixing the 2026-07-27 regression) leaves this
harness scoring a detector nobody runs, and it reads as a passing test.

So it now LOADS the real function from the bridge. The bridge's module body is
import-safe -- UPSTREAMS and friends are env-defaulted and the server only starts
under `__main__` -- so importing it costs nothing and cannot drift.

To trial a CANDIDATE change, edit the regexes in the bridge and re-run this, or
monkeypatch the loaded module here (e.g. `bridge._NARRATION = re.compile(...)`)
before calling corpus.score. Both approaches score the same code path the pods do.

History worth keeping (why the detector looks the way it does):
  * The FIRST rewrite scored 17/17 on a self-built corpus and was still wrong in
    production, two ways at once. `_SELF` (the "denial must be about the
    assistant" guard) matched the bare pronoun 我, present in nearly every Chinese
    reply -- so the guard was vacuous and CORRECT answers that mentioned a missing
    permission, or closed with a next-step suggestion, were discarded and
    re-asked. Requiring that same pairing lost every pronoun-less Chinese refusal
    ("抱歉，这里没有终端可用"), and `_EN_DENIAL` was pinned to a leading "i" so it
    missed "Unable to read..." / "Sorry, cannot run..." -- those scored as SUCCESS
    and were returned to the user verbatim.
  * The discriminator that actually works is `_REPORTED_RESULT`: a reply stating
    what it RAN and what came BACK is an answer, however much hedging follows.
    Denials are then sufficient on their own.
  * But the report exemption must NOT be an unconditional early return: a reply
    that ran `--help` and then admits it has not done the real task is a stall.
    See the STALL set in corpus.py. Flipping the order outright (bb2bd57) traded
    that for 8 false positives on ordinary report content; the fix was to tighten
    the narration patterns and let the admission override the report.

Lesson: a self-built corpus is not a regression baseline. When replacing a
matcher, every phrasing the OLD one caught must be added as a test first -- and
the corpus must FAIL the old implementation, or it is not measuring anything.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGE = os.path.join(_HERE, "..", "zerokey-codex-responses-bridge.py")


def load_bridge():
    """Import the bridge module without starting its server."""
    spec = importlib.util.spec_from_file_location("bridge", _BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bridge"] = mod
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    sys.path.insert(0, _HERE)
    import corpus

    bridge = load_bridge()
    print("scoring SHIPPED detector from %s" % os.path.relpath(_BRIDGE, _HERE))
    corpus.score(bridge._looks_like_refusal, verbose=True)
