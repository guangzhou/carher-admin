#!/usr/bin/env python3
"""
Review Bitable data after insert — checks completeness and reconciliation.
- All non-zero uids from stats-dir appear in Bitable
- Per-uid cost matches source JSON within $0.01
- 人员 link coverage vs uid_to_person map

Usage:
  review.py --stats-dir ./out --uid-to-person ./reg/uid_to_person.json
"""
import argparse, glob, json, os, re, subprocess, time
from collections import defaultdict

BASE = "MBKKbBkGcaTLOPs7KuncM7Rkn6g"
TABLE = "tblpcw43LreeMmNP"
URL_LIST = f"/open-apis/bitable/v1/apps/{BASE}/tables/{TABLE}/records"


def lark(method, url, *, params=None, identity="bot"):
    cmd = ["lark-cli", "api", method, url, "--as", identity]
    if params is not None:
        cmd += ["--params", json.dumps(params)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout)


def txt(v):
    if isinstance(v, list) and v:
        x = v[0]
        return x.get("text", "") if isinstance(x, dict) else str(x)
    return str(v or "")


def num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list) and v:
        x = v[0]
        if isinstance(x, dict):
            return float(x.get("value", 0) or 0)
        try:
            return float(x)
        except Exception:
            return 0.0
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def fetch_all_rows(identity: str) -> list[dict]:
    token, out = "", []
    while True:
        params = {"page_size": "500"}
        if token:
            params["page_token"] = token
        r = lark("GET", URL_LIST, params=params, identity=identity)
        if r.get("code") != 0:
            raise RuntimeError(r)
        out.extend(r["data"]["items"])
        if not r["data"].get("has_more"):
            break
        token = r["data"].get("page_token", "")
        time.sleep(0.2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats-dir", required=True)
    ap.add_argument("--uid-to-person", required=True)
    ap.add_argument("--as", dest="identity", default="bot")
    ap.add_argument("--category-prefix", default="OpenClaw-",
                    help="Only review rows whose 账户类型 starts with this (ignore other data in the table)")
    args = ap.parse_args()

    # Source truth
    pod_uid_total = {}
    for p in sorted(glob.glob(os.path.join(args.stats_dir, "uid-*.json"))):
        uid = os.path.basename(p).split("uid-")[1].split(".")[0]
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if "total" in d:
            pod_uid_total[uid] = d["total"]["cost_usd"]
    pod_nonzero = {u for u, t in pod_uid_total.items() if t > 0}

    # Table state
    rows = fetch_all_rows(args.identity)
    table_uid_cost = defaultdict(float)
    table_uid_person = {}
    for it in rows:
        f = it["fields"]
        cat = txt(f.get("账户类型", ""))
        if not cat.startswith(args.category_prefix):
            continue
        alias = txt(f.get("key_alias", ""))
        m = re.match(r"^carher-(\d+)$", alias.strip())
        if not m:
            continue
        uid = m.group(1)
        table_uid_cost[uid] += num(f.get("费用(USD)"))
        person = f.get("人员")
        if isinstance(person, list) and person:
            pid = person[0].get("id") or person[0].get("open_id")
            if pid:
                table_uid_person[uid] = pid

    uid_to_person = json.load(open(args.uid_to_person))

    print("=== Coverage ===")
    print(f"pods with non-zero : {len(pod_nonzero)}")
    print(f"table uids         : {len(table_uid_cost)}")
    missing = sorted(pod_nonzero - set(table_uid_cost), key=int)
    extra   = sorted(set(table_uid_cost) - pod_nonzero, key=int)
    print(f"  missing in tbl   : {missing}")
    print(f"  extra in tbl     : {extra}")

    print("\n=== Cost reconciliation ===")
    diffs = []
    for uid in pod_nonzero | set(table_uid_cost):
        src = pod_uid_total.get(uid, 0)
        tbl = table_uid_cost.get(uid, 0)
        diffs.append((uid, src, tbl, tbl - src))
    diffs.sort(key=lambda x: abs(x[3]), reverse=True)
    mism = [d for d in diffs if abs(d[3]) > 0.01]
    print(f"uids with |Δ| > $0.01: {len(mism)}")
    for uid, s, t, dd in mism[:10]:
        print(f"  uid={uid:<5} src=${s:>9.2f} tbl=${t:>9.2f} Δ=${dd:+.4f}")
    print(f"\ntotal src=${sum(pod_uid_total.values()):.2f}  tbl=${sum(table_uid_cost.values()):.2f}")

    print("\n=== 人员 link coverage ===")
    has_person = set(table_uid_person)
    no_reg = [u for u in table_uid_cost if u not in uid_to_person]
    missing_link = [u for u in table_uid_cost
                    if u in uid_to_person and u not in has_person]
    print(f"uids with 人员     : {len(has_person)}")
    print(f"uids no registration: {len(no_reg)} {sorted(no_reg, key=int)}")
    print(f"reg exists, link missing: {len(missing_link)} {missing_link}")


if __name__ == "__main__":
    main()
