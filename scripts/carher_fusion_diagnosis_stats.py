#!/usr/bin/env python3
"""Collect privacy-safe evidence stats for a CarHer fusion diagnosis report.

The script executes read-only Python inside a Her pod and prints aggregate JSON.
It intentionally reports counts, sanitized path hints, and keyword hit totals,
not raw private chat content or secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Any


DEFAULT_KEYWORDS = [
    "AI KPI",
    "-1层",
    "源头逻辑",
    "ICTS",
    "IAA",
    "罗马尼亚",
    "质量体系",
    "质量",
    "自诊断",
    "模板",
    "纠偏",
]
DEFAULT_LOW_CONFIDENCE_FIELDS = ["B3", "C1", "C2", "C3", "D1", "D2"]

TOKEN_RE = re.compile(r"\b(?:sk|cli|ou|om|oc|chatcmpl|gen)_[A-Za-z0-9_.:-]+|\b(?:sk|cli|ou|om|oc|chatcmpl|gen)-[A-Za-z0-9_.:-]+")
LONG_ID_RE = re.compile(r"[A-Za-z0-9_-]{32,}")


REMOTE_COLLECTOR = r'''
import datetime
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

params = json.loads(sys.argv[1])
base = params.get("base", "/data/.openclaw")
keywords = params["keywords"]
owner_aliases = [alias for alias in params.get("owner_aliases", []) if alias]
bot_aliases = [alias for alias in params.get("bot_aliases", []) if alias]
text_suffixes = tuple(params["text_suffixes"])
max_text_bytes = int(params["max_text_bytes"])
start_s = params["start"]
end_s = params["end"]
tz = datetime.timezone(datetime.timedelta(hours=8))

def parse_local_epoch(value):
    return int(datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp())

start_epoch = parse_local_epoch(start_s)
end_epoch = parse_local_epoch(end_s)

def normalize_epoch(value):
    if value is None:
        return 0
    try:
        value = float(value)
    except Exception:
        return 0
    while value > 32503680000:  # larger than year 3000 in seconds, likely ms/us/ns.
        value = value / 1000
    return int(value)

def stamp(epoch):
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M:%S")

def area_for(path):
    rel = path[len(base):].lstrip("/") if path.startswith(base) else path
    return rel.split("/", 1)[0] if rel else "root"

def sanitize_path(path):
    rel = path[len(base):].lstrip("/") if path.startswith(base) else path
    parts = []
    for part in rel.split("/"):
        if len(part) >= 32 and all(c.isalnum() or c in "_-." for c in part):
            parts.append("[id]")
        elif part.startswith(("oc_", "ou_", "om_", "cli_")):
            parts.append("[id]")
        else:
            parts.append(part[:120])
    return "/".join(parts)

def path_category(path):
    rel = path[len(base):].lstrip("/") if path.startswith(base) else path
    parts = rel.split("/")
    area = parts[0] if parts and parts[0] else "root"
    ext = os.path.splitext(rel.lower())[1] or "[no-ext]"
    if area == "feishu-groups":
        return "feishu-groups/[group-cache]"
    if area == "sessions":
        return "sessions/[session]"
    if area == "logs":
        return "logs/[log]"
    if area == "memory":
        return "memory/[memory-store]"
    if area == "workspace" and len(parts) > 1:
        if parts[1] in ("memory", "outputs", "tmp", "skills"):
            return "workspace/" + parts[1]
        return "workspace/[file-" + ext.lstrip(".") + "]"
    return area + "/[file-" + ext.lstrip(".") + "]"

def is_text_candidate(path, size):
    lowered = path.lower()
    return size <= max_text_bytes and lowered.endswith(text_suffixes)

def alias_in_text(text, aliases):
    return any(alias and alias in text for alias in aliases)

def alias_in_mentions(mentions, aliases):
    for mention in mentions:
        if isinstance(mention, dict):
            if alias_in_text(str(mention.get("name") or ""), aliases):
                return True
            if alias_in_text(str(mention), aliases):
                return True
        elif alias_in_text(str(mention), aliases):
            return True
    return False

roots = [
    os.path.join(base, "sessions"),
    os.path.join(base, "workspace"),
    os.path.join(base, "memory"),
    os.path.join(base, "logs"),
    os.path.join(base, "feishu-groups"),
]

files_total = 0
files_total_by_area = Counter()
recent_files = []
for root in roots:
    if not os.path.exists(root):
        continue
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files_total += 1
            area = area_for(path)
            files_total_by_area[area] += 1
            mtime = normalize_epoch(st.st_mtime)
            if start_epoch <= mtime < end_epoch:
                recent_files.append({
                    "area": area,
                    "path": path_category(path),
                    "path_category": path_category(path),
                    "_real_path": path,
                    "size_bytes": st.st_size,
                    "mtime": stamp(mtime),
                    "text_candidate": is_text_candidate(path, st.st_size),
                })

recent_by_area = Counter(item["area"] for item in recent_files)
recent_files_sorted = sorted(recent_files, key=lambda item: (item["mtime"] or "", item["path"]))
recent_by_day = Counter((item["mtime"] or "")[:10] for item in recent_files if item.get("mtime"))
recent_by_area_day = defaultdict(Counter)
recent_ext_counts = Counter()
recent_top_dirs = Counter()
recent_text_candidate_count = 0
recent_size_bytes_total = 0
for item in recent_files:
    day = (item.get("mtime") or "")[:10]
    if day:
        recent_by_area_day[item["area"]][day] += 1
    _, ext = os.path.splitext(item["path"].lower())
    recent_ext_counts[ext or "[no-ext]"] += 1
    recent_top_dirs[item["path_category"]] += 1
    if item["text_candidate"]:
        recent_text_candidate_count += 1
    recent_size_bytes_total += int(item.get("size_bytes") or 0)

keyword_paths = {kw: [] for kw in keywords}
keyword_occurrences = Counter()
for item in recent_files_sorted:
    if not item["text_candidate"]:
        continue
    try:
        with open(item["_real_path"], "r", errors="ignore") as handle:
            text = handle.read(max_text_bytes + 1)
    except Exception:
        continue
    for kw in keywords:
        count = text.count(kw)
        if count:
            keyword_occurrences[kw] += count
            keyword_paths[kw].append(item["path_category"])

keyword_recent_file_hits = {}
for kw in keywords:
    category_counts = Counter(keyword_paths[kw])
    keyword_recent_file_hits[kw] = {
        "file_count": len(keyword_paths[kw]),
        "occurrences": keyword_occurrences[kw],
        "path_hints": [{"category": category, "count": count} for category, count in category_counts.most_common(30)],
    }

group_stats = {
    "total_recent_messages": 0,
    "groups_with_recent_messages": 0,
    "owner_aliases_configured": bool(owner_aliases),
    "bot_aliases_configured": bool(bot_aliases),
    "owner_messages": 0,
    "bot_mentions": 0,
    "messages_by_day": {},
    "bot_mentions_by_day": {},
    "keyword_counts": {},
}
group_message_counts = Counter()
messages_by_day = Counter()
mentions_by_day = Counter()
group_keyword_counts = Counter()
for dirpath, _, filenames in os.walk(os.path.join(base, "feishu-groups")) if os.path.exists(os.path.join(base, "feishu-groups")) else []:
    for name in filenames:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(dirpath, name)
        try:
            handle = open(path, "r", errors="ignore")
        except Exception:
            continue
        with handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = normalize_epoch(obj.get("ts"))
                if not (start_epoch <= ts < end_epoch):
                    continue
                group_stats["total_recent_messages"] += 1
                group = os.path.basename(os.path.dirname(path))
                group_message_counts[group] += 1
                day = datetime.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
                messages_by_day[day] += 1
                sender = obj.get("sender") or ""
                if owner_aliases and alias_in_text(sender, owner_aliases):
                    group_stats["owner_messages"] += 1
                text = (obj.get("text") or "") + " " + str((obj.get("textParts") or {}).get("withoutFooter") or "")
                mentions = obj.get("mentions") or []
                if bot_aliases and (alias_in_text(text, bot_aliases) or alias_in_mentions(mentions, bot_aliases)):
                    group_stats["bot_mentions"] += 1
                    mentions_by_day[day] += 1
                for kw in keywords:
                    if kw in text:
                        group_keyword_counts[kw] += text.count(kw)

group_stats["groups_with_recent_messages"] = len(group_message_counts)
group_stats["messages_by_day"] = dict(sorted(messages_by_day.items()))
group_stats["bot_mentions_by_day"] = dict(sorted(mentions_by_day.items()))
group_stats["keyword_counts"] = {kw: group_keyword_counts.get(kw, 0) for kw in keywords}

memory_db = {
    "status": "missing",
    "tables": {},
    "keyword_fts_hits": {},
}
db_path = os.path.join(base, "memory", "main.sqlite")
if os.path.exists(db_path):
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        memory_db["status"] = "available"
        for table in ["files", "chunks", "chunks_fts", "embedding_cache"]:
            try:
                memory_db["tables"][table] = cur.execute(f"select count(*) from {table}").fetchone()[0]
            except Exception as exc:
                memory_db["tables"][table] = {"error": str(exc)[:120]}
        for kw in keywords:
            rows = []
            try:
                query = '"' + kw.replace('"', '""') + '"'
                rows = cur.execute(
                    "select path, count(*) from chunks_fts where chunks_fts match ? group by path order by count(*) desc limit 20",
                    (query,),
                ).fetchall()
            except Exception:
                try:
                    rows = cur.execute(
                        "select path, count(*) from chunks where text like ? group by path order by count(*) desc limit 20",
                        ("%" + kw + "%",),
                    ).fetchall()
                except Exception:
                    rows = []
            memory_db["keyword_fts_hits"][kw] = {
                "path_count": len(rows),
                "hits": sum(int(row[1]) for row in rows),
                "path_hints": [{"category": path_category(row[0]), "hits": int(row[1])} for row in rows[:12]],
            }
    except Exception as exc:
        memory_db = {"status": "error", "error": str(exc)[:240], "tables": {}, "keyword_fts_hits": {}}

result = {
    "period": {"start": start_s, "end": end_s, "timezone": "Asia/Shanghai"},
    "openclaw_base": base,
    "files": {
        "total_scanned": files_total,
        "total_by_area": dict(files_total_by_area),
        "recent_count": len(recent_files_sorted),
        "recent_by_area": dict(recent_by_area),
        "recent_active_days": len(recent_by_day),
        "recent_by_day": dict(sorted(recent_by_day.items())),
        "recent_by_area_day": {area: dict(sorted(days.items())) for area, days in sorted(recent_by_area_day.items())},
        "recent_ext_counts": dict(recent_ext_counts.most_common(30)),
        "recent_top_dirs": [{"path": path, "count": count} for path, count in recent_top_dirs.most_common(30)],
        "recent_text_candidate_count": recent_text_candidate_count,
        "recent_size_bytes_total": recent_size_bytes_total,
        "recent_samples": [{key: value for key, value in item.items() if not key.startswith("_")} for item in recent_files_sorted[:120]],
    },
    "keyword_recent_file_hits": keyword_recent_file_hits,
    "feishu_group_cache": group_stats,
    "memory_db": memory_db,
}
print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
'''


def sanitize_label(value: str) -> str:
    value = TOKEN_RE.sub("[redacted]", value or "")
    value = LONG_ID_RE.sub("[redacted]", value)
    return value


def _top_dict_items(values: dict[str, int], limit: int = 10) -> list[dict[str, int | str]]:
    return [
        {"key": key, "count": int(count)}
        for key, count in sorted(values.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def _keyword_counts(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    file_hits = data.get("keyword_recent_file_hits") or {}
    group_hits = (data.get("feishu_group_cache") or {}).get("keyword_counts") or {}
    memory_hits = (data.get("memory_db") or {}).get("keyword_fts_hits") or {}
    keywords = sorted(set(file_hits) | set(group_hits) | set(memory_hits))
    result: dict[str, dict[str, int]] = {}
    for keyword in keywords:
        file_item = file_hits.get(keyword) or {}
        memory_item = memory_hits.get(keyword) or {}
        result[keyword] = {
            "recent_file_count": int(file_item.get("file_count") or 0),
            "recent_file_occurrences": int(file_item.get("occurrences") or 0),
            "group_occurrences": int(group_hits.get(keyword) or 0),
            "memory_path_count": int(memory_item.get("path_count") or 0),
            "memory_hits": int(memory_item.get("hits") or 0),
        }
    return result


def _int_metric(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _data_quality_warnings(coverage: dict[str, Any]) -> list[str]:
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


def build_base_metrics(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten raw pod evidence into the stable all-Her evidence table shape."""
    scoring = data.get("scoring_evidence") or {}
    coverage = scoring.get("coverage_summary") or {}
    files = data.get("files") or {}
    recent_by_area = files.get("recent_by_area") or {}
    memory_tables = (data.get("memory_db") or {}).get("tables") or {}
    topics = (
        (scoring.get("B_fusion_quality") or {})
        .get("B1_topic_depth", {})
        .get("topic_strength_top", [])
    )[:10]

    return {
        "uid": data.get("uid"),
        "her_id": f"carher-{data.get('uid')}" if data.get("uid") is not None else None,
        "period": data.get("period"),
        "pod": data.get("pod"),
        "container": data.get("container"),
        "bot_aliases_configured": bool(coverage.get("bot_aliases_configured")),
        "owner_aliases_configured": bool(coverage.get("owner_aliases_configured")),
        "group_recent_messages": _int_metric(coverage.get("group_recent_messages")),
        "group_bot_mentions": _int_metric(coverage.get("group_bot_mentions")),
        "group_active_days": _int_metric(coverage.get("group_active_days")),
        "groups_with_recent_messages": _int_metric(coverage.get("groups_with_recent_messages")),
        "files_recent_count": _int_metric(coverage.get("files_recent_count")),
        "files_recent_active_days": _int_metric(coverage.get("files_recent_active_days")),
        "workspace_recent_files": _int_metric(recent_by_area.get("workspace")),
        "feishu_groups_recent_files": _int_metric(recent_by_area.get("feishu-groups")),
        "memory_recent_files": _int_metric(recent_by_area.get("memory")),
        "logs_recent_files": _int_metric(recent_by_area.get("logs")),
        "memory_status": coverage.get("memory_status"),
        "memory_files": _int_metric(memory_tables.get("files")),
        "memory_chunks": _int_metric(memory_tables.get("chunks")),
        "memory_fts_chunks": _int_metric(memory_tables.get("chunks_fts")),
        "embedding_cache": _int_metric(memory_tables.get("embedding_cache")),
        "topic_strength_top": topics,
        "topic_summary": ", ".join(
            f"{item.get('keyword')}:{_int_metric(item.get('weighted_evidence'))}"
            for item in topics[:8]
        ),
        "data_quality_warnings": _data_quality_warnings(coverage),
        "low_confidence_fields": DEFAULT_LOW_CONFIDENCE_FIELDS,
        "privacy": data.get("privacy") or scoring.get("privacy") or {},
    }


