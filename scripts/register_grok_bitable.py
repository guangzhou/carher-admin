#!/usr/bin/env python3
"""Write Grok account rows to the shared Feishu Base without persisting secrets."""

import argparse
import json
import subprocess
import sys

BASE = "Ukpobc4fcaNwGJsZz5cc0GnFnfh"
TABLE = "tblP0Dpf2BQE3mTR"
FIELDS = ["账号", "Grok密码", "Grok SSO"]


def parse_rows(text: str):
    rows = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split("----")
        if len(parts) != 3 or any(not item for item in parts):
            raise ValueError(f"line {lineno}: expected account----password----sso")
        rows.append(parts)
    if not rows:
        raise ValueError("no rows supplied")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        rows = parse_rows(sys.stdin.read())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    body = {"fields": FIELDS, "rows": rows}
    if args.dry_run:
        print(json.dumps({"fields": FIELDS, "row_count": len(rows)}, ensure_ascii=False))
        return 0

    proc = subprocess.run(
        [
            "lark-cli",
            "base",
            "+record-batch-create",
            "--base-token",
            BASE,
            "--table-id",
            TABLE,
            "--json",
            json.dumps(body, ensure_ascii=False),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        # Preserve the CLI's non-secret error, but do not print request data.
        print(proc.stderr or proc.stdout, file=sys.stderr, end="")
        return proc.returncode
    try:
        result = json.loads(proc.stdout)
        data = result.get("data", {})
        print(json.dumps({"ok": result.get("ok"), "count": len(data.get("record_id_list", [])), "record_id_list": data.get("record_id_list", [])}))
    except json.JSONDecodeError:
        print("lark-cli returned non-JSON output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
