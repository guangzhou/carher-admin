#!/usr/bin/env python3
"""
Write aggregated rows to the Lark Bitable spend table.

Spend Bitable: MBKKbBkGcaTLOPs7KuncM7Rkn6g, table tblpcw43LreeMmNP
Identity: bot (needs base:record write permission, or user with table ACL).

Usage:
  write_to_bitable.py --rows ./out/openclaw_rows.jsonl
  write_to_bitable.py --rows ./out/openclaw_rows.jsonl --replace-category OpenClaw
      # delete all existing rows with 账户类型 starting with "OpenClaw" first
"""
import argparse, json, subprocess, sys, time

BASE = "MBKKbBkGcaTLOPs7KuncM7Rkn6g"
TABLE = "tblpcw43LreeMmNP"

URL_CREATE = f"/open-apis/bitable/v1/apps/{BASE}/tables/{TABLE}/records/batch_create"
URL_DELETE = f"/open-apis/bitable/v1/apps/{BASE}/tables/{TABLE}/records/batch_delete"
URL_LIST   = f"/open-apis/bitable/v1/apps/{BASE}/tables/{TABLE}/records"

BATCH = 200


def lark(method, url, *, params=None, data=None, identity="bot"):
    cmd = ["lark-cli", "api", method, url, "--as", identity]
    if params is not None:
        cmd += ["--params", json.dumps(params)]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(p.stdout)
    except Exception:
        raise RuntimeError(f"stdout not JSON: {p.stdout[:400]}")


def delete_category(prefix: str, identity: str):
    """Page through all rows, find those with 账户类型 starting with prefix, delete them."""
    def txt(v):
        if isinstance(v, list) and v:
            x = v[0]
            return x.get("text", "") if isinstance(x, dict) else str(x)
        return str(v or "")

    ids = []
    token = ""
    while True:
        params = {"page_size": "500"}
        if token:
            params["page_token"] = token
        resp = lark("GET", URL_LIST, params=params, identity=identity)
        if resp.get("code") != 0:
            raise RuntimeError(f"list failed: {resp}")
        for it in resp["data"]["items"]:
            cat = txt(it["fields"].get("账户类型", ""))
            if cat.startswith(prefix):
                ids.append(it["record_id"])
        if not resp["data"].get("has_more"):
            break
        token = resp["data"].get("page_token", "")
        time.sleep(0.2)

    if not ids:
        print(f"no existing rows with 账户类型 prefix='{prefix}'")
        return

    print(f"deleting {len(ids)} existing rows...")
    for i in range(0, len(ids), BATCH):
        chunk = ids[i : i + BATCH]
        resp = lark("POST", URL_DELETE, data={"records": chunk}, identity=identity)
        if resp.get("code") != 0:
            raise RuntimeError(f"delete batch {i // BATCH + 1} failed: {resp}")
        print(f"  del batch {i // BATCH + 1}: {len(chunk)}")
        time.sleep(0.3)


def insert_rows(rows: list[dict], identity: str):
    print(f"inserting {len(rows)} rows in batches of {BATCH}...")
    total = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        data = {"records": [{"fields": r} for r in chunk]}
        resp = lark("POST", URL_CREATE, data=data, identity=identity)
        if resp.get("code") != 0:
            raise RuntimeError(f"insert batch {i // BATCH + 1} failed: {resp}")
        n = len(resp.get("data", {}).get("records", []))
        total += n
        print(f"  ins batch {i // BATCH + 1}/{(len(rows) + BATCH - 1) // BATCH}: +{n} total={total}")
        time.sleep(0.3)
    print(f"DONE. inserted {total}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True, help="JSONL file from build_rows.py")
    ap.add_argument("--replace-category", default=None,
                    help="If set, delete existing rows whose 账户类型 starts with this before insert")
    ap.add_argument("--as", dest="identity", default="bot", choices=["bot", "user"])
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.rows) if l.strip()]
    if not rows:
        print("no rows to insert", file=sys.stderr)
        return

    if args.replace_category:
        delete_category(args.replace_category, args.identity)

    insert_rows(rows, args.identity)


if __name__ == "__main__":
    main()
