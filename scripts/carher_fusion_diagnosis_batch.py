#!/usr/bin/env python3
"""Batch collect compact fusion diagnosis evidence for many CarHer instances.

This wrapper calls carher_fusion_diagnosis_stats.py once per Her and writes one
privacy-safe compact JSON object per line. The compact objects are intended as
the only model input for batch scoring/report generation.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import subprocess
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
STATS_SCRIPT = SCRIPT_DIR / "carher_fusion_diagnosis_stats.py"
DEFAULT_LOW_CONFIDENCE_FIELDS = ["B3", "C1", "C2", "C3", "D1", "D2"]
BASE_METRIC_FIELDS = [
    "group_recent_messages",
    "group_bot_mentions",
    "group_active_days",
    "groups_with_recent_messages",
    "files_recent_count",
    "files_recent_active_days",
    "workspace_recent_files",
    "feishu_groups_recent_files",
    "memory_recent_files",
    "logs_recent_files",
    "memory_files",
    "memory_chunks",
    "memory_fts_chunks",
    "embedding_cache",
]


def parse_uid_spec(value: str) -> list[int]:
    """Parse uid specs like '1-3,5,8' into sorted unique ints."""
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            if end < start:
                raise ValueError(f"Invalid uid range: {part}")
            result.update(range(start, end + 1))
        else:
            result.add(int(part))
    return sorted(result)


def load_alias_map(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        return {str(item["uid"]): item for item in data}
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    raise ValueError("owner/bot map must be a JSON object or list")


def listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)]


def alias_info(alias_map: dict[str, Any], uid: int) -> tuple[str, list[str], list[str]]:
    item = alias_map.get(str(uid)) or {}
    her_id = str(item.get("her_id") or item.get("name") or f"carher-{uid}")
    owner_aliases = listify(item.get("owner_aliases") or item.get("owner_alias") or item.get("owner_name"))
    bot_aliases = listify(item.get("bot_aliases") or item.get("bot_alias") or item.get("bot_name"))
    return her_id, owner_aliases, bot_aliases


def int_metric(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def data_quality_warnings(coverage: dict[str, Any]) -> list[str]:
    warnings = []
    if not coverage.get("bot_aliases_configured"):
        warnings.append("bot_alias_missing_mentions_unavailable")
    if not coverage.get("owner_aliases_configured"):
        warnings.append("owner_alias_missing_owner_message_count_unavailable")
    if coverage.get("memory_status") != "available":
        warnings.append("memory_db_unavailable")
    if not coverage.get("group_recent_messages"):
        warnings.append("no_group_messages_in_period")
    return warnings


def top_topics(scoring: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    return (
        (scoring.get("B_fusion_quality") or {})
        .get("B1_topic_depth", {})
        .get("topic_strength_top", [])
    )[:limit]


def build_base_metrics(uid: int, her_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Her's evidence into the stable table/report input shape."""
    scoring = data.get("scoring_evidence") or {}
    coverage = scoring.get("coverage_summary") or {}
    files = data.get("files") or {}
    recent_by_area = files.get("recent_by_area") or {}
    memory_tables = (data.get("memory_db") or {}).get("tables") or {}
    topics = top_topics(scoring, 10)

    metrics = {
        "uid": uid,
        "her_id": her_id,
        "period": data.get("period"),
        "pod": data.get("pod"),
        "container": data.get("container"),
        "bot_aliases_configured": bool(coverage.get("bot_aliases_configured")),
        "owner_aliases_configured": bool(coverage.get("owner_aliases_configured")),
        "group_recent_messages": int_metric(coverage.get("group_recent_messages")),
        "group_bot_mentions": int_metric(coverage.get("group_bot_mentions")),
        "group_active_days": int_metric(coverage.get("group_active_days")),
        "groups_with_recent_messages": int_metric(coverage.get("groups_with_recent_messages")),
        "files_recent_count": int_metric(coverage.get("files_recent_count")),
        "files_recent_active_days": int_metric(coverage.get("files_recent_active_days")),
        "workspace_recent_files": int_metric(recent_by_area.get("workspace")),
        "feishu_groups_recent_files": int_metric(recent_by_area.get("feishu-groups")),
        "memory_recent_files": int_metric(recent_by_area.get("memory")),
        "logs_recent_files": int_metric(recent_by_area.get("logs")),
        "memory_status": coverage.get("memory_status"),
        "memory_files": int_metric(memory_tables.get("files")),
        "memory_chunks": int_metric(memory_tables.get("chunks")),
        "memory_fts_chunks": int_metric(memory_tables.get("chunks_fts")),
        "embedding_cache": int_metric(memory_tables.get("embedding_cache")),
        "topic_strength_top": topics,
        "topic_summary": ", ".join(
            f"{item.get('keyword')}:{int_metric(item.get('weighted_evidence'))}"
            for item in topics[:8]
        ),
        "data_quality_warnings": data_quality_warnings(coverage),
        "low_confidence_fields": DEFAULT_LOW_CONFIDENCE_FIELDS,
        "privacy": data.get("privacy") or scoring.get("privacy") or {},
    }
    return metrics


