"""Administrative API for LiteLLM key budget fallback policies."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from contextlib import contextmanager, nullcontext
import os
import socket
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from . import database as db
from .budget_fallback import (
    BudgetFallbackController,
    managed_fingerprint,
    public_generation_models,
    utilization_percent,
)
from .budget_fallback_store import BudgetFallbackStore
from .litellm_budget_client import KeySnapshot, LiteLLMBudgetClient, LiteLLMBudgetError
from .models import (
    BudgetFallbackActionRequest,
    BudgetFallbackDisableRequest,
    BudgetFallbackEnableRequest,
)


router = APIRouter(prefix="/api/litellm/budget-fallback", tags=["litellm"])
ACTION_LEASE_SECONDS = 300


def get_budget_store() -> BudgetFallbackStore:
    return BudgetFallbackStore(db)


def get_budget_client() -> LiteLLMBudgetClient:
    return LiteLLMBudgetClient()


def get_budget_controller(
    store: BudgetFallbackStore = Depends(get_budget_store),
    client: LiteLLMBudgetClient = Depends(get_budget_client),
) -> BudgetFallbackController:
    return BudgetFallbackController(store, client)


def _actor(request: Request) -> str:
    auth = getattr(request.state, "auth", None) or {}
    return str(auth.get("sub") or request.headers.get("x-test-actor") or "admin")


@contextmanager
def _policy_lease(store: BudgetFallbackStore, key_id: str):
    owner = f"api:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
    if not store.acquire_lease(
        key_id, owner, datetime.now(UTC), ACTION_LEASE_SECONDS
    ):
        raise HTTPException(409, "policy is being updated; retry shortly")
    try:
        yield
    finally:
        store.release_lease(key_id, owner)


def _eligibility(snapshot: KeySnapshot, health=None) -> tuple[bool, str]:
    if snapshot.max_budget is None or snapshot.max_budget <= 0:
        return False, "未设置正数周期预算"
    if not snapshot.budget_duration:
        return False, "仅总预算，没有自动重置周期"
    if not snapshot.budget_reset_at:
        return False, "缺少下次预算重置时间"
    try:
        reset_at = datetime.fromisoformat(snapshot.budget_reset_at)
    except ValueError:
        return False, "预算重置时间格式无效"
    if reset_at.tzinfo is None:
        return False, "预算重置时间缺少时区"
    if reset_at.astimezone(UTC) <= datetime.now(UTC):
        return False, "预算重置时间已经过期"
    if snapshot.blocked:
        return False, "Key 已被手动停用"
    if snapshot.key_alias.lower() in {"master", "admin", "litellm-master"}:
        return False, "管理 Key 不允许启用"
    if not public_generation_models(snapshot):
        return False, "没有可映射的公开生成模型"
    if health is not None and (not health.available or not health.zero_cost):
        return False, health.error or "5.3 兜底产品未通过零成本检查"
    return True, ""


def _public_key_row(snapshot: KeySnapshot, policy: dict | None) -> dict:
    eligible, reason = _eligibility(snapshot)
    row = {
        "key_id": snapshot.key_id,
        "key_alias": snapshot.key_alias,
        "models": list(snapshot.models),
        "spend": snapshot.spend,
        "max_budget": snapshot.max_budget,
        "budget_duration": snapshot.budget_duration,
        "budget_reset_at": snapshot.budget_reset_at,
        "blocked": snapshot.blocked,
        "utilization_percent": round(utilization_percent(snapshot), 2),
        "eligible": eligible,
        "eligibility_reason": reason,
        "enabled": False,
        "state": "NORMAL",
        "threshold_percent": 98,
        "automation_paused": False,
        "last_error": "",
    }
    if policy:
        for field in (
            "enabled",
            "state",
            "threshold_percent",
            "automation_paused",
            "last_error",
            "fallback_entered_at",
            "last_observed_at",
        ):
            row[field] = policy.get(field, row.get(field))
        if snapshot.max_budget is None:
            row["max_budget"] = policy.get("original_max_budget")
            row["budget_duration"] = policy.get("original_budget_duration")
            row["budget_reset_at"] = policy.get("original_budget_reset_at")
            original_budget = float(policy.get("original_max_budget") or 0)
            row["utilization_percent"] = round(
                float(policy.get("last_observed_spend") or snapshot.spend or 0)
                / original_budget
                * 100,
                2,
            ) if original_budget > 0 else 0
    return row


@router.get("/keys")
def list_keys(
    store: BudgetFallbackStore = Depends(get_budget_store),
    client: LiteLLMBudgetClient = Depends(get_budget_client),
):
    try:
        snapshots = client.list_budgeted_keys()
        policies = {row["key_id"]: row for row in store.list_policies()}
        snapshots_by_id = {row.key_id: row for row in snapshots}
        for key_id, policy in policies.items():
            if key_id in snapshots_by_id:
                continue
            try:
                snapshots_by_id[key_id] = client.get_key(key_id)
            except LiteLLMBudgetError:
                continue
        return {
            "keys": [
                _public_key_row(row, policies.get(row.key_id))
                for row in sorted(snapshots_by_id.values(), key=lambda item: item.key_alias)
            ],
            "fallback_health": asdict(client.check_fallback_model()),
            "observed_at": datetime.now(UTC).isoformat(),
        }
    except LiteLLMBudgetError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/keys/{key_id}/events")
def list_events(
    key_id: str,
    limit: int = 100,
    store: BudgetFallbackStore = Depends(get_budget_store),
):
    return {"events": store.list_events(key_id, limit)}


@router.get("/metrics")
def metrics(
    store: BudgetFallbackStore = Depends(get_budget_store),
    client: LiteLLMBudgetClient = Depends(get_budget_client),
):
    result = store.metrics_snapshot()
    result["fallback_health"] = asdict(client.check_fallback_model())
    result["observed_at"] = datetime.now(UTC).isoformat()
    return result


@router.post("/keys/{key_id}/enable")
def enable_policy(
    key_id: str,
    body: BudgetFallbackEnableRequest,
    request: Request,
    store: BudgetFallbackStore = Depends(get_budget_store),
    client: LiteLLMBudgetClient = Depends(get_budget_client),
):
    if body.key_id != key_id:
        raise HTTPException(400, "path key_id does not match request body")
    existing = store.get_policy(key_id)
    lease = _policy_lease(store, key_id) if existing is not None else nullcontext()
    with lease:
        try:
            snapshot = client.get_key(key_id)
        except (LiteLLMBudgetError, StopIteration) as exc:
            raise HTTPException(404, "LiteLLM key not found") from exc
        health = client.check_fallback_model()
        eligible, reason = _eligibility(snapshot, health)
        if not eligible:
            status = 409 if health.available and not health.zero_cost else 422
            if not health.available:
                status = 409
            raise HTTPException(status, reason)
        data = {
            "key_id": snapshot.key_id,
            "key_alias": snapshot.key_alias,
            "models": list(snapshot.models),
            "aliases": snapshot.aliases,
            "max_budget": snapshot.max_budget,
            "budget_duration": snapshot.budget_duration,
            "budget_reset_at": snapshot.budget_reset_at,
            "blocked": snapshot.blocked,
            "spend": snapshot.spend,
            "config_fingerprint": managed_fingerprint(snapshot),
        }
        policy = store.enable_policy(data, _actor(request))
    return {"policy": policy, "live": _public_key_row(snapshot, policy)}


@router.post("/keys/{key_id}/disable")
def disable_policy(
    key_id: str,
    body: BudgetFallbackDisableRequest,
    request: Request,
    store: BudgetFallbackStore = Depends(get_budget_store),
    controller: BudgetFallbackController = Depends(get_budget_controller),
):
    policy = store.get_policy(key_id)
    if policy is None:
        raise HTTPException(404, "policy not found")
    with _policy_lease(store, key_id):
        result = None
        if body.restore and policy.get("state") in {"FALLBACK_PENDING", "FALLBACK_5_3", "RESTORING"}:
            result = controller.force_restore(key_id, _actor(request), datetime.now(UTC))
            if result.to_state != "NORMAL":
                raise HTTPException(409, result.error or "restore did not complete")
        policy = store.disable_policy(key_id, _actor(request))
    return {"policy": policy, "result": asdict(result) if result else None}


@router.post("/keys/{key_id}/fallback")
def force_fallback(
    key_id: str,
    body: BudgetFallbackActionRequest,
    request: Request,
    store: BudgetFallbackStore = Depends(get_budget_store),
    controller: BudgetFallbackController = Depends(get_budget_controller),
):
    policy = store.get_policy(key_id)
    if policy is None:
        raise HTTPException(404, "policy not found")
    if policy.get("state") != "NORMAL":
        raise HTTPException(409, "fallback is only allowed from NORMAL")
    with _policy_lease(store, key_id):
        result = controller.force_fallback(key_id, _actor(request), datetime.now(UTC))
    if not result.changed and result.event_type == "invalid_action":
        raise HTTPException(409, result.error)
    return {"result": asdict(result)}


@router.post("/keys/{key_id}/restore")
def force_restore(
    key_id: str,
    body: BudgetFallbackActionRequest,
    request: Request,
    store: BudgetFallbackStore = Depends(get_budget_store),
    controller: BudgetFallbackController = Depends(get_budget_controller),
):
    policy = store.get_policy(key_id)
    if policy is None:
        raise HTTPException(404, "policy not found")
    if policy.get("state") not in {"FALLBACK_PENDING", "FALLBACK_5_3", "RESTORING"}:
        raise HTTPException(409, "restore is only allowed while fallback is active")
    with _policy_lease(store, key_id):
        result = controller.force_restore(key_id, _actor(request), datetime.now(UTC))
    if not result.changed and result.event_type == "invalid_action":
        raise HTTPException(409, result.error)
    return {"result": asdict(result)}


@router.post("/keys/{key_id}/recapture")
def recapture(
    key_id: str,
    body: BudgetFallbackActionRequest,
    request: Request,
    store: BudgetFallbackStore = Depends(get_budget_store),
    controller: BudgetFallbackController = Depends(get_budget_controller),
):
    policy = store.get_policy(key_id)
    if policy is None:
        raise HTTPException(404, "policy not found")
    if policy.get("state") not in {"NORMAL", "MANUAL_HOLD"}:
        raise HTTPException(409, "cannot recapture while fallback is active")
    try:
        with _policy_lease(store, key_id):
            updated = controller.recapture(key_id, _actor(request))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"policy": updated}


@router.post("/keys/{key_id}/pause")
def pause(
    key_id: str,
    request: Request,
    store: BudgetFallbackStore = Depends(get_budget_store),
):
    if store.get_policy(key_id) is None:
        raise HTTPException(404, "policy not found")
    with _policy_lease(store, key_id):
        policy = store.update_policy(key_id, automation_paused=True, updated_by=_actor(request))
    return {"policy": policy}


@router.post("/keys/{key_id}/resume")
def resume(
    key_id: str,
    request: Request,
    store: BudgetFallbackStore = Depends(get_budget_store),
):
    if store.get_policy(key_id) is None:
        raise HTTPException(404, "policy not found")
    with _policy_lease(store, key_id):
        policy = store.update_policy(key_id, automation_paused=False, updated_by=_actor(request))
    return {"policy": policy}
