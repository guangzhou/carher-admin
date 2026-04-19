#!/usr/bin/env python3
"""
Aggregate uid-*.json outputs from her-cost-stats.js into Bitable-ready rows.
One row per (uid, non-zero bucket). 账户类型 = OpenClaw-<label>.

Usage:
  build_rows.py --stats-dir ./out --uid-to-person ./reg/uid_to_person.json \\
                --out ./out/openclaw_rows.jsonl
"""
import argparse, glob, json, os
from datetime import datetime, timezone
from pathlib import Path

BUCKETS = [
    ("main_chat", "主对话"),
    ("dm", "DM私聊"),
    ("group_chat", "群聊"),
    ("subagent", "子代理"),
    ("dreaming", "Dreaming"),
    ("realtime", "Realtime"),
    ("orphan_sessions", "孤儿Session"),
    ("cron", "定时任务"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats-dir", required=True)
    ap.add_argument("--uid-to-person", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--snapshot-date", default=None,
                    help="YYYY-MM-DD, defaults to today UTC")
    args = ap.parse_args()

    uid_to_person = json.load(open(args.uid_to_person))
    today_str = args.snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    day_ms = int(datetime.strptime(today_str, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)

    summary = {"files": 0, "uids_no_person": [], "uids_zero": [],
               "per_bucket": {}, "total_usd": 0.0}
    rows = []

    for p in sorted(glob.glob(os.path.join(args.stats_dir, "uid-*.json"))):
        uid = Path(p).stem.split("uid-")[-1]
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if "total" not in d:
            continue
        summary["files"] += 1
        total = d["total"]["cost_usd"]
        summary["total_usd"] += total
        if total == 0:
            summary["uids_zero"].append(uid)
            continue

        pid = uid_to_person.get(uid)
        if not pid:
            summary["uids_no_person"].append(uid)

        for key, label in BUCKETS:
            b = d["sources"].get(key, {})
            cost = b.get("cost_usd", 0) or 0
            calls = b.get("calls", 0) or 0
            if cost == 0 and calls == 0:
                continue
            tok = b.get("tokens", {}) or {}
            fields = {
                "key_alias": f"carher-{uid}",
                "统计日": today_str,
                "日期": day_ms,
                "统计日期": today_ms,
                "费用(USD)": round(cost, 6),
                "Input Tokens": int(tok.get("input", 0) or 0),
                "Output Tokens": int(tok.get("output", 0) or 0),
                "Cache Read": int(tok.get("cacheRead", 0) or 0),
                "Cache Write": int(tok.get("cacheWrite", 0) or 0),
                "交互次数": int(calls),
                "账户类型": f"OpenClaw-{label}",
            }
            if pid:
                fields["人员"] = [{"id": pid}]
            rows.append(fields)
            summary["per_bucket"][label] = summary["per_bucket"].get(label, 0) + cost

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"files scanned    : {summary['files']}")
    print(f"uids zero spend  : {len(summary['uids_zero'])}")
    print(f"uids no person   : {len(summary['uids_no_person'])} {summary['uids_no_person']}")
    print(f"rows to insert   : {len(rows)}")
    print(f"total USD (all)  : ${summary['total_usd']:.2f}")
    print("by bucket:")
    for k, v in sorted(summary["per_bucket"].items(), key=lambda x: -x[1]):
        print(f"  {k:<16} ${v:>10.2f}")


if __name__ == "__main__":
    main()
