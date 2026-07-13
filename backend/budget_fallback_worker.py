"""Background scheduler and edge alerts for budget fallback policies."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import urllib.request
from datetime import UTC, datetime

from . import database as db
from .budget_fallback import BudgetFallbackController, TransitionResult
from .budget_fallback_store import BudgetFallbackStore
from .litellm_budget_client import LiteLLMBudgetClient


logger = logging.getLogger("carher-admin")
WORKER_TICK_SECONDS = 5
_worker_task: asyncio.Task | None = None
_worker_id = f"{socket.gethostname()}:{os.getpid()}"


def _default_store() -> BudgetFallbackStore:
    return BudgetFallbackStore(db)


def _default_controller(store=None) -> BudgetFallbackController:
    return BudgetFallbackController(store or _default_store(), LiteLLMBudgetClient())


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _policy_interval(policy: dict) -> int:
    state = policy.get("state") or "NORMAL"
    if state in {"FALLBACK_5_3", "MANUAL_HOLD"}:
        return 60
    if state in {"FALLBACK_PENDING", "RESTORING"}:
        return 5
    budget = float(policy.get("original_max_budget") or 0)
    spend = float(policy.get("last_observed_spend") or 0)
    return 5 if budget > 0 and spend / budget >= 0.9 else 30


def _is_due(policy: dict, now: datetime) -> bool:
    last = _parse_time(policy.get("last_observed_at"))
    return last is None or (now.astimezone(UTC) - last).total_seconds() >= _policy_interval(policy)


def _sanitize(value: Exception | str) -> str:
    text = str(value)
    text = re.sub(r"Bearer\s+[^\s,;]+", "Bearer [REDACTED]", text, flags=re.I)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-[REDACTED]", text)
    return text[:500]


async def run_budget_fallback_cycle(
    now: datetime | None = None,
    *,
    store: BudgetFallbackStore | None = None,
    controller: BudgetFallbackController | None = None,
) -> list[TransitionResult]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    store = store or _default_store()
    controller = controller or _default_controller(store)
    results: list[TransitionResult] = []
    for policy in store.list_policies():
        if not policy.get("enabled") or policy.get("automation_paused"):
            continue
        if not _is_due(policy, now):
            continue
        key_id = policy["key_id"]
        if not store.acquire_lease(key_id, _worker_id, now, 30):
            continue
        try:
            try:
                result = await asyncio.to_thread(controller.run_policy, key_id, now)
            except Exception as exc:
                error = _sanitize(exc)
                result = TransitionResult(
                    key_id,
                    policy.get("state") or "",
                    policy.get("state") or "",
                    False,
                    "worker_failed",
                    error,
                )
                logger.error("Budget fallback worker failed for %s: %s", key_id, error)
            results.append(result)
            notify_transition(result)
        finally:
            store.release_lease(key_id, _worker_id)
    return results


def post_feishu_text(text: str) -> None:
    webhook = db.get_setting("feishu_webhook") or os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook or webhook.startswith("stub"):
        return
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    request = urllib.request.Request(
        webhook,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except Exception as exc:
        logger.warning("Budget fallback Feishu alert failed: %s", _sanitize(exc))


def notify_transition(result: TransitionResult) -> None:
    edge_events = {
        "automatic_switch",
        "switch_failed",
        "automatic_restore",
        "restore_failed",
        "manual_hold",
        "fallback_unhealthy",
    }
    if result.event_type not in edge_events:
        return
    message = (
        "LiteLLM budget fallback\n"
        f"key={result.key_id}\n"
        f"event={result.event_type}\n"
        f"state={result.from_state}->{result.to_state}"
    )
    if result.error:
        message += f"\nerror={_sanitize(result.error)}"
    try:
        post_feishu_text(message)
    except Exception as exc:
        logger.warning("Budget fallback notification failed: %s", _sanitize(exc))


async def _worker_loop() -> None:
    while True:
        try:
            await run_budget_fallback_cycle()
        except Exception as exc:
            logger.error("Budget fallback cycle failed: %s", _sanitize(exc))
        await asyncio.sleep(WORKER_TICK_SECONDS)


def start_budget_fallback_worker() -> asyncio.Task:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker_loop())
        logger.info("LiteLLM budget fallback worker started")
    return _worker_task
