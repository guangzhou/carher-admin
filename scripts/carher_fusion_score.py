#!/usr/bin/env python3
"""Score CarHer fusion diagnosis evidence from batch base metrics.

This scorer intentionally consumes only the compact JSONL and summary generated
by carher_fusion_diagnosis_batch.py. It does not read K8s/PVC/raw chat content.
Scores are system-evidence scores, not owner-confirmed final scores.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any


LOW_CONFIDENCE_FIELDS = ["B3", "C1", "C2", "C3", "D1", "D2"]
C_REVIEW_REQUIRED_FIELDS = ["C1", "C2", "C3"]
PERIOD_START = "2026-05-01 00:00:00"
PERIOD_END_DISPLAY = "2026-06-12 23:59:59"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def metric(record: dict[str, Any], name: str) -> int:
    try:
        return int((record.get("base_metrics") or {}).get(name) or 0)
    except (TypeError, ValueError):
        return 0


def tier(record: dict[str, Any], name: str) -> str:
    return str(((record.get("relative_metrics") or {}).get(name) or {}).get("tier") or "no_signal")


def tier_points(value: str, mapping: dict[str, int]) -> int:
    return mapping.get(value, mapping.get("no_signal", 0))


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def topic_value(record: dict[str, Any], keywords: set[str] | None = None) -> int:
    total = 0
    for item in (record.get("base_metrics") or {}).get("topic_strength_top") or []:
        keyword = str(item.get("keyword") or "")
        if keywords is None or keyword in keywords:
            try:
                total += int(item.get("weighted_evidence") or 0)
            except (TypeError, ValueError):
                pass
    return total


def topic_count(record: dict[str, Any], min_weight: int = 100) -> int:
    count = 0
    for item in (record.get("base_metrics") or {}).get("topic_strength_top") or []:
        try:
            if int(item.get("weighted_evidence") or 0) >= min_weight:
                count += 1
        except (TypeError, ValueError):
            pass
    return count


def grade_for(total: int) -> str:
    if total >= 88:
        return "S·深度融合"
    if total >= 70:
        return "A·主动协作"
    if total >= 55:
        return "B·日常使用"
    if total >= 40:
        return "C·初步接入"
    return "D·浅层接触"


def confidence_for(record: dict[str, Any]) -> str:
    warnings = set((record.get("base_metrics") or {}).get("data_quality_warnings") or [])
    if "memory_db_unavailable" in warnings or "bot_alias_missing_mentions_unavailable" in warnings:
        return "低"
    if "owner_alias_missing_owner_message_count_unavailable" in warnings:
        return "中"
    return "高"


def score_a(record: dict[str, Any]) -> dict[str, Any]:
    a1 = tier_points(tier(record, "group_recent_messages"), {
        "p90_plus": 9,
        "p75_p90": 8,
        "p50_p75": 6,
        "below_p50": 4,
        "no_signal": 1,
    })
    if metric(record, "group_bot_mentions") >= 20:
        a1 = min(10, a1 + 1)

    areas = sum(1 for name in ["workspace_recent_files", "feishu_groups_recent_files", "logs_recent_files"] if metric(record, name) > 0)
    if metric(record, "memory_chunks") > 0:
        areas += 1
    group_tier = tier(record, "groups_with_recent_messages")
    a2 = min(8, areas + tier_points(group_tier, {"p90_plus": 4, "p75_p90": 3, "p50_p75": 2, "below_p50": 1, "no_signal": 0}))

    a3 = tier_points(tier(record, "group_active_days"), {
        "p90_plus": 4,
        "p75_p90": 4,
        "p50_p75": 3,
        "below_p50": 2,
        "no_signal": 0,
    })
    if metric(record, "files_recent_active_days") >= 30:
        a3 = max(a3, 3)

    a4 = tier_points(tier(record, "memory_chunks"), {
        "p90_plus": 4,
        "p75_p90": 4,
        "p50_p75": 3,
        "below_p50": 2,
        "no_signal": 0,
    })
    if metric(record, "memory_chunks") == 0:
        a4 = 0

    a5 = tier_points(tier(record, "groups_with_recent_messages"), {
        "p90_plus": 4,
        "p75_p90": 3,
        "p50_p75": 2,
        "below_p50": 1,
        "no_signal": 0,
    })
    return {
        "A1": clamp(a1, 0, 10),
        "A2": clamp(a2, 0, 8),
        "A3": clamp(a3, 0, 4),
        "A4": clamp(a4, 0, 4),
        "A5": clamp(a5, 0, 4),
    }


def score_b(record: dict[str, Any]) -> dict[str, Any]:
    strong_topics = topic_count(record, 500)
    medium_topics = topic_count(record, 100)
    if strong_topics >= 6:
        b1 = 11
    elif strong_topics >= 4:
        b1 = 10
    elif medium_topics >= 4:
        b1 = 8
    elif medium_topics >= 2:
        b1 = 6
    else:
        b1 = 4 if topic_value(record) else 2

    source_points = 0
    if metric(record, "group_recent_messages") > 0:
        source_points += 2
    if metric(record, "files_recent_count") > 0:
        source_points += 2
    if metric(record, "memory_chunks") > 0:
        source_points += 2
    if metric(record, "group_bot_mentions") > 0:
        source_points += 1
    if tier(record, "files_recent_count") in {"p75_p90", "p90_plus"} or tier(record, "memory_chunks") in {"p75_p90", "p90_plus"}:
        source_points += 1
    b2 = clamp(source_points, 1, 8)

    attribution_keywords = topic_value(record, {"纠偏", "源头逻辑", "模板"})
    b3 = 5
    if attribution_keywords >= 1000:
        b3 = 7
    elif attribution_keywords >= 300:
        b3 = 6
    if metric(record, "memory_chunks") == 0:
        b3 = min(b3, 4)

    meta_keywords = topic_value(record, {"自诊断", "纠偏", "模板", "质量体系"})
    b4 = 5
    if meta_keywords >= 3000:
        b4 = 9
    elif meta_keywords >= 1000:
        b4 = 8
    elif meta_keywords >= 300:
        b4 = 7
    elif meta_keywords >= 100:
        b4 = 6
    return {"B1": clamp(b1, 0, 12), "B2": b2, "B3": b3, "B4": clamp(b4, 0, 10)}


def score_c(record: dict[str, Any]) -> dict[str, Any]:
    output_base = mean([
        tier_points(tier(record, "files_recent_count"), {"p90_plus": 8, "p75_p90": 7, "p50_p75": 5, "below_p50": 3, "no_signal": 1}),
        tier_points(tier(record, "workspace_recent_files"), {"p90_plus": 8, "p75_p90": 7, "p50_p75": 5, "below_p50": 3, "no_signal": 1}),
    ])
    c1 = int(round(output_base))
    if topic_count(record, 500) >= 4:
        c1 = min(8, c1 + 1)

    c2 = mean([
        tier_points(tier(record, "group_recent_messages"), {"p90_plus": 6, "p75_p90": 5, "p50_p75": 3, "below_p50": 2, "no_signal": 0}),
        tier_points(tier(record, "groups_with_recent_messages"), {"p90_plus": 6, "p75_p90": 5, "p50_p75": 3, "below_p50": 2, "no_signal": 0}),
    ])
    c2 = int(round(c2))

    c3 = tier_points(tier(record, "files_recent_count"), {"p90_plus": 4, "p75_p90": 3, "p50_p75": 3, "below_p50": 2, "no_signal": 1})
    return {"C1": clamp(c1, 0, 8), "C2": clamp(c2, 0, 8), "C3": clamp(c3, 0, 4)}


def score_d(record: dict[str, Any]) -> dict[str, Any]:
    expansion = topic_value(record, {"质量体系", "自诊断", "模板", "IAA", "ICTS", "罗马尼亚"})
    d1 = 2
    if expansion >= 2000:
        d1 = 5
    elif expansion >= 700:
        d1 = 4
    elif expansion >= 100:
        d1 = 3

    repair = topic_value(record, {"纠偏", "源头逻辑", "模板"})
    d2 = 2
    if repair >= 2000:
        d2 = 4
    elif repair >= 500:
        d2 = 3
    if metric(record, "memory_chunks") == 0:
        d2 = min(d2, 2)
    return {"D1": clamp(d1, 0, 5), "D2": clamp(d2, 0, 5)}


def score_record(record: dict[str, Any]) -> dict[str, Any]:
    a = score_a(record)
    b = score_b(record)
    c = score_c(record)
    d = score_d(record)
    total_a = sum(a.values())
    total_b = sum(b.values())
    total_c = sum(c.values())
    total_d = sum(d.values())
    total = total_a + total_b + total_c + total_d
    bm = record.get("base_metrics") or {}
    warnings = bm.get("data_quality_warnings") or []
    return {
        "uid": record.get("uid"),
        "her_id": record.get("her_id"),
        "period": {"start": PERIOD_START, "end": PERIOD_END_DISPLAY},
        "scores": {
            **a,
            "A_total": total_a,
            **b,
            "B_total": total_b,
            **c,
            "C_total": total_c,
            **d,
            "D_total": total_d,
            "total": total,
        },
        "grade": grade_for(total),
        "confidence": confidence_for(record),
        "c2_adoption_pending": True,
        "score_type": "system_evidence_score",
        "owner_review_required_fields": C_REVIEW_REQUIRED_FIELDS,
        "owner_review_note": (
            "C类融合效果只能由系统估算证据强弱；不可替代产出、组织传导成功、"
            "认知时间释放需要Her/owner确认后才形成最终确认分。"
        ),
        "low_confidence_fields": LOW_CONFIDENCE_FIELDS,
        "data_source_summary": (
            f"系统证据分：群消息{metric(record, 'group_recent_messages')}条({tier(record, 'group_recent_messages')})；"
            f"Bot mention {metric(record, 'group_bot_mentions')}次；"
            f"群活跃{metric(record, 'group_active_days')}天；覆盖群{metric(record, 'groups_with_recent_messages')}个；"
            f"近期文件{metric(record, 'files_recent_count')}个({tier(record, 'files_recent_count')})；"
            f"Memory chunks {metric(record, 'memory_chunks')}({tier(record, 'memory_chunks')})；"
            f"Top主题：{bm.get('topic_summary') or 'none'}。"
        ),
        "notes": (
            "系统证据自动评分；C1/C2/C3是证据分，不代表组织传导已经成功，待Her/owner确认；D2等主观项也需复核；"
            + ("数据质量告警：" + ", ".join(warnings) if warnings else "无数据质量告警。")
        ),
        "evidence_refs": {
            "base_metrics": bm,
            "relative_metrics": record.get("relative_metrics") or {},
            "warnings": warnings,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Evidence JSONL from batch collection.")
    parser.add_argument("--summary", required=True, help="Batch summary JSON, kept for traceability.")
    parser.add_argument("--output", required=True, help="Output scored JSONL.")
    parser.add_argument("--summary-output", help="Optional score distribution summary JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records = load_jsonl(Path(args.input))
    scored = [score_record(record) for record in records if record.get("status") == "ok"]
    scored.sort(key=lambda item: int(item.get("uid") or 0))
    Path(args.output).write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in scored))
    if args.summary_output:
        totals = [item["scores"]["total"] for item in scored]
        by_grade: dict[str, int] = {}
        for item in scored:
            by_grade[item["grade"]] = by_grade.get(item["grade"], 0) + 1
        summary = {
            "count": len(scored),
            "min": min(totals) if totals else None,
            "max": max(totals) if totals else None,
            "mean": round(mean(totals), 2) if totals else None,
            "grade_counts": dict(sorted(by_grade.items())),
            "source_summary": str(Path(args.summary)),
            "scoring_note": "System evidence score only; owner-reviewed final score is separate.",
        }
        Path(args.summary_output).write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
