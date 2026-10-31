#!/usr/bin/env python3
"""Add speed-test numbers to kiro slots and append new exits as DISABLED slots (no Kiro account assigned).

Run on 198 as cltx (needs slots49+ /slots/config with the `bench` field, i.e. slots51+):

    python3 kiro-slots-bench-apply.py plan.json            # dry-run: prints every row's decision
    python3 kiro-slots-bench-apply.py plan.json --apply    # backup → one PUT → read back and check

plan.json rows (from .work/px31/plan.py): ip, kiroSecs, downMbps, score, rank, of, action, proxyUrl, [slotName], cc.
  action=existing ⇒ find the slot by exit IP in its name, set bench only (name / proxy / weight / enabled untouched)
  action=new      ⇒ append {name: <cc>-<ip>, proxyUrl, user kiro, password from ~/kiro-px-egress/<n>/pw,
                     enabled: false, weight}. Disabled slots never get an account (reconcile only fills enabled ones).
🔴 Dedup: refuses if an IP or a proxy URL would appear twice, or a name collides. One PUT for the whole batch, so it is
all-or-nothing; the backup restores the exact previous list (`--restore <file>`).
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error

B = "http://127.0.0.1:30891/api/admin"
AK = open(os.path.expanduser("~/kiro-us-egress/.ak")).read().strip()


def api(method, path, body=None):
    req = urllib.request.Request(B + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"x-api-key": AK, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def ip_of(name):
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)$", name or "")
    return m.group(1) if m else None


def weight_for(kiro_secs):
    # same rule as the handoff: <1.0s ⇒ 3, <1.8s ⇒ 2, else 1
    return 3 if kiro_secs < 1.0 else 2 if kiro_secs < 1.8 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", nargs="?")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore")
    ap.add_argument("--measured", default="10-31 iGuge 31 出口")
    a = ap.parse_args()

    if a.restore:
        rows = json.load(open(a.restore))
        st, j = api("PUT", "/slots/config", rows)
        print("restore", st, (j if st != 200 else f"{len(j)} slots"))
        return 0 if st == 200 else 1

    # version gate: slots51+ keeps `bench`; older binaries silently drop it ⇒ STOP before writing anything
    snap0 = api("GET", "/slots")[1]
    if snap0["slots"] and "bench" not in snap0["slots"][0]:
        print("STOP: running kiro has no `bench` field (needs slots51+)"); return 1
    empty = [s["name"] for s in snap0["slots"] if s.get("accountId") is None]
    if empty:
        print("STOP: enabled slots without an account would get one on save:", empty); return 1
    cur_st, cur = api("GET", "/slots/config")
    assert cur_st == 200 and isinstance(cur, list), (cur_st, cur)
    assert all(r.get("proxyPassword") in (None, "", "******") for r in cur), "unexpected unmasked password"
    plan = json.load(open(a.plan))
    by_ip = {}
    for i, r in enumerate(cur):
        ip = ip_of(r["name"])
        if ip:
            assert ip not in by_ip, f"STOP: IP {ip} already appears twice in current slots"
            by_ip[ip] = i
    rows = [dict(r) for r in cur]
    new_rows, problems = [], []
    for p in plan:
        bench = {"kiroSecs": p["kiroSecs"], "downMbps": p["downMbps"], "score": p["score"], "rank": p["rank"], "of": p["of"], "measured": a.measured}
        if p["ip"] in by_ip:
            i = by_ip[p["ip"]]
            card = sum(1 for r in rows[:i + 1] if r["enabled"])
            if rows[i].get("bench") == bench:
                print(f'OK  {"槽" + str(card) if rows[i]["enabled"] else "停用":<4} {rows[i]["name"]:<22} bench already set')
            else:
                rows[i]["bench"] = bench
                print(f'OK  {"槽" + str(card) if rows[i]["enabled"] else "停用":<4} {rows[i]["name"]:<22} + bench 第{p["rank"]}/{p["of"]} {p["score"]:.1f}分 {p["kiroSecs"]:.2f}s {p["downMbps"]:.0f}Mbps')
            continue
        if p["action"] != "new":
            problems.append(f'{p["ip"]}: no slot and no kiro proxy (action={p["action"]}) — skipped')
            continue
        n = int(re.search(r"kiro-px-(\d+)\.", p["proxyUrl"]).group(1))
        pwf = os.path.expanduser(f"~/kiro-px-egress/{n}/pw")
        if not os.path.exists(pwf):
            problems.append(f'{p["ip"]}: missing {pwf}')
            continue
        cc = (p.get("cc") or "").lower()
        if not re.fullmatch(r"[a-z]{2}", cc):
            problems.append(f'{p["ip"]}: no measured country code ({cc!r})')
            continue
        name = f"{cc}-{p['ip']}"
        new_rows.append({"name": name, "proxyUrl": p["proxyUrl"], "proxyUsername": "kiro", "proxyPassword": open(pwf).read().strip(),
                         "enabled": False, "weight": weight_for(p["kiroSecs"]), "acceptNew": True, "vipAliases": [], "bench": bench})
        print(f'NEW 停用 {name:<22} {p["proxyUrl"]:<38} w{weight_for(p["kiroSecs"])} 停用 · 第{p["rank"]}/{p["of"]} {p["score"]:.1f}分')
    final = rows + new_rows
    names = [r["name"] for r in final]
    urls = [r.get("proxyUrl") for r in final if r.get("proxyUrl")]
    ips = [ip_of(n) for n in names if ip_of(n)]
    dup = {x for x in names if names.count(x) > 1} | {x for x in urls if urls.count(x) > 1} | {x for x in ips if ips.count(x) > 1}
    if dup:
        print("STOP duplicates:", sorted(dup)); return 1
    if len(final) > 50:
        print("STOP: more than 50 slot rows"); return 1
    for x in problems:
        print("SKIP", x)
    print(f"current {len(cur)} rows → {len(final)} rows (new {len(new_rows)}, all disabled); enabled stays {sum(r['enabled'] for r in final)}")
    if not a.apply:
        print("dry-run, nothing written"); return 0
    bk = os.path.expanduser(f"~/kiro-px-egress/slots-config-before-bench-{time.strftime('%Y%m%dT%H%M%S')}.json")
    json.dump(cur, open(bk, "w"), ensure_ascii=False, indent=1)
    os.chmod(bk, 0o600)
    print("backup", bk)
    before_snap = api("GET", "/slots")[1]["slots"]
    # someone saved in the Admin page between our GET and now ⇒ don't overwrite their change
    if api("GET", "/slots/config")[1] != cur:
        print("STOP: slot config changed while planning, re-run"); return 1
    st, j = api("PUT", "/slots/config", final)
    if st != 200:
        print("STOP PUT", st, j); return 1
    st, back = api("GET", "/slots/config")
    ok = len(back) == len(final) and all(
        b["name"] == f["name"] and b.get("proxyUrl") == f.get("proxyUrl") and b["enabled"] == f["enabled"] and b["weight"] == f["weight"] and b.get("bench") == f.get("bench")
        for b, f in zip(back, final))
    after_snap = api("GET", "/slots")[1]["slots"]
    same_accounts = [(s["name"], s["accountId"]) for s in before_snap] == [(s["name"], s["accountId"]) for s in after_snap]
    print(f"readback {'OK' if ok else 'MISMATCH'} · running slots {len(before_snap)}→{len(after_snap)} · accounts unchanged: {same_accounts}")
    return 0 if ok and same_accounts else 1


if __name__ == "__main__":
    sys.exit(main())
