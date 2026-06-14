"""Tests for batch fusion diagnosis evidence collection helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "carher_fusion_diagnosis_batch.py"
SPEC = importlib.util.spec_from_file_location("carher_fusion_diagnosis_batch", SCRIPT_PATH)
batch = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(batch)


def test_parse_uid_spec_supports_ranges_and_dedupes():
    assert batch.parse_uid_spec("3,1-2,2,5") == [1, 2, 3, 5]


def test_alias_info_supports_object_map():
    alias_map = {
        "3": {
            "her_id": "carher-3",
            "owner_aliases": ["owner-a"],
            "bot_aliases": ["bot-a", "bot-b"],
        }
    }

    her_id, owner_aliases, bot_aliases = batch.alias_info(alias_map, 3)

    assert her_id == "carher-3"
    assert owner_aliases == ["owner-a"]
    assert bot_aliases == ["bot-a", "bot-b"]


def test_compact_record_keeps_scoring_evidence_and_warnings():
    data = {
        "period": {"start": "2026-05-01 00:00:00", "end": "2026-06-13 00:00:00"},
        "pod": "carher-3-demo",
        "container": "carher",
        "privacy": {"raw_chat_content_included": False},
        "scoring_evidence": {
            "coverage_summary": {
                "group_recent_messages": 0,
                "group_bot_mentions": 0,
                "bot_aliases_configured": False,
                "owner_aliases_configured": True,
                "memory_status": "available",
            },
            "B_fusion_quality": {
                "B1_topic_depth": {
                    "topic_strength_top": [{"keyword": "质量", "weighted_evidence": 10}]
                }
            },
        },
    }

    record = batch.compact_record(3, "carher-3", data)

    assert record["uid"] == 3
    assert record["status"] == "ok"
    assert record["base_metrics"]["group_recent_messages"] == 0
    assert record["topic_strength_top"][0]["keyword"] == "质量"
    assert "bot_alias_missing_mentions_unavailable" in record["data_quality_warnings"]


def test_summarize_builds_cross_her_baselines():
    records = [
        {"status": "ok", "base_metrics": {"group_recent_messages": 10, "group_bot_mentions": 1, "group_active_days": 2, "groups_with_recent_messages": 1, "files_recent_count": 5, "files_recent_active_days": 1, "memory_files": 10, "memory_chunks": 100}},
        {"status": "ok", "base_metrics": {"group_recent_messages": 30, "group_bot_mentions": 3, "group_active_days": 4, "groups_with_recent_messages": 2, "files_recent_count": 15, "files_recent_active_days": 3, "memory_files": 20, "memory_chunks": 200}},
        {"status": "error"},
    ]

    summary = batch.summarize(records)

    assert summary["record_count"] == 3
    assert summary["ok_count"] == 2
    assert summary["error_count"] == 1
    assert summary["metrics"]["group_recent_messages"]["p50"] == 20
    assert summary["metrics"]["group_recent_messages"]["p25"] == 15
    assert summary["metrics"]["files_recent_count"]["max"] == 15


def test_add_relative_metrics_adds_percentile_tiers():
    records = [
        {"status": "ok", "base_metrics": {"group_recent_messages": 10, "files_recent_count": 5}},
        {"status": "ok", "base_metrics": {"group_recent_messages": 30, "files_recent_count": 15}},
        {"status": "error"},
    ]
    summary = batch.summarize(records)

    batch.add_relative_metrics(records, summary)

    assert records[0]["relative_metrics"]["group_recent_messages"]["tier"] == "below_p50"
    assert records[1]["relative_metrics"]["group_recent_messages"]["tier"] == "p90_plus"
    assert records[1]["relative_metrics"]["files_recent_count"]["percentile_rank"] == 0.75
    assert records[0]["relative_metrics"]["memory_chunks"]["tier"] == "no_signal"


def test_build_stats_cmd_passes_batch_aliases():
    args = batch.parse_args([
        "--uids",
        "3",
        "--start",
        "2026-05-01 00:00:00",
        "--end",
        "2026-06-13 00:00:00",
        "--output",
        "/tmp/out.jsonl",
        "--keyword",
        "现金流",
    ])

    cmd = batch.build_stats_cmd(args, 3, ["owner-a"], ["bot-a"])

    assert "--owner-alias" in cmd
    assert "owner-a" in cmd
    assert "--bot-alias" in cmd
    assert "bot-a" in cmd
    assert "--keyword" in cmd
    assert "现金流" in cmd
