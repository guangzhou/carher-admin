#!/usr/bin/env python3
"""
Streaming truncate for CarHer session jsonl files.

Mirrors carher's built-in `truncateSessionAfterCompaction` behaviour
(see pi-embedded-D5HWI-D1.js:30002), but runs offline and:
  - Streams the file (no full-load OOM risk on huge jsonl)
  - Always backs up the original to .pre-truncate-backup-<TS>
  - Atomic rename via .truncate-tmp -> sessionFile

Behaviour:
  - Find the LAST `type:"compaction"` entry in the branch.
  - Read its `firstKeptEntryId`.
  - DROP all `type:"message"` entries that appear before that compaction
    AND whose id != firstKeptEntryId AND were not already kept.
  - Keep: header, all compaction entries, model_change, thinking_level_change,
    custom, custom_message, session_info, label, branch_summary, and every
    entry from firstKeptEntryId onwards.
  - For dropped messages, also drop dependent label/branch_summary that
    point at them (matches carher's logic).

Usage:
  truncate_session_jsonl.py --dry-run /path/to/session.jsonl
  truncate_session_jsonl.py --apply   /path/to/session.jsonl
  truncate_session_jsonl.py --apply --no-backup /path/to/session.jsonl

Exit codes:
  0 = success (or dry-run completed)
  1 = nothing to truncate (no compaction or already at root)
  2 = file not found / parse error
  3 = rewrite failed
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def scan_branch(path):
    """First pass: stream-parse to find latest compaction + collect drop ids."""
    last_compaction = None
    last_compaction_line_no = None
    n_entries = 0
    n_messages = 0
    types = {}

    # We need to know which message ids appear BEFORE the latest compaction
    # AND are NOT kept (i.e. id != firstKeptEntryId). But to know "the latest
    # compaction", we must read once. Then second pass to actually drop.
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f):
            if not line.strip():
                continue
            n_entries += 1
            # Cheap pre-filter without full json.loads to spot compaction lines
            if '"type":"compaction"' not in line[:200]:
                # Track types cheaply
                if line_no == 0 and '"type":"session"' in line[:200]:
                    types["session"] = types.get("session", 0) + 1
                continue
            try:
                e = json.loads(line)
            except Exception as ex:
                log(f"[scan] WARN: failed to parse compaction line {line_no}: {ex}")
                continue
            if e.get("type") == "compaction":
                last_compaction = e
                last_compaction_line_no = line_no

    # Second pass: full type histogram for dry-run report
    return last_compaction, last_compaction_line_no


def stream_decide_drops(path, latest_compaction):
    """
    Second pass: walk all entries, build the set of message ids to drop.

    A message entry is dropped iff:
      - It appears before the latest_compaction in the branch
      - Its id != firstKeptEntryId
      - The compaction's firstKeptEntryId has not yet been seen
    Once we see firstKeptEntryId (or the compaction entry itself), we stop
    dropping anything else.

    Then label/branch_summary referencing dropped ids also get dropped.
    """
    first_kept = latest_compaction.get("firstKeptEntryId")
    compaction_id = latest_compaction.get("id")

    drop_ids = set()
    seen_first_kept = False
    seen_compaction = False

    # Stats
    bytes_dropped = 0
    bytes_kept = 0
    n_dropped = 0
    n_kept = 0

    # We also need a second walk for label/branch_summary deps.
    # Strategy: collect drop_ids first (single pass), then a second pass
    # to expand drop_ids with label/branch_summary references.
    # And a final pass to actually write. So 3 passes total. Acceptable.

    # ---- Pass A: identify dropped message ids ----
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            line_len = len(line)
            try:
                e = json.loads(line)
            except Exception:
                bytes_kept += line_len
                n_kept += 1
                continue

            t = e.get("type")
            eid = e.get("id")

            # If we've already passed firstKeptEntryId / compaction, keep all
            if seen_compaction:
                bytes_kept += line_len
                n_kept += 1
                continue

            if first_kept and eid == first_kept:
                seen_first_kept = True
                bytes_kept += line_len
                n_kept += 1
                continue

            if eid == compaction_id:
                seen_compaction = True
                bytes_kept += line_len
                n_kept += 1
                continue

            # Before compaction, before first_kept
            if t == "message" and not seen_first_kept:
                drop_ids.add(eid)
                bytes_dropped += line_len
                n_dropped += 1
            else:
                # Keep all non-message state entries (custom, model_change, etc)
                bytes_kept += line_len
                n_kept += 1

    # ---- Pass B: expand drop_ids with dependent label / branch_summary ----
    # (Match carher's logic: label whose targetId is dropped -> also drop;
    #  branch_summary whose parentId is dropped -> also drop.)
    extra_drops = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = e.get("type")
            if t == "label" and e.get("targetId") in drop_ids:
                extra_drops.add(e.get("id"))
            elif t == "branch_summary":
                parent_id = e.get("parentId")
                if parent_id and parent_id in drop_ids:
                    extra_drops.add(e.get("id"))
    drop_ids.update(extra_drops)

    return {
        "drop_ids": drop_ids,
        "first_kept": first_kept,
        "compaction_id": compaction_id,
        "bytes_dropped": bytes_dropped,
        "bytes_kept": bytes_kept,
        "n_dropped": n_dropped,
        "n_kept": n_kept,
    }


def write_truncated(path, drop_ids):
    """Pass C: stream copy to .truncate-tmp, skipping drop_ids."""
    tmp_path = path + ".truncate-tmp"
    bytes_written = 0
    n_written = 0

    # Re-parent: when an entry's parent is dropped, walk up to nearest kept ancestor
    # We need parentId resolution. To avoid 4-pass file read, precompute parent map
    # by reading the file once more. Acceptable on cold filesystems; uses streaming.
    parent_of = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            eid = e.get("id")
            pid = e.get("parentId")
            if eid:
                parent_of[eid] = pid

    def resolve_parent(pid):
        # Walk up while pid is in drop_ids
        while pid is not None and pid in drop_ids:
            pid = parent_of.get(pid)
        return pid

    with open(path, "r", encoding="utf-8") as fin, \
         open(tmp_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except Exception:
                # Pass through unparsable lines unchanged
                fout.write(line)
                bytes_written += len(line)
                n_written += 1
                continue
            eid = e.get("id")
            if eid in drop_ids:
                continue
            # Re-parent if parent was dropped
            pid = e.get("parentId")
            if pid in drop_ids:
                new_pid = resolve_parent(pid)
                e["parentId"] = new_pid
                out_line = json.dumps(e, ensure_ascii=False) + "\n"
            else:
                # Pass through original line bytes (no re-encode for huge entries)
                out_line = line if line.endswith("\n") else line + "\n"
            fout.write(out_line)
            bytes_written += len(out_line)
            n_written += 1

    return tmp_path, bytes_written, n_written


def fmt_bytes(n):
    if n is None:
        return "?"
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024:
            return f"{f:.1f}{u}"
        f /= 1024
    return f"{f:.1f}TB"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Scan only; don't modify anything.")
    g.add_argument("--apply", action="store_true",
                   help="Actually rewrite the file.")
    ap.add_argument("--no-backup", action="store_true",
                    help="Skip backup (default: backup to .pre-truncate-backup-<TS>).")
    ap.add_argument("--idle-secs", type=int, default=30,
                    help="Require file mtime to be older than this many seconds "
                         "before --apply (default 30). Set 0 to skip.")
    ap.add_argument("path", help="Path to session jsonl file")
    args = ap.parse_args()

    path = args.path
    if not os.path.isfile(path):
        log(f"[error] not a file: {path}")
        sys.exit(2)

    size_before = os.path.getsize(path)
    mtime = os.path.getmtime(path)
    age = time.time() - mtime

    log(f"[scan] file: {path}")
    log(f"[scan] size: {fmt_bytes(size_before)} ({size_before} bytes)")
    log(f"[scan] mtime age: {age:.0f}s ago")

    if args.apply and args.idle_secs > 0 and age < args.idle_secs:
        log(f"[error] file modified within last {args.idle_secs}s — refusing to "
            f"--apply. Wait or pass --idle-secs 0 to override.")
        sys.exit(3)

    # Pass 1: find latest compaction
    log("[scan] pass 1/3: locating latest compaction entry...")
    latest_compaction, line_no = scan_branch(path)
    if latest_compaction is None:
        log("[scan] no compaction entry found — nothing to truncate.")
        sys.exit(1)

    log(f"[scan]   compaction id={latest_compaction.get('id')} "
        f"firstKeptEntryId={latest_compaction.get('firstKeptEntryId')} "
        f"line={line_no}")

    if not latest_compaction.get("firstKeptEntryId"):
        log("[scan] compaction has no firstKeptEntryId — nothing to truncate.")
        sys.exit(1)

    # Pass 2 (composite): build drop set
    log("[scan] pass 2/3: building drop set...")
    decision = stream_decide_drops(path, latest_compaction)
    drop_ids = decision["drop_ids"]
    bytes_drop = decision["bytes_dropped"]
    n_drop = decision["n_dropped"]
    n_keep = decision["n_kept"]

    log(f"[scan] result:")
    log(f"[scan]   total entries  : {n_drop + n_keep}")
    log(f"[scan]   to drop        : {n_drop} entries (~{fmt_bytes(bytes_drop)})")
    log(f"[scan]   to keep        : {n_keep} entries")
    log(f"[scan]   estimated new  : ~{fmt_bytes(size_before - bytes_drop)}")
    log(f"[scan]   reduction      : {bytes_drop / size_before * 100:.1f}%")

    if not drop_ids:
        log("[scan] nothing to drop — already minimal.")
        sys.exit(1)

    if args.dry_run:
        log("[dry-run] no files modified.")
        return

    # Apply
    log("[apply] starting rewrite...")
    backup = None
    if not args.no_backup:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{path}.pre-truncate-backup-{ts}"
        log(f"[apply] backup: cp {path} -> {backup}")
        # Use hardlink first (instant + same inode shares blocks until rename),
        # fall back to copy if cross-fs.
        try:
            os.link(path, backup)
        except OSError:
            import shutil
            shutil.copy2(path, backup)

    log("[apply] pass 3/3: streaming rewrite...")
    tmp_path, bytes_written, n_written = write_truncated(path, drop_ids)
    size_after = os.path.getsize(tmp_path)

    log(f"[apply]   wrote {n_written} entries, {fmt_bytes(size_after)}")
    log(f"[apply] atomic rename: {tmp_path} -> {path}")
    os.replace(tmp_path, path)

    log("[apply] DONE.")
    log(f"[apply]   before: {fmt_bytes(size_before)}")
    log(f"[apply]   after : {fmt_bytes(size_after)}")
    log(f"[apply]   saved : {fmt_bytes(size_before - size_after)} "
        f"({(size_before - size_after) / size_before * 100:.1f}%)")
    if backup:
        log(f"[apply]   backup: {backup}")


if __name__ == "__main__":
    main()
