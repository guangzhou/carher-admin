#!/usr/bin/env python3
"""Move generated CarHer fusion reports into a target Lark Wiki node.

The script is resumable. It consumes the report manifest produced by
carher_fusion_reports.py, optionally appends executive reports, moves each
Drive docx into Wiki under a target parent node, and can sync individual Her
report links back to the Feishu Base check table.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any


DEFAULT_BASE_TOKEN = "GBRPbLmFZaut74s8iFkcis8xnae"
DEFAULT_TABLE_ID = "tblHoikuAaekwki5"
DEFAULT_TARGET_PARENT_TOKEN = "RbOPwFrPqijcuzkpmZFc6sM4nRh"
DEFAULT_TARGET_SPACE_ID = "7611015400976846012"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def is_retryable(output: str) -> bool:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return False
    error = data.get("error") or {}
    return bool(error.get("retryable")) or error.get("subtype") == "rate_limit"


def run_json(cmd: list[str], cwd: Path | None = None, retries: int = 6) -> dict[str, Any]:
    delay = 4.0
    last_output = ""
    for attempt in range(retries + 1):
        proc = subprocess.run(cmd, text=True, capture_output=True, cwd=str(cwd) if cwd else None)
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


def report_items(args: argparse.Namespace) -> list[dict[str, Any]]:
    items = []
    for item in load_jsonl(Path(args.manifest)):
        items.append({
            "uid": item.get("uid"),
            "her_id": item["her_id"],
            "document_id": item["document_id"],
            "url": item.get("url"),
            "kind": "individual",
        })
    for extra in args.extra_doc or []:
        name, token = extra.split("=", 1)
        items.append({
            "uid": name,
            "her_id": name,
            "document_id": token,
            "url": f"https://t83dfrspj4.feishu.cn/docx/{token}",
            "kind": "executive",
        })
    return items


def wiki_url(node_token: str) -> str:
    return f"https://t83dfrspj4.feishu.cn/wiki/{node_token}"


def existing_results(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {item["her_id"]: item for item in load_jsonl(path)}


def append_result(path: Path, item: dict[str, Any]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def list_base_records(base_token: str, table_id: str) -> dict[str, str]:
    records: dict[str, str] = {}
    offset = 0
    while True:
        data = run_json([
            "lark-cli",
            "base",
            "+record-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--offset",
            str(offset),
            "--limit",
            "200",
            "--format",
            "json",
            "--field-id",
            "Her ID",
        ])
        payload = data["data"]
        fields = payload["fields"]
        idx = {field: index for index, field in enumerate(fields)}
        for record_id, row in zip(payload["record_id_list"], payload["data"]):
            her_id_idx = idx.get("Her ID")
            if her_id_idx is not None and her_id_idx < len(row) and row[her_id_idx]:
                records[str(row[her_id_idx])] = record_id
        if not payload.get("has_more"):
            break
        offset += 200
    return records


def move_one(args: argparse.Namespace, item: dict[str, Any]) -> dict[str, Any]:
    data = run_json([
        "lark-cli",
        "wiki",
        "+move",
        "--obj-type",
        "docx",
        "--obj-token",
        item["document_id"],
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
    node_token = payload.get("node_token") or payload.get("wiki_token")
    if not node_token and payload.get("node", {}).get("node_token"):
        node_token = payload["node"]["node_token"]
    return {
        **item,
        "move_result": payload,
        "node_token": node_token,
        "wiki_url": wiki_url(node_token) if node_token else None,
    }


def sync_base_link(args: argparse.Namespace, record_id: str, url: str) -> None:
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
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target-space-id", default=DEFAULT_TARGET_SPACE_ID)
    parser.add_argument("--target-parent-token", default=DEFAULT_TARGET_PARENT_TOKEN)
    parser.add_argument("--extra-doc", action="append", help="name=docx_token, repeatable.")
    parser.add_argument("--sync-base-links", action="store_true")
    parser.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    parser.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    done = existing_results(output)
    records = list_base_records(args.base_token, args.table_id) if args.sync_base_links else {}
    items = [item for item in report_items(args) if item["her_id"] not in done]
    if args.limit:
        items = items[: args.limit]

    moved = 0
    synced = 0
    for index, item in enumerate(items, start=1):
        result = move_one(args, item)
        if args.sync_base_links and item["kind"] == "individual" and result.get("wiki_url"):
            record_id = records.get(item["her_id"])
            if not record_id:
                raise RuntimeError(f"missing Base row for {item['her_id']}")
            sync_base_link(args, record_id, result["wiki_url"])
            result["base_synced"] = True
            synced += 1
        else:
            result["base_synced"] = False
        append_result(output, result)
        moved += 1
        print(json.dumps({"moved": moved, "remaining_this_run": len(items) - index, "her_id": item["her_id"], "wiki_url": result.get("wiki_url")}, ensure_ascii=False), flush=True)
        if args.sleep_seconds and index < len(items):
            time.sleep(args.sleep_seconds)

    print(json.dumps({"planned": len(items), "moved": moved, "base_synced": synced, "output": str(output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
