#!/usr/bin/env python3
"""Sync CarHer fusion system scores into the Feishu Base check table.

The script is deliberately data-driven: it reads scored JSONL, resolves current
Base rows by Her ID, then updates existing records and creates missing records.
It preserves existing report links unless a caller explicitly supplies them in
the score input.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_BASE_TOKEN = "GBRPbLmFZaut74s8iFkcis8xnae"
DEFAULT_TABLE_ID = "tblHoikuAaekwki5"
DEFAULT_PERIOD_START = "2026-05-01 00:00:00"
DEFAULT_PERIOD_END = "2026-06-12 23:59:59"

FIELD_ORDER = [
    "Her ID",
    "Her 名称",
    "检查周期开始",
    "检查周期结束",
    "A1 交互频次",
    "A2 场景覆盖",
    "A3 时间分布",
    "A4 信息录入",
    "A5 群聊覆盖",
    "A 连接强度",
    "B1 课题深度",
    "B2 数据支撑度",
    "B3 认知增量归因",
    "B4 元认知讨论",
    "B 融合质量",
    "C1 不可替代产出",
    "C2 组织传导率",
    "C3 认知时间释放",
    "C 融合效果",
    "D1 月度能力扩展",
    "D2 失败修复率",
    "D 进化能力",
    "总分",
    "等级",
    "置信度",
    "C2 组织采纳待确认",
    "数据源摘要",
    "备注",
    "报告链接",
]


def run_json(cmd: list[str], cwd: Path | None = None) -> dict[str, Any]:
    proc = subprocess.run(cmd, text=True, capture_output=True, cwd=str(cwd) if cwd else None)
    if proc.returncode:
        raise RuntimeError(proc.stderr or proc.stdout)
    data = json.loads(proc.stdout)
    if not data.get("ok"):
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_alias_map(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        iterable = data.values()
    else:
        iterable = data
    result = {}
    for item in iterable:
        her_id = str(item.get("her_id") or f"carher-{item.get('uid')}")
        aliases = item.get("bot_aliases") or []
        result[her_id] = str(aliases[0] if aliases else her_id)
    return result


def list_existing(base_token: str, table_id: str) -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
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
            "--field-id",
            "报告链接",
        ])
        payload = data["data"]
        fields = payload["fields"]
        idx = {field: i for i, field in enumerate(fields)}
        for record_id, row in zip(payload["record_id_list"], payload["data"]):
            her_id = row[idx["Her ID"]] if idx.get("Her ID") is not None and idx["Her ID"] < len(row) else None
            if her_id:
                existing[str(her_id)] = {
                    "record_id": record_id,
                    "report_link": row[idx["报告链接"]] if idx.get("报告链接") is not None and idx["报告链接"] < len(row) else None,
                }
        if not payload.get("has_more"):
            break
        offset += 200
    return existing


def score_fields(score: dict[str, Any], name_by_her: dict[str, str], include_report_link: bool) -> dict[str, Any]:
    scores = score["scores"]
    her_id = str(score["her_id"])
    row = {
        "Her ID": her_id,
        "Her 名称": name_by_her.get(her_id, her_id),
        "检查周期开始": DEFAULT_PERIOD_START,
        "检查周期结束": DEFAULT_PERIOD_END,
        "A1 交互频次": scores["A1"],
        "A2 场景覆盖": scores["A2"],
        "A3 时间分布": scores["A3"],
        "A4 信息录入": scores["A4"],
        "A5 群聊覆盖": scores["A5"],
        "A 连接强度": scores["A_total"],
        "B1 课题深度": scores["B1"],
        "B2 数据支撑度": scores["B2"],
        "B3 认知增量归因": scores["B3"],
        "B4 元认知讨论": scores["B4"],
        "B 融合质量": scores["B_total"],
        "C1 不可替代产出": scores["C1"],
        "C2 组织传导率": scores["C2"],
        "C3 认知时间释放": scores["C3"],
        "C 融合效果": scores["C_total"],
        "D1 月度能力扩展": scores["D1"],
        "D2 失败修复率": scores["D2"],
        "D 进化能力": scores["D_total"],
        "总分": scores["total"],
        "等级": [score["grade"]],
        "置信度": [score["confidence"]],
        "C2 组织采纳待确认": bool(score["c2_adoption_pending"]),
        "数据源摘要": str(score["data_source_summary"])[:1800],
        "备注": str(score["notes"])[:1800],
    }
    if include_report_link:
        row["报告链接"] = score.get("report_link") or None
    return row


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def sync(args: argparse.Namespace) -> dict[str, Any]:
    scores = load_jsonl(Path(args.scores))
    name_by_her = load_alias_map(Path(args.alias_map) if args.alias_map else None)
    existing = list_existing(args.base_token, args.table_id)

    updates = []
    creates = []
    for score in scores:
        her_id = str(score["her_id"])
        current = existing.get(her_id)
        if current:
            updates.append({
                "record_id": current["record_id"],
                "fields": score_fields(score, name_by_her, include_report_link=False),
            })
        else:
            creates.append(score_fields(score, name_by_her, include_report_link=True))

    out_dir = Path(args.audit_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "planned-updates.json").write_text(json.dumps(updates, ensure_ascii=False, indent=2))
    (out_dir / "planned-creates.json").write_text(json.dumps(creates, ensure_ascii=False, indent=2))

    if args.dry_run:
        return {"updates": len(updates), "creates": len(creates), "dry_run": True}

    updated = 0
    for item in updates:
        run_json([
            "lark-cli",
            "base",
            "+record-upsert",
            "--base-token",
            args.base_token,
            "--table-id",
            args.table_id,
            "--record-id",
            item["record_id"],
            "--json",
            json.dumps(item["fields"], ensure_ascii=False),
            "--format",
            "json",
        ])
        updated += 1

    created = 0
    for batch_index, rows in enumerate(chunked(creates, args.batch_size), start=1):
        body = {"fields": FIELD_ORDER, "rows": [[row.get(field) for field in FIELD_ORDER] for row in rows]}
        body_path = out_dir / f"create-batch-{batch_index}.json"
        body_path.write_text(json.dumps(body, ensure_ascii=False))
        data = run_json([
            "lark-cli",
            "base",
            "+record-batch-create",
            "--base-token",
            args.base_token,
            "--table-id",
            args.table_id,
            "--json",
            f"@{body_path.name}",
            "--format",
            "json",
        ], cwd=out_dir)
        record_ids = data.get("data", {}).get("record_id_list") or []
        created += len(record_ids)
        (out_dir / f"create-batch-{batch_index}.result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
        if args.sleep_seconds and batch_index < len(chunked(creates, args.batch_size)):
            time.sleep(args.sleep_seconds)

    return {"updates": len(updates), "updated": updated, "creates": len(creates), "created": created, "dry_run": False}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", required=True)
    parser.add_argument("--alias-map")
    parser.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    parser.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    parser.add_argument("--audit-dir", default="/tmp/carher-fusion-runs/score-table-sync")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--sleep-seconds", type=float, default=1.5)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    result = sync(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
