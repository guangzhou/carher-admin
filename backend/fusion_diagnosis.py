"""Read-only fusion diagnosis demo for carher-3.

The demo deliberately avoids persistence and raw-content disclosure. It uses
aggregate evidence where available and marks subjective sections for Her review.
"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

DEMO_UID = 3
DEFAULT_START = "2026-04-20 23:00:00"
DEFAULT_END = "2026-04-21 11:00:00"
SAMPLE_CSV = Path(__file__).resolve().parent.parent / "docs" / "litellm-raw-20260420_23-20260421_1728.csv"

_TOKEN_RE = re.compile(r"\b(?:sk|cli|ou|chatcmpl|gen)-[A-Za-z0-9_.:-]+")
_LONG_WORD_RE = re.compile(r"[A-Za-z0-9_-]{24,}")


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_label(value: str) -> str:
    """Keep labels useful while stripping token-like identifiers."""
    value = _TOKEN_RE.sub("[redacted]", value or "")
    value = _LONG_WORD_RE.sub("[redacted]", value)
    return value[:160]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct) - 1))
    return ordered[idx]


def collect_litellm_csv(
    uid: int = DEMO_UID,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    csv_path: Path = SAMPLE_CSV,
) -> dict[str, Any]:
    """Collect aggregate LiteLLM evidence for a single uid from the sample CSV."""
    started_at = _parse_time(start)
    ended_at = _parse_time(end)
    rows: list[dict[str, str]] = []

    if not csv_path.exists():
        return {
            "source_status": "unavailable",
            "path": str(csv_path),
            "error": "sample CSV not found",
            "rows": 0,
        }

    alias = f"carher-{uid}"
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("user") != alias and row.get("api_key_alias") != alias:
                continue
            try:
                ts = _parse_time(row.get("bj_start_time", ""))
            except ValueError:
                continue
            if started_at <= ts <= ended_at:
                rows.append(row)

    statuses = Counter(r.get("status") or "unknown" for r in rows)
    providers = Counter(_sanitize_label(r.get("provider", "unknown")) for r in rows)
    models = Counter(_sanitize_label(r.get("model", "unknown")) for r in rows)
    hours = Counter(_parse_time(r["bj_start_time"]).hour for r in rows)
    durations = [_safe_float(r.get("duration_s")) for r in rows]
    total_tokens_by_row = [_safe_int(r.get("total_tokens")) for r in rows]
    completion_tokens_by_row = [_safe_int(r.get("completion_tokens")) for r in rows]
    deep_calls = sum(1 for tokens in completion_tokens_by_row if tokens >= 500)
    large_context_calls = sum(1 for tokens in total_tokens_by_row if tokens >= 50_000)

    total_calls = len(rows)
    success_calls = statuses.get("success", 0)
    return {
        "source_status": "available",
        "path": str(csv_path),
        "period": {"start": start, "end": end, "timezone": "Asia/Shanghai"},
        "rows": total_calls,
        "success_calls": success_calls,
        "success_rate": round(success_calls / total_calls, 4) if total_calls else 0,
        "status_distribution": dict(statuses),
        "provider_distribution": dict(providers),
        "model_distribution": dict(models),
        "hour_distribution": {str(k): hours[k] for k in sorted(hours)},
        "active_hours": sorted(hours),
        "total_tokens": sum(total_tokens_by_row),
        "completion_tokens": sum(completion_tokens_by_row),
        "spend": round(sum(_safe_float(r.get("spend")) for r in rows), 4),
        "duration_avg_s": round(sum(durations) / total_calls, 2) if total_calls else 0,
        "duration_p95_s": round(_percentile(durations, 0.95), 3),
        "duration_max_s": round(max(durations), 3) if durations else 0,
        "deep_calls": deep_calls,
        "deep_call_ratio": round(deep_calls / total_calls, 4) if total_calls else 0,
        "large_context_calls": large_context_calls,
        "large_context_ratio": round(large_context_calls / total_calls, 4) if total_calls else 0,
    }


def _exec_in_pod(uid: int, command: str) -> str:
    from kubernetes.stream import stream as k8s_stream

    from . import k8s_ops

    pod_name = k8s_ops._find_pod(uid)
    if not pod_name:
        raise RuntimeError(f"No running pod found for carher-{uid}")
    return k8s_stream(
        k8s_ops._core().connect_get_namespaced_pod_exec,
        pod_name,
        "carher",
        container="carher",
        command=["/bin/sh", "-c", command],
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
    )


def collect_pod_files(uid: int = DEMO_UID) -> dict[str, Any]:
    """Collect safe file metadata from the Her pod without reading raw content."""
    command = r"""
