"""Public /api/health (unauthenticated) and a compact summary of its fields.

Field meanings follow backend/main.py ``/api/health`` (patches 0014/0016, 2026-10-29).
organizations=403 (LiteLLM non-Enterprise) is permanent on this deployment: the server
now marks it ``organization.knownLimitation`` and no longer degrades overall ``status``;
here it is reported as ``known``. Any other organization probe failure is ``warn``.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Any


KNOWN_ORG_403 = "Organizations are only available for LiteLLM Enterprise"


def fetch(public_url: str, timeout: float = 15.0, opener=urllib.request.urlopen) -> dict[str, Any]:
    request = urllib.request.Request(public_url.rstrip("/") + "/api/health", headers={"User-Agent": "cli-anything-myai"})
    with opener(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _age_seconds(value: Any, now: datetime) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((now - parsed).total_seconds()))


def summarize(health: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    sync = health.get("usageSync") or {}
    org = health.get("organization") or {}
    queue = health.get("usageRefreshQueue") or {}
    realtime = health.get("usageRealtime") or {}
    billing = health.get("billing") or {}
    outbox = org.get("outbox") or {}

    findings: list[dict[str, str]] = []

    org_errors = org.get("errors") or {}
    org_detail = json.dumps(org_errors, ensure_ascii=False)
    if org_errors:
        known = org.get("knownLimitation") == "upstream_enterprise_license_missing" or (
            KNOWN_ORG_403 in org_detail and set(org_errors) == {"organizations"}
        )
        findings.append({
            "check": "organization",
            "level": "known" if known else "warn",
            "detail": "organizations 403:上游 LiteLLM 非 Enterprise(常驻)" if known else org_detail[:300],
        })

    success_age = _age_seconds(sync.get("lastSuccessAt"), now)
    if success_age is not None and success_age > 6 * 3600:
        findings.append({
            "check": "usageSync.lastSuccessAt",
            "level": "warn",
            "detail": f"最后一次 status=ok 的同步在 {success_age // 3600}h 前;partial 不推进它。查 `sync errors`",
        })
    if sync.get("heartbeatLagSeconds") not in (None,) and int(sync.get("heartbeatLagSeconds") or 0) > 120:
        findings.append({"check": "usageSync.heartbeat", "level": "warn", "detail": f"worker 心跳滞后 {sync.get('heartbeatLagSeconds')}s"})

    oldest = queue.get("oldestAgeSeconds")
    # oldestAgeSeconds 也算 running：同步首尾相接，正在跑的那条可能已排了一个多小时。只看真正积压的 pending。
    if oldest and int(oldest) > 3600 and int(queue.get("pendingCount") or 0) > 0:
        findings.append({
            "check": "usageRefreshQueue",
            "level": "warn",
            "detail": f"待刷新 {queue.get('pendingCount')} 条,最老 {int(oldest) // 86400}d,最多重试 {queue.get('maxAttempts')} 次;最近错误:{queue.get('lastError') or '-'}",
        })

    lag = realtime.get("maxSettlementLagSeconds")
    # 结算水位取整到分钟 + 只做已关闭 60s 的窗口 ⇒ 正常就有 60~140s 滞后；服务端阈值 180s（补丁 0016）
    if realtime.get("status") != "ok" or (lag and int(lag) > 180):
        findings.append({"check": "usageRealtime", "level": "warn", "detail": f"status={realtime.get('status')} maxSettlementLag={lag}s"})

    if outbox.get("failedCount"):
        findings.append({"check": "organization.outbox", "level": "warn", "detail": f"outbox failed={outbox.get('failedCount')} pending={outbox.get('pendingCount')};查 `billing outbox`"})
    settlement = org.get("settlement") or {}
    # 组织结算从没跑过（settledDayCount=0）时，「差异」= 北汽开通计费以来每天的归属用量，不是对不上，报 known。
    if settlement.get("reconciliationDifferenceCount") and not settlement.get("settledDayCount"):
        findings.append({
            "check": "organization.settlement",
            "level": "known",
            "detail": f"组织日结从未写入（settledDayCount=0）；{settlement.get('reconciliationDifferenceCount')} 天归属用量未结算 ${settlement.get('reconciliationDifferenceUsd')}，不是对账错",
        })
    elif settlement.get("reconciliationDifferenceCount"):
        findings.append({
            "check": "organization.settlement",
            "level": "warn",
            "detail": f"对账差异 {settlement.get('reconciliationDifferenceCount')} 笔 / ${settlement.get('reconciliationDifferenceUsd')}",
        })
    if billing.get("pendingSyncCount") or billing.get("pendingReviewCount"):
        findings.append({
            "check": "billing",
            "level": "warn",
            "detail": f"待同步上游 {billing.get('pendingSyncCount')} / 待人工核账 {billing.get('pendingReviewCount')};查 `billing orders`",
        })
    if not (health.get("usageDatabase") or {}).get("connected", True):
        findings.append({"check": "usageDatabase", "level": "error", "detail": "用量库未连接"})

    return {
        "status": health.get("status"),
        "workerId": sync.get("workerId"),
        "lastSuccessAt": sync.get("lastSuccessAt"),
        "publishedAt": sync.get("publishedAt"),
        "snapshotLagSeconds": sync.get("snapshotLagSeconds"),
        "realtimeLagSeconds": lag,
        "refreshPending": queue.get("pendingCount"),
        "findings": findings,
    }