def build_scoring_evidence(data: dict[str, Any]) -> dict[str, Any]:
    """Build model-ready evidence so reports don't need to inspect raw sources."""
    files = data.get("files") or {}
    groups = data.get("feishu_group_cache") or {}
    memory = data.get("memory_db") or {}
    memory_tables = memory.get("tables") or {}
    keyword_counts = _keyword_counts(data)
    topic_strength = []
    for keyword, counts in keyword_counts.items():
        weighted = (
            counts["recent_file_count"] * 3
            + counts["recent_file_occurrences"]
            + counts["group_occurrences"] * 2
            + counts["memory_path_count"] * 2
            + counts["memory_hits"]
        )
        topic_strength.append({"keyword": keyword, "weighted_evidence": int(weighted), **counts})
    topic_strength.sort(key=lambda item: item["weighted_evidence"], reverse=True)

    messages_by_day = groups.get("messages_by_day") or {}
    mentions_by_day = groups.get("bot_mentions_by_day") or {}
    recent_by_day = files.get("recent_by_day") or {}
    recent_by_area = files.get("recent_by_area") or {}

    return {
        "schema_version": "2026-06-13.1",
        "purpose": "Model-ready aggregate evidence for A/B/C/D fusion diagnosis scoring.",
        "period": data.get("period"),
        "coverage_summary": {
            "pod": data.get("pod"),
            "container": data.get("container"),
            "files_recent_count": int(files.get("recent_count") or 0),
            "files_recent_active_days": int(files.get("recent_active_days") or 0),
            "group_recent_messages": int(groups.get("total_recent_messages") or 0),
            "group_active_days": len(messages_by_day),
            "group_bot_mentions": int(groups.get("bot_mentions") or 0),
            "bot_aliases_configured": bool(groups.get("bot_aliases_configured")),
            "owner_aliases_configured": bool(groups.get("owner_aliases_configured")),
            "groups_with_recent_messages": int(groups.get("groups_with_recent_messages") or 0),
            "memory_status": memory.get("status"),
            "memory_files": int(memory_tables.get("files") or 0) if isinstance(memory_tables.get("files"), int) else 0,
            "memory_chunks": int(memory_tables.get("chunks") or 0) if isinstance(memory_tables.get("chunks"), int) else 0,
        },
        "A_connection_strength": {
            "A1_interaction_frequency": {
                "group_recent_messages": int(groups.get("total_recent_messages") or 0),
                "bot_mentions": int(groups.get("bot_mentions") or 0),
                "bot_aliases_configured": bool(groups.get("bot_aliases_configured")),
                "workspace_recent_files": int(recent_by_area.get("workspace") or 0),
                "session_recent_files": int(recent_by_area.get("sessions") or 0),
                "log_recent_files": int(recent_by_area.get("logs") or 0),
                "note": "Counts support frequency, but file count is not equal to chat count.",
            },
            "A2_scenario_coverage": {
                "areas_with_recent_files": sorted([area for area, count in recent_by_area.items() if count]),
                "groups_with_recent_messages": int(groups.get("groups_with_recent_messages") or 0),
                "has_workspace_activity": int(recent_by_area.get("workspace") or 0) > 0,
                "has_memory_activity": int(recent_by_area.get("memory") or 0) > 0,
                "has_group_activity": int(groups.get("total_recent_messages") or 0) > 0,
            },
            "A3_time_distribution": {
                "file_active_days": int(files.get("recent_active_days") or 0),
                "group_active_days": len(messages_by_day),
                "top_message_days": _top_dict_items(messages_by_day, 10),
                "top_file_days": _top_dict_items(recent_by_day, 10),
                "top_mention_days": _top_dict_items(mentions_by_day, 10),
            },
            "A4_information_capture": {
                "memory_tables": memory_tables,
                "recent_text_candidate_count": int(files.get("recent_text_candidate_count") or 0),
                "recent_size_bytes_total": int(files.get("recent_size_bytes_total") or 0),
            },
            "A5_group_coverage": {
                "group_recent_messages": int(groups.get("total_recent_messages") or 0),
                "groups_with_recent_messages": int(groups.get("groups_with_recent_messages") or 0),
                "bot_mentions": int(groups.get("bot_mentions") or 0),
                "bot_aliases_configured": bool(groups.get("bot_aliases_configured")),
                "group_keyword_counts": groups.get("keyword_counts") or {},
            },
        },
        "B_fusion_quality": {
            "B1_topic_depth": {
                "topic_strength_top": topic_strength[:20],
                "keyword_counts": keyword_counts,
                "note": "topic_strength_top is a within-Her ranking proxy, not a calibrated cross-Her score.",
            },
            "B2_data_support": {
                "available_sources": [
                    source
                    for source, available in {
                        "k8s_pod": bool(data.get("pod")),
                        "pvc_files": bool(files.get("recent_count")),
                        "feishu_group_cache": bool(groups.get("total_recent_messages")),
                        "memory_db": memory.get("status") == "available",
                    }.items()
                    if available
                ],
                "source_limits": [
                    "Formal adoption records are not proven by group messages alone.",
                    "Private chat exact counts may be unavailable unless backend logs are integrated.",
                    "Keyword hits show topic presence, not conclusion quality.",
                    "Workspace and memory file recency use mtime evidence and may be affected by sync or copy operations.",
                ],
            },
            "B3_cognitive_increment_attribution": {
                "proxy_keywords": {
                    keyword: keyword_counts.get(keyword, {})
                    for keyword in ["纠偏", "源头逻辑", "模板", "自诊断"]
                    if keyword in keyword_counts
                },
                "requires_owner_review": True,
            },
            "B4_metacognition_discussion": {
                "proxy_keywords": {
                    keyword: keyword_counts.get(keyword, {})
                    for keyword in ["纠偏", "模板", "自诊断", "源头逻辑"]
                    if keyword in keyword_counts
                },
            },
        },
        "C_fusion_effect": {
            "C1_irreplaceable_outputs": {
                "workspace_recent_files": int(recent_by_area.get("workspace") or 0),
                "top_recent_dirs": files.get("recent_top_dirs") or [],
                "requires_owner_review": True,
            },
            "C2_organization_propagation": {
                "group_recent_messages": int(groups.get("total_recent_messages") or 0),
                "groups_with_recent_messages": int(groups.get("groups_with_recent_messages") or 0),
                "bot_mentions": int(groups.get("bot_mentions") or 0),
                "bot_aliases_configured": bool(groups.get("bot_aliases_configured")),
                "top_message_days": _top_dict_items(messages_by_day, 10),
                "formal_adoption_requires_review": True,
            },
            "C3_cognitive_time_release": {
                "proxy_recent_files": int(files.get("recent_count") or 0),
                "proxy_text_candidates": int(files.get("recent_text_candidate_count") or 0),
                "requires_owner_review": True,
            },
        },
        "D_evolution_ability": {
            "D1_capability_expansion": {
                "recent_ext_counts": files.get("recent_ext_counts") or {},
                "topic_strength_top": topic_strength[:10],
                "top_recent_dirs": files.get("recent_top_dirs") or [],
            },
            "D2_failure_repair": {
                "repair_proxy_keywords": {
                    keyword: keyword_counts.get(keyword, {})
                    for keyword in ["纠偏", "源头逻辑", "模板"]
                    if keyword in keyword_counts
                },
                "requires_recurrence_check": True,
            },
        },
        "privacy": {
            "raw_chat_content_included": False,
            "secrets_included": False,
            "person_or_group_names_should_not_be_reported": True,
        },
    }