set -eu
for base in /data/.openclaw/sessions /data/.openclaw/logs /data/.openclaw; do
  if [ -d "$base" ]; then
    echo "BASE $base"
    find "$base" -maxdepth 2 -type f 2>/dev/null | head -80 | while IFS= read -r file; do
      size=$(wc -c < "$file" 2>/dev/null || echo 0)
      lines=$(wc -l < "$file" 2>/dev/null || echo 0)
      mtime=$(stat -c %Y "$file" 2>/dev/null || stat -f %m "$file" 2>/dev/null || echo 0)
      echo "FILE $base $size $lines $mtime ${file#$base/}"
    done
  fi
done
"""
    try:
        output = _exec_in_pod(uid, command)
    except Exception as exc:  # K8s/tunnel may be unavailable in local demo.
        return {"source_status": "unavailable", "error": str(exc)[:240], "files": [], "summary": {}}

    files: list[dict[str, Any]] = []
    base_counts: Counter[str] = Counter()
    total_bytes = 0
    total_lines = 0
    for raw_line in output.splitlines():
        if not raw_line.startswith("FILE "):
            continue
        parts = raw_line.split(" ", 5)
        if len(parts) != 6:
            continue
        _, base, size, lines, mtime, rel_path = parts
        safe_rel = _sanitize_label(rel_path)
        size_i = _safe_int(size)
        lines_i = _safe_int(lines)
        total_bytes += size_i
        total_lines += lines_i
        base_counts[base] += 1
        files.append({
            "base": base,
            "path_hint": safe_rel,
            "size_bytes": size_i,
            "lines": lines_i,
            "mtime_epoch": _safe_int(mtime),
            "category": _file_category(base, safe_rel),
        })

    category_counts = Counter(f["category"] for f in files)
    return {
        "source_status": "available",
        "files": files[:100],
        "summary": {
            "file_count": len(files),
            "base_counts": dict(base_counts),
            "category_counts": dict(category_counts),
            "total_bytes": total_bytes,
            "total_lines": total_lines,
        },
    }


def _file_category(base: str, rel_path: str) -> str:
    lowered = f"{base}/{rel_path}".lower()
    if "session" in lowered:
        return "session"
    if "memory" in lowered or lowered.endswith(".db") or lowered.endswith(".sqlite"):
        return "memory"
    if "log" in lowered:
        return "log"
    if "skill" in lowered:
        return "skill"
    return "other"


def build_demo_report(
    uid: int = DEMO_UID,
    *,
    litellm: dict[str, Any] | None = None,
    pod_files: dict[str, Any] | None = None,
    instance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured demo report from aggregate evidence."""
    if uid != DEMO_UID:
        raise ValueError("fusion diagnosis demo is only available for carher-3")
    litellm = litellm if litellm is not None else collect_litellm_csv(uid)
    pod_files = pod_files if pod_files is not None else collect_pod_files(uid)
    instance = instance or {}

    scores = _score_report(litellm, pod_files)
    sections = _sections(litellm, pod_files, scores)
    return {
        "uid": uid,
        "title": "融合体月度自诊断 Demo",
        "demo": True,
        "period": litellm.get("period", {"start": DEFAULT_START, "end": DEFAULT_END, "timezone": "Asia/Shanghai"}),
        "instance": _safe_instance(instance),
        "scores": scores,
        "sections": sections,
        "field_classes": {
            "auto_scored": [
                "A1 交互频次代理指标",
                "A3 时间分布",
                "B2 数据支撑代理指标",
                "D2 调用失败修复代理指标",
            ],
            "evidence_available": [
                "session/log/memory 文件活跃度",
                "模型/provider/状态聚合",
                "资源与实例上下文",
            ],
            "needs_her_review": [
                "B1 课题深度",
                "B3 认知增量归因",
                "C1 不可替代产出",
                "C2 组织传导率",
                "E 失败模式自诊断",
                "F 下月行动处方",
            ],
        },
        "sources": {
            "litellm_csv": {
                "status": litellm.get("source_status", "unknown"),
                "path": litellm.get("path", ""),
                "rows": litellm.get("rows", 0),
            },
            "pod_files": {
                "status": pod_files.get("source_status", "unknown"),
                "summary": pod_files.get("summary", {}),
                "error": pod_files.get("error", ""),
            },
            "privacy": "Only aggregate counts and sanitized path hints are included. Raw private messages are not returned.",
        },
    }


