#!/usr/bin/env python3
"""Report local Kiro account status without exposing credential fields."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


DEFAULT_ACCOUNTS = Path.home() / "Library/Application Support/.kiro-account-manager/accounts.json"


def load_accounts(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError("expected accounts.json to contain a list")
    return [item for item in data if isinstance(item, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--emails", type=Path, help="optional newline-delimited expected email list")
    args = parser.parse_args()

    accounts = load_accounts(args.accounts)
    emails = sorted({str(item.get("email", "")).strip() for item in accounts if item.get("email")})
    print(f"accounts_file={args.accounts}")
    print(f"count={len(emails)}")
    for email in emails:
        print(email)

    if args.emails:
        expected = {
            line.strip()
            for line in args.emails.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        present = set(emails)
        print(f"expected={len(expected)}")
        print(f"missing={len(expected - present)}")
        for email in sorted(expected - present):
            print(f"MISSING {email}")
        print(f"unexpected={len(present - expected)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
