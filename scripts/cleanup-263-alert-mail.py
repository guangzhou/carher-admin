#!/usr/bin/env python3
"""Repeat recoverable 263 alert-mail deletion until the Inbox is clean.

Each pass uses the existing UI-only delete script in a fresh process.  This is
intentional: the legacy mailbox refreshes pagination after every move.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_PATTERN = r"aliyun\.com|阿里云"


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-move matching 263 Inbox mail to Deleted")
    parser.add_argument("--sender-regex", default=DEFAULT_PATTERN)
    parser.add_argument("--max-delete", type=int, default=1200)
    parser.add_argument("--max-passes", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=180, help="timeout per mailbox session in seconds")
    parser.add_argument("--session-retries", type=int, default=3, help="retries for pre-delete mailbox initialization failures")
    parser.add_argument("--preview-only", action="store_true")
    args = parser.parse_args()
    if args.max_delete < 1 or args.max_passes < 1:
        parser.error("--max-delete and --max-passes must be positive")
    if args.session_retries < 1:
        parser.error("--session-retries must be positive")
    try:
        re.compile(args.sender_regex, re.IGNORECASE)
    except re.error as exc:
        parser.error(f"invalid --sender-regex: {exc}")

    repo_root = Path(__file__).resolve().parent.parent
    delete_script = str(repo_root / ".codex/skills/carher-263-webmail/scripts/delete-263-mail.py")
    mode = "preview" if args.preview_only else "apply"
    for pass_number in range(1, args.max_passes + 1):
        command = [sys.executable, delete_script, "--sender-regex", args.sender_regex, "--max-delete", str(args.max_delete)]
        if not args.preview_only:
            command.append("--apply")
        print(f"cleanup: pass {pass_number}/{args.max_passes} ({mode})", flush=True)
        for attempt in range(1, args.session_retries + 1):
            try:
                result = subprocess.run(command, text=True, capture_output=True, timeout=args.timeout)
            except subprocess.TimeoutExpired as exc:
                print(f"cleanup: pass timed out after {args.timeout}s; stopping", file=sys.stderr)
                if exc.stdout:
                    print(exc.stdout, end="")
                if exc.stderr:
                    print(exc.stderr, end="", file=sys.stderr)
                return 124
            output = result.stdout + result.stderr
            print(output, end="")
            if result.returncode == 0:
                break
            transient = re.search(r"mailbox surface|treeBox|Frame was detached|tabsHome", output, re.I)
            if not transient or attempt == args.session_retries:
                return result.returncode
            print(f"cleanup: transient mailbox initialization failure; retry {attempt + 1}/{args.session_retries}", flush=True)
        if args.preview_only:
            return 0
        if re.search(r"moved to Deleted: 0 messages", output):
            print("cleanup: Inbox has no matching messages", flush=True)
            return 0
        if not re.search(r"moved to Deleted: [1-9][0-9]* messages", output):
            print("cleanup: no successful move count found; stopping", file=sys.stderr)
            return 2
    print("cleanup: pass limit reached; rerun after inspecting the last output", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
