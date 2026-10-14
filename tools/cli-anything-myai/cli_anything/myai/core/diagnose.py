"""Explain *why* sync is stuck by replaying the sparse-fallback gate offline.

Gate (backend/usage_sync.py ~1316-1330 at 2818c0d), applied per backend per run
when the run used per-user daily activity instead of a log scan:

    expected_nonempty = max(1, int(attempted_users * RATIO))     # RATIO default 0.05
    sparse = attempted_users >= 20 and users_with_usage < expected_nonempty

``her`` (source="Her") never takes the log-scan path, so every her run is gated.
A sparse backend is dropped from the publish; the run is ``partial``; the
worker only advances ``last_success_at`` on ``ok``; queued refreshes stay
pending and retry forever.
"""

from __future__ import annotations

from typing import Any


DEFAULT_RATIO = 0.05


def expected_nonempty(attempted_users: int, ratio: float = DEFAULT_RATIO) -> int:
    ratio = max(0.0, min(1.0, float(ratio)))
    return max(1, int(attempted_users * ratio))


def is_sparse(attempted_users: int, users_with_usage: int, ratio: float = DEFAULT_RATIO) -> bool:
    return attempted_users >= 20 and users_with_usage < expected_nonempty(attempted_users, ratio)


def gate_report(
    attempted_users: int,
    per_day_active: list[dict[str, Any]],
    ratio: float = DEFAULT_RATIO,
) -> dict[str, Any]:
    """per_day_active: [{"date", "activeUsers", "requests"}] from upstream."""
    need = expected_nonempty(attempted_users, ratio)
    days = []
    for row in per_day_active:
        active = int(row.get("activeUsers") or 0)
        days.append({
            **row,
            "need": need,
            "blocked": is_sparse(attempted_users, active, ratio),
        })
    blocked = [d["date"] for d in days if d["blocked"]]
    return {
        "attemptedUsers": attempted_users,
        "ratio": ratio,
        "needNonEmptyUsers": need,
        "days": days,
        "blockedDays": blocked,
        "verdict": (
            f"{len(blocked)}/{len(days)} 天会被稀疏门禁拦下:上游真实活跃人数 < {need}。"
            "这是上游没流量,不是采集坏了;任何包含这些天的短窗口同步都会 partial。"
            if blocked
            else "门禁不会拦这些天;卡住另有原因,看 `sync errors`。"
        ),
    }


SPEND_LOG_CAP = 10000  # upstream /spend/logs/v2 never reports total above this


