"""Recompute the combined ranking over all 36 exits (31 iGuge + 5 self-built).

  python3 rank36.py doc-before.md self5.tsv > rank36.json

Same formula as the 31-exit doc: each metric is normalised 0~100 across THIS set's best/worst,
combined as 连 Kiro 60% + 下载 40%. Adding the 5 self-built exits changes both extremes, so every
rank and score is recomputed — the old "第 N/31" numbers are not reusable.

Source labels: iguge | self-243 | self-aliyun-squid. The 4 aliyun exits' 下载 is the EIP bandwidth
cap from the console, not a measurement — carried through as down_is_cap so every renderer can say so.
"""
import json
import re
import sys

ALIYUN = {"47.84.112.136", "47.84.101.218", "47.84.85.100", "47.236.200.98"}


def load_doc(path):
    doc = json.load(open(path))["data"]["document"]["content"]
    t = doc.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", t, re.S):
        c = [re.sub(r"<[^>]+>", "", x).replace("&amp;", "&").strip()
             for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        rank, node, ip, score, note, kiro, down = c
        rows.append({"node": node, "ip": ip, "note": note,
                     "kiroSecs": float(kiro.replace("秒", "").strip()),
                     "downMbps": float(down.replace("Mbps", "").strip()),
                     "source": "iguge", "down_is_cap": False, "old_rank": int(rank)})
    assert len(rows) == 31, len(rows)
    return rows


def load_self(path):
    rows = []
    for line in open(path):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) < 6 or f[2] == "STOP":
            print("SKIP (not benched): %s" % line, file=sys.stderr)
            continue
        name, ip, kiro, n_ok, down, src = f[0], f[1], float(f[2]), int(f[3]), f[4], f[5]
        assert down != "", "no download figure for %s" % name
        rows.append({"node": name, "ip": ip, "note": "自建",
                     "kiroSecs": kiro, "downMbps": float(down),
                     "source": "self-aliyun-squid" if ip in ALIYUN else "self-243",
                     "down_is_cap": ip in ALIYUN, "n_kiro": n_ok})
    return rows


def main():
    rows = load_doc(sys.argv[1]) + load_self(sys.argv[2])
    ips = [r["ip"] for r in rows]
    assert len(set(ips)) == len(ips), "duplicate IP across the two sources"
    kbest, kworst = min(r["kiroSecs"] for r in rows), max(r["kiroSecs"] for r in rows)
    dbest, dworst = max(r["downMbps"] for r in rows), min(r["downMbps"] for r in rows)
    for r in rows:
        ks = 100.0 * (kworst - r["kiroSecs"]) / (kworst - kbest)
        ds = 100.0 * (r["downMbps"] - dworst) / (dbest - dworst)
        r["score"] = round(ks * 0.6 + ds * 0.4, 1)
    rows.sort(key=lambda r: -r["score"])
    for i, r in enumerate(rows, 1):
        r["rank"], r["of"] = i, len(rows)
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    print("# kiro best/worst %.2f/%.2f  down best/worst %.0f/%.0f  n=%d"
          % (kbest, kworst, dbest, dworst, len(rows)), file=sys.stderr)
    for r in rows:
        print("%2d %-26s %-18s %5.1f  %.2fs %5.0fMbps%s"
              % (r["rank"], r["node"], r["source"], r["score"], r["kiroSecs"], r["downMbps"],
                 "  (EIP上限)" if r["down_is_cap"] else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
