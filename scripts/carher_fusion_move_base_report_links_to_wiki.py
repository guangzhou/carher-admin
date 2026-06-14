#!/usr/bin/env python3
"""Move report links from the Feishu Base check table into a target Wiki node.

This script treats the Base table as the source of truth. It reads Her ID and
报告链接, moves remaining docx reports under the target Wiki node, and writes the
new Wiki URL back to the same Base record. Already-moved /wiki/ links are
recorded and skipped.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any


DEFAULT_BASE_TOKEN = "GBRPbLmFZaut74s8iFkcis8xnae"
DEFAULT_TABLE_ID = "tblHoikuAaekwki5"
DEFAULT_TARGET_PARENT_TOKEN = "RbOPwFrPqijcuzkpmZFc6sM4nRh"
DEFAULT_TARGET_SPACE_ID = "7611015400976846012"


def run_json(cmd: list[str], retries: int = 6) -> dict[str, Any]:
    delay = 4.0
    last_output = ""
    for attempt in range(retries + 1):
        proc = subprocess.run(cmd, text=True, capture_output=True)
        last_output = proc.stderr or proc.stdout
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            if data.get("ok", True):
                return data
            last_output = json.dumps(data, ensure_ascii=False)
        if attempt < retries and is_retryable(last_output):
            time.sleep(delay)
            delay = min(delay * 1.8, 60.0)
            continue
        raise RuntimeError(last_output)
    raise RuntimeError(last_output)


def is_retryable(output: str) -> bool:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return False
    error = data.get("error") or {}
    return bool(error.get("retryable")) or error.get("subtype") == "rate_limit"


def normalize_url(value: Any) -> str:
    text = str(value or "").strip()
    markdown = re.match(r"^\[(https?://[^\]]+)\]\(https?://[^)]+\)$", text)
    return markdown.group(1) if markdown else text


def token_from_url(url: str, kind: str) -> str | None:
    match = re.search(rf"/{kind}/([^/?#]+)", url)
    return match.group(1) if match else None


def wiki_url(node_token: str) -> str:
    return f"https://t83dfrspj4.feishu.cn/wiki/{node_token}"


def list_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = run_json([
            "lark-cli",
            "base",
            "+record-list",
            "--base-token",
            args.base_token,
            "--table-id",
            args.table_id,
            "--offset",
            str(offset),
            "--limit",
            "200",
            "--format",
            "json",
            "--field-id",
            "Her ID",
            "--field-id",
            "报告链接",
        ])
        payload = data["data"]
        fields = payload["fields"]
        idx = {field: index for index, field in enumerate(fields)}
        for record_id, row in zip(payload["record_id_list"], payload["data"]):
            her_idx = idx.get("Her ID")
            link_idx = idx.get("报告链接")
            her_id = row[her_idx] if her_idx is not None and her_idx < len(row) else None
            link = row[link_idx] if link_idx is not None and link_idx < len(row) else None
            if her_id:
                rows.append({"record_id": record_id, "her_id": str(her_id), "report_link": normalize_url(link)})
        if not payload.get("has_more"):
            break
        offset += 200
    return rows


def existing_results(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    results = {}
    for line in path.read_text().splitlines():
        if line.strip():
            item = json.loads(line)
            results[item["her_id"]] = item
    return results


def append_result(path: Path, item: dict[str, Any]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def move_docx(args: argparse.Namespace, docx_token: str) -> str:
    data = run_json([
        "lark-cli",
        "wiki",
        "+move",
        "--obj-type",
        "docx",
        "--obj-token",
        docx_token,
        "--target-space-id",
        args.target_space_id,
        "--target-parent-token",
        args.target_parent_token,
        "--as",
        "user",
        "--format",
        "json",
    ])
    payload = data.get("data") or data
    node_token = payload.get("node_token") or payload.get("wiki_token") or payload.get("node", {}).get("node_token")
    if not node_token:
        raise RuntimeError(json.dumps({"missing_node_token": payload}, ensure_ascii=False))
    return wiki_url(node_token)


def update_link(args: argparse.Namespace, record_id: str, url: str) -> None:
    run_json([
        "lark-cli",
        "base",
        "+record-upsert",
        "--base-token",
        args.base_token,
        "--table-id",
        args.table_id,
        "--record-id",
        record_id,
        "--json",
        json.dumps({"报告链接": url}, ensure_ascii=False),
        "--format",
        "json",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    parser.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    parser.add_argument("--target-space-id", default=DEFAULT_TARGET_SPACE_ID)
    parser.add_argument("--target-parent-token", default=DEFAULT_TARGET_PARENT_TOKEN)
    parser.add_argument("--output", default="/tmp/carher-fusion-runs/wiki-move/base-report-link-move.jsonl")
    parser.add_argument("--sleep-seconds", type=float, default=0.8)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    done = existing_results(output)
    rows = [row for row in list_rows(args) if row["her_id"] not in done]
    if args.limit:
        rows = rows[: args.limit]

    moved = 0
    skipped = 0
    failed = 0
    for index, row in enumerate(rows, start=1):
        result = {**row}
        url = row["report_link"]
        try:
            if not url:
                result.update({"status": "skipped", "reason": "missing_report_link"})
                skipped += 1
            elif token_from_url(url, "wiki"):
                result.update({"status": "skipped", "reason": "already_wiki", "wiki_url": url})
                skipped += 1
            else:
                docx_token = token_from_url(url, "docx")
                if not docx_token:
                    result.update({"status": "skipped", "reason": "unsupported_link"})
                    skipped += 1
                else:
                    new_url = move_docx(args, docx_token)
                    update_link(args, row["record_id"], new_url)
                    result.update({"status": "moved", "docx_token": docx_token, "wiki_url": new_url})
                    moved += 1
            append_result(output, result)
            print(json.dumps({"index": index, "total_this_run": len(rows), "her_id": row["her_id"], "status": result["status"], "wiki_url": result.get("wiki_url")}, ensure_ascii=False), flush=True)
            if args.sleep_seconds and index < len(rows):
                time.sleep(args.sleep_seconds)
        except Exception as exc:  # Keep a resumable audit trail.
            result.update({"status": "failed", "error": str(exc)})
            append_result(output, result)
            failed += 1
            print(json.dumps({"index": index, "total_this_run": len(rows), "her_id": row["her_id"], "status": "failed", "error": str(exc)[:300]}, ensure_ascii=False), flush=True)
            break

    print(json.dumps({"rows_this_run": len(rows), "moved": moved, "skipped": skipped, "failed": failed, "output": str(output)}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
