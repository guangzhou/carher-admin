"""Tests for rendering sanitized fusion diagnosis reports."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "carher_fusion_reports.py"
SPEC = importlib.util.spec_from_file_location("carher_fusion_reports", SCRIPT_PATH)
reports = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(reports)


def test_render_report_includes_scores_and_sanitized_evidence():
    score = {
        "uid": 3,
        "her_id": "carher-3",
        "period": {"start": "2026-05-01 00:00:00", "end": "2026-06-12 23:59:59"},
        "scores": {
            "A1": 9,
            "A2": 8,
            "A3": 4,
            "A4": 4,
            "A5": 4,
            "A_total": 29,
            "B1": 10,
            "B2": 8,
            "B3": 7,
            "B4": 9,
            "B_total": 34,
            "C1": 6,
            "C2": 6,
            "C3": 3,
            "C_total": 15,
            "D1": 4,
            "D2": 4,
            "D_total": 8,
            "total": 86,
        },
        "grade": "A·主动协作",
        "confidence": "中",
        "low_confidence_fields": ["B3", "C1", "C2"],
    }
    evidence = {
        "base_metrics": {
            "group_recent_messages": 100,
            "group_bot_mentions": 20,
            "group_active_days": 10,
            "groups_with_recent_messages": 5,
            "files_recent_count": 200,
            "workspace_recent_files": 180,
            "memory_chunks": 300,
            "memory_files": 30,
            "feishu_groups_recent_files": 6,
            "files_recent_active_days": 9,
            "topic_summary": "质量:1000, 模板:500",
            "topic_strength_top": [{"keyword": "质量", "weighted_evidence": 1000, "group_occurrences": 10, "recent_file_occurrences": 100, "memory_hits": 5}],
            "data_quality_warnings": ["owner_alias_missing_owner_message_count_unavailable"],
            "privacy": {"raw_chat_content_included": False, "secrets_included": False},
        },
        "relative_metrics": {
            "group_recent_messages": {"tier": "p75_p90", "percentile_rank": 0.8},
            "memory_chunks": {"tier": "p50_p75", "percentile_rank": 0.6},
        },
    }

    xml = reports.render_report(score, evidence, {"count": 260, "mean": 58.89})

    assert "carher-3 融合体自诊断报告" in xml
    assert "A. 连接强度" in xml
    assert "F. 下周期行动处方" in xml
    assert "C. 融合效果（20分，系统证据分）" in xml
    assert "C类校准口径" in xml
    assert "组织传导是否成功" in xml
    assert "最终确认分" in xml
    assert "86/100" in xml
    assert "群消息" in xml
    assert "原始聊天内容" in xml
    assert "secret" not in xml.lower()
    assert "sk-" not in xml.lower()
    assert "request_id" not in xml.lower()