def _safe_instance(instance: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": instance.get("id", DEMO_UID),
        "name": _sanitize_label(str(instance.get("name", ""))),
        "status": _sanitize_label(str(instance.get("status", ""))),
        "provider": _sanitize_label(str(instance.get("provider", ""))),
        "deploy_group": _sanitize_label(str(instance.get("deploy_group", ""))),
        "has_memory": instance.get("has_memory"),
    }


def _score_report(litellm: dict[str, Any], pod_files: dict[str, Any]) -> dict[str, Any]:
    calls = int(litellm.get("rows") or 0)
    active_hours = set(litellm.get("active_hours") or [])
    success_rate = float(litellm.get("success_rate") or 0)
    deep_ratio = float(litellm.get("deep_call_ratio") or 0)
    large_context_ratio = float(litellm.get("large_context_ratio") or 0)
    category_counts = (pod_files.get("summary") or {}).get("category_counts") or {}

    a1 = _score_frequency(calls)
    a3 = _score_time_distribution(active_hours)
    a4 = min(4, 1 + int(category_counts.get("memory", 0) > 0) + int(category_counts.get("session", 0) > 0) + int(category_counts.get("log", 0) > 0)) if category_counts else None
    b2 = min(8, 2 + int(large_context_ratio >= 0.5) * 2 + int(deep_ratio >= 0.3) * 2 + int(calls >= 30) * 2)
    d2 = 5 if calls and success_rate >= 0.99 else 3 if success_rate >= 0.9 else 1

    a_total = a1 + a3 + (a4 if isinstance(a4, int) else 0)
    b_total = b2
    d_total = d2
    total = a_total + b_total + d_total
    return {
        "A": {"score": a_total, "max": 30, "confidence": "partial", "items": {"A1": a1, "A2": None, "A3": a3, "A4": a4, "A5": None}},
        "B": {"score": b_total, "max": 40, "confidence": "low", "items": {"B1": None, "B2": b2, "B3": None, "B4": None}},
        "C": {"score": None, "max": 20, "confidence": "needs_her_review", "items": {"C1": None, "C2": None, "C3": None}},
        "D": {"score": d_total, "max": 10, "confidence": "partial", "items": {"D1": None, "D2": d2}},
        "total_auto": {"score": total, "max_observed": 80, "note": "Demo total excludes subjective C and several B/A fields."},
    }


def _score_frequency(calls: int) -> int:
    if calls < 30:
        return 2
    if calls <= 100:
        return 4
    if calls <= 300:
        return 6
    if calls <= 600:
        return 8
    return 10


def _score_time_distribution(active_hours: set[int]) -> int:
    if not active_hours:
        return 0
    score = 1
    if any(18 <= h <= 23 for h in active_hours):
        score = max(score, 2)
    if any(h in {0, 1, 2, 3, 4, 5, 6} for h in active_hours):
        score = max(score, 4)
    return score