def build_stats_cmd(args: argparse.Namespace, uid: int, owner_aliases: list[str], bot_aliases: list[str]) -> list[str]:
    cmd = [
        sys.executable,
        str(STATS_SCRIPT),
        "--uid",
        str(uid),
        "--start",
        args.start,
        "--end",
        args.end,
        "--namespace",
        args.namespace,
        "--container",
        args.container,
        "--kubeconfig",
        args.kubeconfig,
        "--openclaw-base",
        args.openclaw_base,
        "--max-text-bytes",
        str(args.max_text_bytes),
    ]
    for keyword in args.keyword:
        cmd.extend(["--keyword", keyword])
    for alias in owner_aliases:
        cmd.extend(["--owner-alias", alias])
    for alias in bot_aliases:
        cmd.extend(["--bot-alias", alias])
    return cmd


def compact_record(uid: int, her_id: str, data: dict[str, Any]) -> dict[str, Any]:
    scoring = data.get("scoring_evidence") or {}
    coverage = scoring.get("coverage_summary") or {}
    base_metrics = build_base_metrics(uid, her_id, data)
    warnings = base_metrics["data_quality_warnings"]

    return {
        "uid": uid,
        "her_id": her_id,
        "status": "ok",
        "period": data.get("period"),
        "pod": data.get("pod"),
        "container": data.get("container"),
        "base_metrics": base_metrics,
        "coverage_summary": coverage,
        "scoring_evidence": scoring,
        "topic_strength_top": base_metrics["topic_strength_top"],
        "low_confidence_fields": DEFAULT_LOW_CONFIDENCE_FIELDS,
        "data_quality_warnings": warnings,
        "privacy": data.get("privacy") or scoring.get("privacy") or {},
    }


def error_record(uid: int, her_id: str, error: str) -> dict[str, Any]:
    return {
        "uid": uid,
        "her_id": her_id,
        "status": "error",
        "error": error[:1000],
        "low_confidence_fields": ["A", "B", "C", "D"],
        "data_quality_warnings": ["collection_failed"],
    }


def collect_one(args: argparse.Namespace, uid: int, alias_map: dict[str, Any]) -> dict[str, Any]:
    her_id, owner_aliases, bot_aliases = alias_info(alias_map, uid)
    cmd = build_stats_cmd(args, uid, owner_aliases, bot_aliases)
    try:
        proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
        return compact_record(uid, her_id, json.loads(proc.stdout))
    except subprocess.CalledProcessError as exc:
        detail = "\n".join(part for part in [exc.stderr, exc.stdout, str(exc)] if part)
        return error_record(uid, her_id, detail)
    except Exception as exc:
        return error_record(uid, her_id, str(exc))


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = (len(values) - 1) * pct
    lower = int(idx)
    upper = min(lower + 1, len(values) - 1)
    weight = idx - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def percentile_rank(values: list[float], value: float) -> float | None:
    if not values:
        return None
    below = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return round((below + 0.5 * equal) / len(values), 4)


def tier_for(value: float, thresholds: dict[str, float | int | None]) -> str:
    if not thresholds.get("nonzero_count") and value == 0:
        return "no_signal"
    p50 = thresholds.get("p50")
    p75 = thresholds.get("p75")
    p90 = thresholds.get("p90")
    if p90 is not None and value >= float(p90):
        return "p90_plus"
    if p75 is not None and value >= float(p75):
        return "p75_p90"
    if p50 is not None and value >= float(p50):
        return "p50_p75"
    return "below_p50"


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, dict[str, float | int | None]] = {}
    ok_records = [record for record in records if record.get("status") == "ok"]
    for field_name in BASE_METRIC_FIELDS:
        values = [
            float((record.get("base_metrics") or {}).get(field_name) or 0)
            for record in ok_records
        ]
        metrics[field_name] = {
            "count": len(values),
            "nonzero_count": sum(1 for value in values if value > 0),
            "min": min(values) if values else None,
            "p25": percentile(values, 0.25),
            "p50": median(values) if values else None,
            "p75": percentile(values, 0.75),
            "p90": percentile(values, 0.90),
            "max": max(values) if values else None,
            "mean": round(mean(values), 2) if values else None,
        }
    warning_counts: dict[str, int] = {}
    for record in records:
        for warning in record.get("data_quality_warnings") or []:
            warning_counts[warning] = warning_counts.get(warning, 0) + 1
    return {
        "schema_version": "2026-06-13.2",
        "record_count": len(records),
        "ok_count": len(ok_records),
        "error_count": len(records) - len(ok_records),
        "data_quality_warning_counts": dict(sorted(warning_counts.items())),
        "metrics": metrics,
        "scoring_note": "Use these batch baselines and per-record relative_metrics for cross-Her normalization before generating final reports.",
    }


