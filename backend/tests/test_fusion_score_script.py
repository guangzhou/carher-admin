"""Tests for system evidence scoring from batch fusion metrics."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "carher_fusion_score.py"
SPEC = importlib.util.spec_from_file_location("carher_fusion_score", SCRIPT_PATH)
score = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(score)


def record(uid: int, group_tier: str, files_tier: str, memory_tier: str, topic_weight: int) -> dict:
    return {
        "uid": uid,
        "her_id": f"carher-{uid}",
        "status": "ok",
        "base_metrics": {
            "group_recent_messages": {"p90_plus": 3000, "p75_p90": 1500, "p50_p75": 500, "below_p50": 10, "no_signal": 0}[group_tier],
            "group_bot_mentions": 10 if group_tier != "no_signal" else 0,
            "group_active_days": 20 if group_tier in {"p75_p90", "p90_plus"} else 3,
            "groups_with_recent_messages": 8 if group_tier in {"p75_p90", "p90_plus"} else 1,
            "files_recent_count": {"p90_plus": 6000, "p75_p90": 3000, "p50_p75": 500, "below_p50": 20, "no_signal": 0}[files_tier],
            "files_recent_active_days": 30 if files_tier in {"p75_p90", "p90_plus"} else 2,
            "workspace_recent_files": 100 if files_tier != "no_signal" else 0,
            "feishu_groups_recent_files": 2,
            "logs_recent_files": 1,
            "memory_chunks": {"p90_plus": 9000, "p75_p90": 5000, "p50_p75": 2000, "below_p50": 100, "no_signal": 0}[memory_tier],
            "topic_strength_top": [
                {"keyword": "质量", "weighted_evidence": topic_weight},
                {"keyword": "模板", "weighted_evidence": topic_weight},
                {"keyword": "纠偏", "weighted_evidence": topic_weight},
                {"keyword": "自诊断", "weighted_evidence": topic_weight},
            ],
            "topic_summary": f"质量:{topic_weight}",
            "data_quality_warnings": ["owner_alias_missing_owner_message_count_unavailable"],
        },
        "relative_metrics": {
            "group_recent_messages": {"tier": group_tier},
            "group_active_days": {"tier": group_tier},
            "groups_with_recent_messages": {"tier": group_tier},
            "files_recent_count": {"tier": files_tier},
            "workspace_recent_files": {"tier": files_tier},
            "memory_chunks": {"tier": memory_tier},
        },
    }


def test_score_record_differentiates_high_and_low_evidence():
    high = score.score_record(record(1, "p90_plus", "p90_plus", "p90_plus", 2000))
    low = score.score_record(record(2, "below_p50", "below_p50", "below_p50", 0))

    assert high["scores"]["total"] > low["scores"]["total"]
    assert high["grade"] in {"S·深度融合", "A·主动协作"}
    assert low["grade"] in {"B·日常使用", "C·初步接入", "D·浅层接触"}
    assert high["c2_adoption_pending"] is True
    assert high["score_type"] == "system_evidence_score"
    assert high["owner_review_required_fields"] == ["C1", "C2", "C3"]
    assert "组织传导" in high["owner_review_note"]
    assert "C2" in high["low_confidence_fields"]


def test_confidence_is_medium_when_owner_alias_missing():
    scored = score.score_record(record(3, "p75_p90", "p50_p75", "p50_p75", 500))

    assert scored["confidence"] == "中"
    assert "owner_alias_missing_owner_message_count_unavailable" in scored["notes"]
