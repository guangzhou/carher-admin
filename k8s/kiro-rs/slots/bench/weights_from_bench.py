"""Set slot weights from the bench score (enabled slots only).

  python3 weights_from_bench.py            # dry-run table
  python3 weights_from_bench.py --apply    # backup -> one PUT -> read back

Run on 198 as cltx (reads ~/kiro-us-egress/.ak; sudo would turn ~ into /root).

Mapping (weight is 1~10, default 5; it drives new-user assignment, the 封口 judgement
(用量 ÷ 权重) and the in-flight cap (auto × weight/5)):

    score >= 70  -> 6      55~70 -> 5      40~55 -> 4      25~40 -> 3      < 25 -> 2

then the guards the score alone cannot see:

  🔴 NO bandwidth floor. My first version cut any exit with downMbps <= 5 to weight 1, which would
     have taken sg-47.84.85.100 from 5 to 1 — a 5x cut on a slot carrying 45 users. The live numbers
     say the opposite: that slot has the LOWEST latency of the fast group (9.2s) and 0 queueTimeouts,
     i.e. 5 Mbps is not hurting it. Kiro answers are small streamed JSON, not 10MB files, so the
     download figure is a poor proxy for user-visible speed. For the 4 aliyun exits it is not even
     measured (it is the EIP cap, see README) ⇒ never gate weight on it.
  🔴 An exit that is up but takes NO traffic is not a win: sg-47.236.200.98 has 20 users bound and
     `sent=0` / `latencyMs=None`. Weight cannot fix that; leave it at 1 and raise it with the user.
  🔴 Never raise a slot by more than +2 in one go: weight also widens the in-flight cap, and these
     exits are carrying live users. Lowering is not capped (relieving a bad exit is safe).
  🔴 Relieve the slot that is actually queueing: sg-47.84.112.136 is w7 with 60 users and owns
     51/51 of all queueTimeouts (10.2% of its own sends) ⇒ the score band putting it at 5 is the
     right direction and is applied even though it is the busiest slot.

Only `weight` changes. name / proxy / enabled / acceptNew / vipAliases / bench are untouched, and the
readback asserts that. Disabled slots are left alone: their weight is just a starting value for
whenever someone enables them, and rewriting it here would be noise.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

B = "http://127.0.0.1:30891/api/admin"
AK = open(os.path.expanduser("~/kiro-us-egress/.ak")).read().strip()
MAX_RAISE = 2
# exits to leave pinned at their current weight regardless of score, with the reason
PINNED = {"sg-47.236.200.98": "1 Mbps 按流量计费 + 现在 sent=0，权重解决不了，等用户拍板"}


def api(method, path, body=None):
    req = urllib.request.Request(B + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"x-api-key": AK, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def target(score):
    for lo, w in ((70, 6), (55, 5), (40, 4), (25, 3)):
        if score >= lo:
            return w
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore")
    a = ap.parse_args()

    if a.restore:
        rows = json.load(open(a.restore))
        st, j = api("PUT", "/slots/config", rows)
        print("restore", st, j if st != 200 else "%d slots" % len(j))
        return 0 if st == 200 else 1

    st, cur = api("GET", "/slots/config")
    assert st == 200 and isinstance(cur, list), (st, cur)
    assert all(r.get("proxyPassword") in (None, "", "******") for r in cur), "unexpected unmasked password"

    # live view: bound users per slot, so the dry-run shows who is affected
    live = {s["name"]: s for s in api("GET", "/slots")[1]["slots"]}

    rows = [dict(r) for r in cur]
    changes = []
    print("%-24s %-4s %-5s %-7s %-6s %-6s %s" % ("slot", "w", "->", "score", "Mbps", "users", "why"))
    for r in rows:
        if not r.get("enabled"):
            continue
        b = r.get("bench") or {}
        sc, down = b.get("score"), b.get("downMbps")
        if sc is None:
            print("%-24s %-4s %-5s %-7s %-6s %-6s SKIP no bench" % (r["name"], r["weight"], "-", "-", "-", "-"))
            continue
        if r["name"] in PINNED:
            print("%-24s %-4s %-5s %-7s %-6s %-6s PINNED %s"
                  % (r["name"], r["weight"], r["weight"], sc, down, "-", PINNED[r["name"]]))
            continue
        want = target(sc)
        if want > r["weight"] + MAX_RAISE:
            want = r["weight"] + MAX_RAISE
            why = "capped +%d/round" % MAX_RAISE
        else:
            why = "score band"
        users = (live.get(r["name"], {}) or {}).get("users")
        users = len(users) if isinstance(users, list) else users
        mark = "" if want == r["weight"] else ("  ← " + why)
        print("%-24s %-4s %-5s %-7s %-6s %-6s %s" % (r["name"], r["weight"], want, sc, down, users, mark.strip()))
        if want != r["weight"]:
            changes.append((r["name"], r["weight"], want))
            r["weight"] = want

    if not changes:
        print("\nnothing to change")
        return 0
    print("\n%d slots change: %s" % (len(changes), ", ".join("%s %d→%d" % c for c in changes)))
    print("🔴 权重只影响新人分配/封口/在途上限，**不挪已绑的人**（要挪点界面「重新分配用户」）")
    if not a.apply:
        print("dry-run, nothing written")
        return 0

    bk = os.path.expanduser("~/kiro-px-egress/slots-config-before-weights-%s.json" % time.strftime("%Y%m%dT%H%M%S"))
    json.dump(cur, open(bk, "w"), ensure_ascii=False, indent=1)
    os.chmod(bk, 0o600)
    print("backup", bk)

    before = [(s["name"], s.get("accountId")) for s in api("GET", "/slots")[1]["slots"]]
    if api("GET", "/slots/config")[1] != cur:
        print("STOP: slot config changed while planning, re-run")
        return 1
    st, j = api("PUT", "/slots/config", rows)
    if st != 200:
        print("STOP PUT", st, j)
        return 1

    st, back = api("GET", "/slots/config")
    ok = len(back) == len(rows) and all(
        b["name"] == f["name"] and b["weight"] == f["weight"] and b["enabled"] == f["enabled"]
        and b.get("proxyUrl") == f.get("proxyUrl") and b.get("bench") == f.get("bench")
        and b.get("acceptNew") == f.get("acceptNew") and b.get("vipAliases") == f.get("vipAliases")
        for b, f in zip(back, rows))
    after = [(s["name"], s.get("accountId")) for s in api("GET", "/slots")[1]["slots"]]
    print("readback %s · slots %d→%d · accounts unchanged: %s"
          % ("OK" if ok else "MISMATCH", len(before), len(after), before == after))
    return 0 if ok and before == after else 1


if __name__ == "__main__":
    sys.exit(main())