def build_kubectl_cmd(args: argparse.Namespace, pod: str) -> list[str]:
    params = {
        "base": args.openclaw_base,
        "start": args.start,
        "end": args.end,
        "keywords": args.keyword,
        "owner_aliases": args.owner_alias,
        "bot_aliases": args.bot_alias,
        "text_suffixes": [
            ".md",
            ".txt",
            ".xml",
            ".json",
            ".jsonl",
            ".py",
            ".yaml",
            ".yml",
        ],
        "max_text_bytes": args.max_text_bytes,
    }
    cmd = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "-n",
        args.namespace,
        "exec",
        pod,
        "-c",
        args.container,
        "--",
        "python3",
        "-c",
        REMOTE_COLLECTOR,
        json.dumps(params, ensure_ascii=False),
    ]
    return cmd


def find_pod(args: argparse.Namespace) -> str:
    if args.pod:
        return args.pod
    selector = f"app=carher-user,user-id={args.uid}"
    cmd = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "-n",
        args.namespace,
        "get",
        "pods",
        "-l",
        selector,
        "-o",
        "json",
    ]
    proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    data = json.loads(proc.stdout)
    pods = [
        item
        for item in data.get("items", [])
        if item.get("status", {}).get("phase") == "Running"
    ]
    if not pods:
        raise RuntimeError(f"No running pod found for carher-{args.uid}")
    pods.sort(key=lambda item: item["metadata"].get("creationTimestamp", ""), reverse=True)
    return pods[0]["metadata"]["name"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", type=int, default=3, help="Her uid, default: 3")
    parser.add_argument("--start", default="2026-06-07 00:00:00", help="Asia/Shanghai start time")
    parser.add_argument("--end", default="2026-06-13 00:00:00", help="Asia/Shanghai exclusive end time")
    parser.add_argument("--namespace", default="carher")
    parser.add_argument("--container", default="carher")
    parser.add_argument("--pod", help="Pod name. If omitted, selected by app=carher-user,user-id=<uid>.")
    parser.add_argument("--kubeconfig", default="~/.kube/config")
    parser.add_argument("--openclaw-base", default="/data/.openclaw")
    parser.add_argument("--keyword", action="append", default=list(DEFAULT_KEYWORDS), help="Keyword to count. Can repeat.")
    parser.add_argument("--owner-alias", action="append", default=[], help="Owner display alias used only for aggregate owner-message counts. Can repeat.")
    parser.add_argument("--bot-alias", action="append", default=[], help="Bot display alias used only for aggregate mention counts. Can repeat.")
    parser.add_argument("--max-text-bytes", type=int, default=5_000_000)
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    args.kubeconfig = args.kubeconfig.replace("~", os.path.expanduser("~"), 1)
    try:
        pod = find_pod(args)
        cmd = build_kubectl_cmd(args, pod)
        proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
        data: dict[str, Any] = json.loads(proc.stdout)
        data["uid"] = args.uid
        data["pod"] = pod
        data["container"] = args.container
        data["privacy"] = {
            "raw_chat_content_included": False,
            "secrets_included": False,
            "path_hints_sanitized": True,
        }
        data["scoring_evidence"] = build_scoring_evidence(data)
        data["base_metrics"] = build_base_metrics(data)
        text = json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True)
        print(sanitize_label(text))
        return 0
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc.stdout or str(exc), file=sys.stderr)
        return exc.returncode or 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