def add_relative_metrics(records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    ok_records = [record for record in records if record.get("status") == "ok"]
    values_by_metric = {
        field: [float((record.get("base_metrics") or {}).get(field) or 0) for record in ok_records]
        for field in BASE_METRIC_FIELDS
    }
    metric_summary = summary.get("metrics") or {}
    for record in records:
        if record.get("status") != "ok":
            continue
        base_metrics = record.get("base_metrics") or {}
        relative = {}
        for field in BASE_METRIC_FIELDS:
            value = float(base_metrics.get(field) or 0)
            thresholds = metric_summary.get(field) or {}
            relative[field] = {
                "value": int(value) if value.is_integer() else value,
                "percentile_rank": percentile_rank(values_by_metric[field], value),
                "tier": tier_for(value, thresholds),
            }
        record["relative_metrics"] = relative


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uids", required=True, help="Comma/range uid spec, e.g. 1-10,15")
    parser.add_argument("--start", required=True, help="Asia/Shanghai start time")
    parser.add_argument("--end", required=True, help="Asia/Shanghai exclusive end time")
    parser.add_argument("--owner-bot-map", help="JSON mapping uid -> owner_aliases/bot_aliases/her_id")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument("--summary-output", help="Optional batch summary JSON path")
    parser.add_argument("--checkpoint-output", help="Optional incremental raw JSONL checkpoint path. Defaults to <output>.checkpoint")
    parser.add_argument("--namespace", default="carher")
    parser.add_argument("--container", default="carher")
    parser.add_argument("--kubeconfig", default="~/.kube/config")
    parser.add_argument("--openclaw-base", default="/data/.openclaw")
    parser.add_argument("--keyword", action="append", default=[], help="Extra keyword to count. Can repeat.")
    parser.add_argument("--max-text-bytes", type=int, default=5_000_000)
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent Her collectors.")
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    uids = parse_uid_spec(args.uids)
    alias_map = load_alias_map(args.owner_bot_map)
    records = []
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = Path(args.checkpoint_output) if args.checkpoint_output else output_path.with_suffix(output_path.suffix + ".checkpoint")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    completed = 0
    workers = max(1, int(args.workers or 1))
    with checkpoint_path.open("w") as checkpoint:
        if workers == 1:
            for uid in uids:
                print(f"[{completed + 1}/{len(uids)}] collecting carher-{uid}", file=sys.stderr, flush=True)
                record = collect_one(args, uid, alias_map)
                completed += 1
                if record.get("status") == "error":
                    print(f"[{completed}/{len(uids)}] {record['her_id']} error: {record['error'][:240]}", file=sys.stderr, flush=True)
                else:
                    print(f"[{completed}/{len(uids)}] {record['her_id']} ok", file=sys.stderr, flush=True)
                records.append(record)
                checkpoint.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                checkpoint.flush()
                if args.fail_fast and record.get("status") == "error":
                    break
        else:
            print(f"collecting {len(uids)} Hers with workers={workers}", file=sys.stderr, flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(collect_one, args, uid, alias_map): uid for uid in uids}
                for future in as_completed(futures):
                    record = future.result()
                    completed += 1
                    if record.get("status") == "error":
                        print(f"[{completed}/{len(uids)}] {record['her_id']} error: {record['error'][:240]}", file=sys.stderr, flush=True)
                    else:
                        print(f"[{completed}/{len(uids)}] {record['her_id']} ok", file=sys.stderr, flush=True)
                    records.append(record)
                    checkpoint.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    checkpoint.flush()
                    if args.fail_fast and record.get("status") == "error":
                        break

    records.sort(key=lambda record: int(record.get("uid") or 0))
    summary = summarize(records)
    add_relative_metrics(records, summary)
    with output_path.open("w") as out:
        for record in records:
            out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    if args.summary_output:
        summary_path = Path(args.summary_output)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if args.fail_fast and any(record.get("status") == "error" for record in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
