#!/usr/bin/env python3
"""Merge CarHer fusion evidence JSONL files and fill missing/error rows.

This script avoids re-reading K8s/PVC when a batch already produced usable
compact evidence. It prefers successful rows from later inputs, then emits
explicit fallback rows for remaining missing/error Hers so downstream scoring
and Feishu Base sync cover the full requested population without inventing data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import carher_fusion_diagnosis_batch as batch


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def choose_record(existing: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return candidate
    if candidate.get("status") == "ok" and existing.get("status") != "ok":
        return candidate
    if candidate.get("status") == "ok" and existing.get("status") == "ok":
        return candidate
    return existing


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="Evidence JSONL. Later ok rows override earlier rows.")
    parser.add_argument("--uids", required=True, help="Comma/range uid spec, e.g. 1-500")
    parser.add_argument("--start", required=True, help="Asia/Shanghai start time")
    parser.add_argument("--end", required=True, help="Asia/Shanghai exclusive end time")
    parser.add_argument("--owner-bot-map", help="JSON mapping uid -> owner_aliases/bot_aliases/her_id")
    parser.add_argument("--output", required=True, help="Merged output JSONL path")
    parser.add_argument("--summary-output", help="Merged summary JSON path")
    parser.add_argument("--fallback-errors", action="store_true", help="Convert missing/error rows to explicit no-evidence ok rows.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    wanted_uids = batch.parse_uid_spec(args.uids)
    alias_map = batch.load_alias_map(args.owner_bot_map)
    by_uid: dict[int, dict[str, Any]] = {}

    for input_path in args.input:
        for record in load_jsonl(Path(input_path)):
            try:
                uid = int(record.get("uid") or 0)
            except (TypeError, ValueError):
                continue
            if uid not in wanted_uids:
                continue
            by_uid[uid] = choose_record(by_uid.get(uid), record)

    shim_args = argparse.Namespace(start=args.start, end=args.end, container="carher")
    records: list[dict[str, Any]] = []
    for uid in wanted_uids:
        her_id, _, _ = batch.alias_info(alias_map, uid)
        record = by_uid.get(uid)
        if record and record.get("status") == "ok":
            records.append(record)
            continue
        if args.fallback_errors:
            error = (record or {}).get("error") or "missing compact evidence row"
            records.append(batch.fallback_record(shim_args, uid, her_id, str(error)))
        elif record:
            records.append(record)
        else:
            records.append(batch.error_record(uid, her_id, "missing compact evidence row"))

    records.sort(key=lambda item: int(item.get("uid") or 0))
    summary = batch.summarize(records)
    batch.add_relative_metrics(records, summary)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records))

    if args.summary_output:
        summary_path = Path(args.summary_output)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
