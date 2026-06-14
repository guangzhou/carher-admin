"""Tests for the privacy-safe fusion diagnosis stats helper script."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "carher_fusion_diagnosis_stats.py"
SPEC = importlib.util.spec_from_file_location("carher_fusion_diagnosis_stats", SCRIPT_PATH)
stats = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(stats)


def test_sanitize_label_redacts_tokens_and_long_ids():
    text = "cli_a917fcf08db81bb6 ou_286e0c02809bbbfe80a747c18cd947d2 abcdef1234567890abcdef1234567890"

    sanitized = stats.sanitize_label(text)

    assert "cli_a917" not in sanitized
    assert "ou_286" not in sanitized
    assert "abcdef1234567890abcdef1234567890" not in sanitized
    assert sanitized.count("[redacted]") >= 2


def test_default_keywords_include_report_alignment_terms():
    for keyword in ["AI KPI", "-1层", "ICTS", "IAA", "罗马尼亚", "质量体系", "自诊断", "模板", "纠偏"]:
        assert keyword in stats.DEFAULT_KEYWORDS


def test_build_kubectl_cmd_uses_read_only_exec_parameters():
    args = stats.parse_args([
        "--uid",
        "3",
        "--start",
        "2026-06-07 00:00:00",
        "--end",
        "2026-06-13 00:00:00",
        "--pod",
        "carher-3-demo",
    ])

    cmd = stats.build_kubectl_cmd(args, "carher-3-demo")

    assert cmd[:5] == ["kubectl", "--kubeconfig", "~/.kube/config", "-n", "carher"]
    assert "exec" in cmd
    assert "-c" in cmd
    assert "carher" in cmd
    assert "python3" in cmd
    assert "delete" not in cmd
    assert "apply" not in cmd


def test_build_scoring_evidence_groups_model_inputs_by_score_section():
    data = {
        "period": {"start": "2026-05-01 00:00:00", "end": "2026-06-13 00:00:00"},
        "pod": "carher-3-demo",
        "container": "carher",
        "files": {
            "recent_count": 12,
            "recent_active_days": 3,
            "recent_by_area": {"workspace": 8, "memory": 2, "logs": 1, "sessions": 1},
            "recent_by_day": {"2026-05-01": 5, "2026-05-02": 4, "2026-05-03": 3},
            "recent_text_candidate_count": 10,
            "recent_size_bytes_total": 12345,
            "recent_ext_counts": {".md": 8, ".json": 2},
            "recent_top_dirs": [{"path": "workspace/outputs", "count": 4}],
        },
        "feishu_group_cache": {
            "total_recent_messages": 20,
            "groups_with_recent_messages": 3,
            "owner_aliases_configured": True,
            "bot_aliases_configured": True,
            "bot_mentions": 5,
            "messages_by_day": {"2026-05-01": 12, "2026-05-02": 8},
            "bot_mentions_by_day": {"2026-05-01": 5},
            "keyword_counts": {"质量": 6, "纠偏": 2},
        },
        "memory_db": {
            "status": "available",
            "tables": {"files": 7, "chunks": 11, "chunks_fts": 13, "embedding_cache": 17},
            "keyword_fts_hits": {"质量": {"path_count": 2, "hits": 9}, "纠偏": {"path_count": 1, "hits": 3}},
        },
        "keyword_recent_file_hits": {
            "质量": {"file_count": 4, "occurrences": 8},
            "纠偏": {"file_count": 2, "occurrences": 5},
        },
    }

    evidence = stats.build_scoring_evidence(data)

    assert evidence["schema_version"] == "2026-06-13.1"
    assert evidence["coverage_summary"]["group_recent_messages"] == 20
    assert evidence["coverage_summary"]["bot_aliases_configured"] is True
    assert evidence["A_connection_strength"]["A1_interaction_frequency"]["bot_mentions"] == 5
    assert evidence["A_connection_strength"]["A2_scenario_coverage"]["has_group_activity"] is True
    assert evidence["B_fusion_quality"]["B1_topic_depth"]["topic_strength_top"][0]["keyword"] == "质量"
    assert evidence["C_fusion_effect"]["C2_organization_propagation"]["formal_adoption_requires_review"] is True
    assert evidence["D_evolution_ability"]["D2_failure_repair"]["requires_recurrence_check"] is True


def test_build_base_metrics_flattens_all_her_table_fields():
    data = {
        "uid": 10,
        "period": {"start": "2026-05-01 00:00:00", "end": "2026-06-13 00:00:00"},
        "pod": "carher-10-demo",
        "container": "carher",
        "privacy": {"raw_chat_content_included": False},
        "files": {"recent_by_area": {"workspace": 8, "feishu-groups": 2, "logs": 1}},
        "memory_db": {
            "tables": {"files": 7, "chunks": 11, "chunks_fts": 13, "embedding_cache": 17},
        },
        "scoring_evidence": {
            "coverage_summary": {
                "group_recent_messages": 20,
                "group_bot_mentions": 5,
                "group_active_days": 2,
                "groups_with_recent_messages": 3,
                "files_recent_count": 12,
                "files_recent_active_days": 3,
                "bot_aliases_configured": True,
                "owner_aliases_configured": False,
                "memory_status": "available",
            },
            "B_fusion_quality": {
                "B1_topic_depth": {
                    "topic_strength_top": [{"keyword": "质量", "weighted_evidence": 100}]
                }
            },
        },
    }

    metrics = stats.build_base_metrics(data)

    assert metrics["her_id"] == "carher-10"
    assert metrics["group_recent_messages"] == 20
    assert metrics["workspace_recent_files"] == 8
    assert metrics["feishu_groups_recent_files"] == 2
    assert metrics["memory_chunks"] == 11
    assert metrics["topic_summary"] == "质量:100"
    assert metrics["low_confidence_fields"] == ["B3", "C1", "C2", "C3", "D1", "D2"]
    assert "owner_alias_missing_owner_message_count_unavailable" in metrics["data_quality_warnings"]


def test_build_kubectl_cmd_passes_owner_and_bot_aliases():
    args = stats.parse_args([
        "--uid",
        "3",
        "--pod",
        "carher-3-demo",
        "--owner-alias",
        "owner-a",
        "--bot-alias",
        "bot-a",
    ])

    cmd = stats.build_kubectl_cmd(args, "carher-3-demo")
    params = cmd[-1]

    assert '"owner_aliases": ["owner-a"]' in params
    assert '"bot_aliases": ["bot-a"]' in params
