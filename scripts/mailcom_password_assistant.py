#!/usr/bin/env python3
"""Safe, local mail.com password-change queue.

This tool deliberately stops before credential entry and submission. It keeps
only account names and progress timestamps; generated passwords are never
written to disk.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

EMAIL_RE = re.compile(r"^[^@\s]+@mail\.com$", re.I)
DEFAULT_SUFFIX = "!@#456"
DEFAULT_STATE = Path("mailcom-password-progress.json")


def target_password(email: str, suffix: str = DEFAULT_SUFFIX) -> str:
    """Return the requested deterministic target without persisting it."""
    local = email.rsplit("@", 1)[0]
    return f"{local}{suffix}"


def load_accounts(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = csv.DictReader(fh)
        if not rows.fieldnames or "email" not in rows.fieldnames:
            raise ValueError("CSV must contain an 'email' column")
        accounts = []
        for row in rows:
            email = (row.get("email") or "").strip()
            if not EMAIL_RE.fullmatch(email):
                raise ValueError(f"invalid mail.com address: {email!r}")
            accounts.append(email)
    return list(dict.fromkeys(accounts))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(path: Path, accounts: list[str]) -> dict:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("accounts") != accounts:
            raise ValueError("state file accounts differ from the CSV; use a new --state")
        return data
    return {"version": 1, "accounts": accounts, "items": {a: {"status": "pending"} for a in accounts}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", type=Path, help="CSV with one 'email' column")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--suffix", default=DEFAULT_SUFFIX)
    ap.add_argument("--open", action="store_true", help="open mail.com for the next account")
    ap.add_argument("--reveal", action="store_true", help="print the target password for the next account")
    ap.add_argument("--mark", choices=("success", "failed", "skip"))
    args = ap.parse_args()

    try:
        accounts = load_accounts(args.csv)
        state = load_state(args.state, accounts)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pending = next((a for a in accounts if state["items"][a]["status"] == "pending"), None)
    if not pending:
        print("queue complete")
        return 0

    item = state["items"][pending]
    if args.mark:
        item.update(status=args.mark, updated_at=now())
        save_state(args.state, state)
        print(f"marked {pending}: {args.mark}")
        return 0

    print(f"next account: {pending}")
    print("1. Log in at https://www.mail.com/")
    print("2. Settings -> Password / Account -> Change password")
    print("3. Enter the current password, then the generated target password twice.")
    print("4. Click Save changes yourself, then rerun with --mark success (or failed).")
    if args.reveal:
        print(f"target password: {target_password(pending, args.suffix)}")
    else:
        print("target password: hidden (rerun with --reveal when you are ready to type it)")
    if args.open:
        webbrowser.open("https://www.mail.com/", new=2)
    item.setdefault("started_at", now())
    save_state(args.state, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
