"""Tests for the executive fusion diagnosis summary report renderer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "carher_fusion_chairman_report.py"
SPEC = importlib.util.spec_from_file_location("carher_fusion_chairman_report", SCRIPT_PATH)
chairman = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(chairman)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def test_render_marks_c_scores_as_owner_review_required(tmp_path):
    scores = [
        {"her_id": "carher-3", "grade": "S·深度融合", "scores": {"total": 90, "A_total": 30, "B_total": 35, "C_total": 17, "D_total": 8}},
        {"her_id": "carher-10", "grade": "A·主动协作", "scores": {"total": 80, "A_total": 25, "B_total": 34, "C_total": 13, "D_total": 8}},
        {"her_id": "carher-11", "grade": "C·初步接入", "scores": {"total": 45, "A_total": 12, "B_total": 20, "C_total": 8, "D_total": 5}},
    ]
    manifest = [
        {"her_id": "carher-3", "url": "https://example.com/3"},
        {"her_id": "carher-10", "url": "https://example.com/10"},
        {"her_id": "carher-11", "url": "https://example.com/11"},
    ]
    evidence_summary = {
        "ok_count": 3,
        "record_count": 3,
        "data_quality_warning_counts": {"owner_alias_missing_owner_message_count_unavailable": 3},
        "metrics": {
            key: {"p50": 1, "p75": 2, "p90": 3, "max": 4}
            for key in [
                "group_recent_messages",
                "group_bot_mentions",
                "groups_with_recent_messages",
                "files_recent_count",
                "memory_chunks",
                "files_recent_active_days",
            ]
        },
    }
    score_summary = {"mean": 71.67, "max": 90, "min": 45}
    scores_path = tmp_path / "scores.jsonl"
    manifest_path = tmp_path / "manifest.jsonl"
    evidence_path = tmp_path / "evidence-summary.json"
    score_summary_path = tmp_path / "score-summary.json"
    write_jsonl(scores_path, scores)
    write_jsonl(manifest_path, manifest)
    write_json(evidence_path, evidence_summary)
    write_json(score_summary_path, score_summary)

    args = type("Args", (), {
        "scores": str(scores_path),
        "manifest": str(manifest_path),
        "evidence_summary": str(evidence_path),
        "score_summary": str(score_summary_path),
    })()
    xml = chairman.render(args)

    assert "董事长版 v2" in xml
    assert "C类校准" in xml
    assert "系统证据分" in xml
    assert "Her/owner" in xml
    assert "最终确认分" in xml
    assert "新增 C类复核流程" in xml