def _sections(litellm: dict[str, Any], pod_files: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    return {
        "A_connection_strength": {
            "classification": "auto_scored",
            "summary": "调用日志显示 carher-3 在样本周期内存在高强度、非常规时段使用。",
            "evidence": {
                "calls": litellm.get("rows", 0),
                "active_hours": litellm.get("active_hours", []),
                "hour_distribution": litellm.get("hour_distribution", {}),
                "deep_call_ratio": litellm.get("deep_call_ratio", 0),
            },
            "score_items": scores["A"]["items"],
        },
        "B_fusion_quality": {
            "classification": "mixed",
            "summary": "可自动证明大上下文和深度输出比例，但课题流转与认知增量需要 Her 基于 session 内容补充。",
            "evidence": {
                "total_tokens": litellm.get("total_tokens", 0),
                "completion_tokens": litellm.get("completion_tokens", 0),
                "large_context_ratio": litellm.get("large_context_ratio", 0),
                "provider_distribution": litellm.get("provider_distribution", {}),
                "model_distribution": litellm.get("model_distribution", {}),
            },
            "review_required": ["TOP3 课题", "课题流转路径", "认知增量归因", "元认知讨论"],
        },
        "C_fusion_effect": {
            "classification": "needs_her_review",
            "summary": "不可替代产出、组织传导率和认知时间释放不能从聚合调用日志可靠判断。",
            "placeholders": ["课题A", "产出物A", "组织采纳：待确认"],
        },
        "D_evolution": {
            "classification": "mixed",
            "summary": "调用成功率可作为失败修复代理指标；能力扩展仍需 Her 补充。",
            "evidence": {
                "success_rate": litellm.get("success_rate", 0),
                "status_distribution": litellm.get("status_distribution", {}),
                "duration_p95_s": litellm.get("duration_p95_s", 0),
            },
            "score_items": scores["D"]["items"],
        },
        "E_failure_modes": {
            "classification": "needs_her_review",
            "required_answers": ["认知松弛", "迎合陷阱", "信息茧房", "能力幻觉", "过度依赖", "信息安全"],
        },
        "F_next_month_prescription": {
            "classification": "needs_her_review",
            "required_outputs": ["给主人的建议", "主人给你的要求", "至少两个可衡量目标"],
        },
        "file_evidence": {
            "classification": "evidence_available",
            "source_status": pod_files.get("source_status", "unknown"),
            "summary": pod_files.get("summary", {}),
            "files": pod_files.get("files", [])[:20],
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the structured report as safe Markdown."""
    scores = report["scores"]
    sections = report["sections"]
    lit = sections["B_fusion_quality"]["evidence"]
    conn = sections["A_connection_strength"]["evidence"]
    d = sections["D_evolution"]["evidence"]
    sources = report["sources"]

    lines = [
        f"# {report['title']} - carher-{report['uid']}",
        "",
        f"- 周期：{report['period']['start']} ~ {report['period']['end']} ({report['period']['timezone']})",
        "- 性质：只读 demo 草稿；主观维度需要 Her 复核",
        "- 隐私：不输出原始私聊、群名、人名、客户名、金额、token 或请求原文",
        "",
        "## 汇总",
        "",
        "| 维度 | Demo 得分 | 满分 | 置信度 |",
        "|---|---:|---:|---|",
        f"| A 连接强度 | {scores['A']['score']} | {scores['A']['max']} | {scores['A']['confidence']} |",
        f"| B 融合质量 | {scores['B']['score']} | {scores['B']['max']} | {scores['B']['confidence']} |",
        f"| C 融合效果 | 待 Her 补充 | {scores['C']['max']} | {scores['C']['confidence']} |",
        f"| D 进化能力 | {scores['D']['score']} | {scores['D']['max']} | {scores['D']['confidence']} |",
        "",
        f"> 自动可观测小计：{scores['total_auto']['score']} / {scores['total_auto']['max_observed']}。{scores['total_auto']['note']}",
        "",
        "## A. 连接强度",
        "",
        f"- 调用次数：{conn['calls']}，A1 得分：{scores['A']['items']['A1']}",
        f"- 活跃小时：{', '.join(str(h) for h in conn['active_hours']) or '无'}，A3 得分：{scores['A']['items']['A3']}",
        f"- 深度调用比例：{conn['deep_call_ratio']:.2%}",
        f"- 小时分布：{conn['hour_distribution']}",
        "",
        "## B. 融合质量",
        "",
        f"- 总 token：{lit['total_tokens']:,}，completion token：{lit['completion_tokens']:,}",
        f"- 大上下文调用比例：{lit['large_context_ratio']:.2%}",
        f"- Provider 分布：{lit['provider_distribution']}",
        f"- 模型分布：{lit['model_distribution']}",
        "- 待 Her 补充：TOP3 课题、课题流转路径、认知增量归因、元认知讨论",
        "",
        "## C. 融合效果",
        "",
        "- 不可替代产出、组织传导率、认知时间释放无法仅从聚合日志判定。",
        "- 占位：课题A / 产出物A / 组织采纳：待确认。",
        "",
        "## D. 进化能力",
        "",
        f"- 调用成功率：{d['success_rate']:.2%}",
        f"- 状态分布：{d['status_distribution']}",
        f"- P95 耗时：{d['duration_p95_s']} 秒",
        "",
        "## E. 失败模式自诊断",
        "",
        "- 待 Her 逐项填写：认知松弛、迎合陷阱、信息茧房、能力幻觉、过度依赖、信息安全。",
        "",
        "## F. 下月行动处方",
        "",
        "- 待 Her 给出给主人建议、主人要求整理、至少两个可衡量目标。",
        "",
        "## 数据源",
        "",
        f"- LiteLLM CSV：{sources['litellm_csv']['status']}，聚合行数 {sources['litellm_csv']['rows']}",
        f"- Pod 文件：{sources['pod_files']['status']}，摘要 {sources['pod_files']['summary']}",
    ]
    return "\n".join(lines) + "\n"
