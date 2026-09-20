#!/usr/bin/env python3
"""Probe ONE litellm-proxy pod several times per model, print a per-pod tally.

WHY per-pod: 4 replicas x --num_workers 2 = 8 independent processes and there is
no Redis, so each worker keeps its OWN in-memory cooldown table. Through the
Service you cannot tell "one bad worker" from "a broken upstream" -- the same
request alternates 200/500 by luck of the draw. Talking to one pod directly at
least narrows it to 2 workers.

Prompt is a bare "hi": the earlier "Reply with exactly: PROBE-OK-9ROUTER" made
the model's compliance part of the ruler, which is not what is being measured
here. HTTP status + x-litellm-model-id is the ruler.

GROK CONTROL (leg 0): a positive control that must be GREEN. If grok is red too,
the fault is not in this repoint and nothing below it can be read.
  NOTE: carher-1's allowlist contains no grok at all, so the control CANNOT run
  on a carher-1-shaped key -- it returns 403 key_model_access_denied, which is
  the probe's own doing and not a finding. The control therefore runs on the
  master key, which bypasses the allowlist. (2026-09-17: my first attempt read
  that 403 as data.)

Env: PROBE_KEY (carher-1-shaped), ROUNDS (default 4), POD_NAME (label only).
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
PROBE_KEY = os.environ.get("PROBE_KEY") or ""
ROUNDS = int(os.environ.get("ROUNDS") or "4")
POD = os.environ.get("POD_NAME") or "?"

# (model, which key). The two targets go through carher-1's real shape; the
# control goes through the master key for the allowlist reason above.
LEGS = [
    ("claude-grok-4.6", "master"),   # positive control
    ("claude-fable-5.1", "probe"),
    ("claude-opus-5", "probe"),
]


def call(path, body=None, method="GET", key=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Authorization": "Bearer " + (key or MK),
                 "Content-Type": "application/json"},
        method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")
    except Exception as e:                      # noqa: BLE001
        return 0, {}, f"{type(e).__name__}: {e}"


def main():
    for model, which in LEGS:
        key = MK if which == "master" else PROBE_KEY
        if which == "probe" and not PROBE_KEY:
            print(f"[{POD}] {model:20} SKIP (no PROBE_KEY)")
            continue
        codes, mids, last = [], set(), ""
        for _ in range(ROUNDS):
            st, hdr, raw = call(
                "/v1/chat/completions",
                {"model": model, "messages": [{"role": "user", "content": "hi"}],
                 "max_tokens": 24},
                "POST", key=key)
            codes.append(st)
            mid = hdr.get("x-litellm-model-id")
            if mid:
                mids.add(mid)
            if st != 200:
                last = raw[:200]
            elif not last:
                try:
                    last = json.loads(raw)["choices"][0]["message"]["content"][:60]
                except Exception:               # noqa: BLE001
                    last = raw[:60]
        green = sum(1 for c in codes if c == 200)
        tag = "CONTROL" if which == "master" else "       "
        print(f"[{POD}] {tag} {model:20} {green}/{ROUNDS} green  "
              f"codes={codes}  model-id={sorted(mids) or ['-']}")
        if green < ROUNDS:
            print(f"           last-non-200: {last}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
