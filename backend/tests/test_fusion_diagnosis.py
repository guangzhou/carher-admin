"""Tests for the carher-3 fusion diagnosis demo."""

from __future__ import annotations

import pytest

from backend import fusion_diagnosis


def test_collect_litellm_csv_filters_carher_3_sample():
    data = fusion_diagnosis.collect_litellm_csv()

    assert data["source_status"] == "available"
    assert data["rows"] == 48
    assert data["success_calls"] == 48
    assert data["success_rate"] == 1
    assert data["active_hours"] == [0, 6, 9, 10, 23]
    assert data["total_tokens"] == 4_776_553
    assert data["completion_tokens"] == 33_729
    assert data["spend"] == 5.9589
    assert data["provider_distribution"] == {"anthropic": 40, "openrouter": 8}


def test_collect_pod_files_unavailable_is_non_fatal(monkeypatch):
    def fail_exec(uid: int, command: str) -> str:
        raise TimeoutError("TLS handshake timeout")

    monkeypatch.setattr(fusion_diagnosis, "_exec_in_pod", fail_exec)

    data = fusion_diagnosis.collect_pod_files()

    assert data["source_status"] == "unavailable"
    assert data["files"] == []
    assert "TLS handshake timeout" in data["error"]


def test_build_demo_report_marks_subjective_fields_for_review():
    report = fusion_diagnosis.build_demo_report(
        pod_files={"source_status": "unavailable", "files": [], "summary": {}, "error": "offline"}
    )

    assert report["uid"] == 3
    assert report["demo"] is True
    assert {"uid", "period", "scores", "sections", "sources"}.issubset(report)
    assert "B3 认知增量归因" in report["field_classes"]["needs_her_review"]
    assert report["sources"]["pod_files"]["status"] == "unavailable"


def test_render_markdown_does_not_leak_raw_ids_or_private_payloads():
    report = fusion_diagnosis.build_demo_report(
        pod_files={"source_status": "unavailable", "files": [], "summary": {}, "error": "offline"}
    )
    markdown = fusion_diagnosis.render_markdown(report)

    assert "chatcmpl-" not in markdown
    assert "gen-" not in markdown
    assert "sk-" not in markdown
    assert "request_id" not in markdown
    assert "payload" not in markdown.lower()
    assert "课题A" in markdown


def test_demo_route_rejects_other_uids():
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from backend import main

    with pytest.raises(HTTPException) as exc:
        main.api_fusion_diagnosis_demo(4)

    assert exc.value.status_code == 404