def truncation_report(upstream_days: list[dict[str, Any]], view_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare upstream per-day totals with what the dashboard view publishes.

    Upstream daily activity is UTC-dated while log-scan rows are Beijing-dated,
    so ratios are approximate. The strong signal is the cap fingerprint: a day
    whose published request count is an exact multiple of 10000.
    """
    view = {str(r.get("usage_date")): r for r in view_rows}
    days, suspects = [], []
    for row in upstream_days:
        day = str(row.get("date"))
        up_req = int(row.get("requests") or 0)
        up_spend = float(row.get("spend") or 0)
        shown = view.get(day) or {}
        shown_req = int(float(shown.get("requests") or 0))
        shown_spend = float(shown.get("spend") or 0)
        fingerprint = shown_req > 0 and shown_req % SPEND_LOG_CAP == 0 and up_req > shown_req
        ratio = round(shown_spend / up_spend, 3) if up_spend else None
        entry = {
            "date": day,
            "upstreamRequests": up_req,
            "shownRequests": shown_req,
            "upstreamSpend": round(up_spend, 2),
            "shownSpend": round(shown_spend, 2),
            "spendRatio": ratio,
            "capFingerprint": fingerprint,
        }
        days.append(entry)
        if fingerprint:
            suspects.append(day)
    missing = sum(d["upstreamSpend"] - d["shownSpend"] for d in days if d["capFingerprint"])
    return {
        "days": days,
        "truncatedDays": suspects,
        "missingSpendOnTruncatedDays": round(missing, 2),
        "verdict": (
            f"{len(suspects)} 天看板显示的请求数正好是 {SPEND_LOG_CAP} 的整数倍,上游实际更多:"
            f"日志扫描在上游 10000 条封顶处停下并当作完整发布了,这几天少算约 ${missing:,.0f}"
            if suspects
            else "没有命中 10000 封顶指纹(比例差异可能只是 UTC/北京日界不同)"
        ),
    }


def coverage_report(
    upstream_days: list[dict[str, Any]],
    shown_rows: list[dict[str, Any]],
    threshold_pct: float = 1.0,
) -> dict[str, Any]:
    """Dashboard spend vs upstream global spend, per day, with the likely cause."""
    shown: dict[str, dict[str, float]] = {}
    for row in shown_rows:
        day = str(row.get("usage_date"))
        source = str(row.get("attribution_source") or "")
        bucket = shown.setdefault(day, {"spend": 0.0, "requests": 0.0, "key_rollup": 0.0})
        spend = float(row.get("spend") or 0)
        bucket["spend"] += spend
        bucket["requests"] += float(row.get("requests") or 0)
        if source == "key_rollup":
            bucket["key_rollup"] += spend
    days, flagged = [], []
    for row in upstream_days:
        day = str(row.get("date"))
        up_spend = float(row.get("spend") or 0)
        mine = shown.get(day, {"spend": 0.0, "requests": 0.0, "key_rollup": 0.0})
        gap_pct = round(100 * (up_spend - mine["spend"]) / up_spend, 2) if up_spend else 0.0
        if not up_spend:
            hint = ""
        elif mine["spend"] == 0:
            hint = "看板这天是 0:原始日志被清空后单日扫描发布了 0?看 logs sync --grep 'below daily rollup'"
        elif int(mine["requests"]) % SPEND_LOG_CAP == 0 and mine["requests"] > 0 and abs(gap_pct) > threshold_pct:
            hint = "请求数是 10000 整数倍:日志扫描被上游计数封顶截断"
        elif abs(gap_pct) > threshold_pct and mine["key_rollup"] == 0:
            hint = "没有 key_rollup 行:这天还没被新代码重算,或 USAGE_SYNC_KEY_ROLLUP_ENABLED 被关了"
        elif abs(gap_pct) > threshold_pct:
            hint = "走的是日志扫描(北京日界)vs 上游 UTC 日界,几个百分点属正常;超过 10% 再查"
        else:
            hint = "ok"
        entry = {
            "date": day,
            "upstreamSpend": round(up_spend, 2),
            "shownSpend": round(mine["spend"], 2),
            "keyRollupSpend": round(mine["key_rollup"], 2),
            "gapPct": gap_pct,
            "hint": hint,
        }
        days.append(entry)
        if hint not in ("", "ok"):
            flagged.append(day)
    missing = sum(max(0.0, d["upstreamSpend"] - d["shownSpend"]) for d in days)
    return {
        "thresholdPct": threshold_pct,
        "days": days,
        "flaggedDays": flagged,
        "missingSpend": round(missing, 2),
        "verdict": (
            f"{len(flagged)} 天偏差超过 {threshold_pct}%,合计少 ${missing:,.0f};看每天的 hint"
            if flagged
            else "每天都与上游对得上"
        ),
    }


# Runs inside the sync worker and replays exactly what the gate measures:
# - attempted users = carher-* ids from /user/list paged with sort_by=user_id
#   (litellm_client.users(); unsorted offset paging drops/duplicates accounts)
# - a user "has usage" on a day iff per-user /user/daily/activity/aggregated
#   for that single day returns any result row (_usage_from_daily_activity)
# Global per-day totals come from the same endpoint without user_id.
HER_GATE_SCRIPT = r'''
import json, os
from concurrent.futures import ThreadPoolExecutor
import httpx
PARAMS = json.loads(__PARAMS__)
base, key = os.environ.get("HER_LITELLM_BASE_URL", ""), os.environ.get("HER_LITELLM_ADMIN_KEY", "")
out = {"configured": bool(base and key), "baseUrl": base, "days": []}
if out["configured"]:
    c = httpx.Client(base_url=base, headers={"Authorization": "Bearer " + key}, timeout=120)
    ids, page = [], 1
    while page <= 100:
        j = c.get("/user/list", params={"page": page, "page_size": 100, "sort_by": "user_id", "sort_order": "asc"}).json()
        for u in j.get("users") or []:
            uid = str(u.get("user_id") or "").strip()
            if uid.lower().startswith("carher-") and uid not in ids:
                ids.append(uid)
        if page >= int(j.get("total_pages") or 1):
            break
        page += 1
    out["attemptedUsers"] = len(ids)
    agg = "/user/daily/activity/aggregated"
    def has_usage(uid, day):
        r = c.get(agg, params={"user_id": uid, "start_date": day, "end_date": day, "page": 1, "page_size": 1000})
        return bool((r.json() or {}).get("results")) if r.status_code == 200 else None
    with ThreadPoolExecutor(max_workers=PARAMS["concurrency"]) as pool:
        for day in PARAMS["days"]:
            md = (c.get(agg, params={"start_date": day, "end_date": day}).json() or {}).get("metadata") or {}
            flags = list(pool.map(lambda u: has_usage(u, day), ids))
            out["days"].append({
                "date": day,
                "requests": md.get("total_api_requests"),
                "spend": round(float(md.get("total_spend") or 0), 2),
                "activeUsers": sum(1 for f in flags if f),
                "failedUsers": sum(1 for f in flags if f is None),
            })
print(json.dumps(out))
'''


def her_gate_script(days: list[str], concurrency: int = 4) -> str:
    """Per-user probes = users × days GETs; concurrency matches the worker's
    USAGE_SYNC_USER_CONCURRENCY default (4) so we add no more load than one sync."""
    import json

    params = {"days": days, "concurrency": max(1, min(8, int(concurrency)))}
    return HER_GATE_SCRIPT.replace("__PARAMS__", repr(json.dumps(params)))
