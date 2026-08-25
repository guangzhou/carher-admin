"""State machine for per-key monetary-budget fallback to GPT-5.3."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .litellm_budget_client import (
    FALLBACK_MODEL_GROUP,
    KeySnapshot,
    LiteLLMBudgetClient,
    LiteLLMBudgetError,
)


_INTERNAL_PREFIXES = (
    "wangsu-",
    "openrouter-",
    "chatgpt-pool-",
    "anthropic.",
    "local-",
)
_NON_GENERATION_MODELS = {"BAAI/bge-m3"}


@dataclass(frozen=True)
class TransitionResult:
    key_id: str
    from_state: str
    to_state: str
    changed: bool
    event_type: str = ""
    error: str = ""


def _managed_fields(value: KeySnapshot | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, KeySnapshot):
        return {
            "models": sorted(value.models),
            "aliases": dict(sorted(value.aliases.items())),
            "max_budget": value.max_budget,
            "budget_duration": value.budget_duration,
            "blocked": value.blocked,
        }
    return {
        "models": sorted(value.get("models") or []),
        "aliases": dict(sorted((value.get("aliases") or {}).items())),
        "max_budget": value.get("max_budget"),
        "budget_duration": value.get("budget_duration"),
        "blocked": bool(value.get("blocked")),
    }


def managed_fingerprint(value: KeySnapshot | dict[str, Any]) -> str:
    encoded = json.dumps(
        _managed_fields(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def utilization_percent(snapshot: KeySnapshot) -> float:
    if snapshot.max_budget is None or snapshot.max_budget <= 0:
        return 0
    return snapshot.spend / snapshot.max_budget * 100


def public_generation_models(snapshot: KeySnapshot) -> list[str]:
    models = []
    for model in snapshot.models:
        if model == FALLBACK_MODEL_GROUP or model in _NON_GENERATION_MODELS:
            continue
        if model.startswith(_INTERNAL_PREFIXES):
            continue
        if model not in models:
            models.append(model)
    return models


def fallback_fields(snapshot: KeySnapshot) -> dict[str, Any]:
    public_models = public_generation_models(snapshot)
    if not public_models:
        raise ValueError("key has no supported public generation models")
    return {
        "models": [*public_models, FALLBACK_MODEL_GROUP],
        "aliases": {model: FALLBACK_MODEL_GROUP for model in public_models},
        "max_budget": None,
        "budget_duration": None,
        "blocked": snapshot.blocked,
    }


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def snapshot_dict(snapshot: KeySnapshot) -> dict[str, Any]:
    data = asdict(snapshot)
    data["models"] = list(snapshot.models)
    return data


class BudgetFallbackController:
    def __init__(self, store, client: LiteLLMBudgetClient):
        self.store = store
        self.client = client

    def run_policy(self, key_id: str, now: datetime) -> TransitionResult:
        policy = self.store.get_policy(key_id)
        if policy is None:
            raise KeyError(key_id)
        state = policy["state"]
        if not policy.get("enabled") or policy.get("automation_paused"):
            return TransitionResult(key_id, state, state, False)

        if state == "NORMAL":
            return self._run_normal(policy, now)
        if state == "FALLBACK_PENDING":
            return self._resume_pending(policy, now)
        if state == "FALLBACK_5_3":
            return self._run_fallback(policy, now)
        if state == "RESTORING":
            return self._restore(policy, now, "automatic_restore", "system")
        return TransitionResult(key_id, state, state, False)

    def force_fallback(
        self, key_id: str, actor: str, now: datetime
    ) -> TransitionResult:
        policy = self.store.get_policy(key_id)
        if policy is None:
            raise KeyError(key_id)
        if policy["state"] != "NORMAL":
            return TransitionResult(
                key_id,
                policy["state"],
                policy["state"],
                False,
                "invalid_action",
                "fallback is only allowed from NORMAL",
            )
        return self._switch(policy, now, "manual_switch", actor)

    def force_restore(
        self, key_id: str, actor: str, now: datetime
    ) -> TransitionResult:
        policy = self.store.get_policy(key_id)
        if policy is None:
            raise KeyError(key_id)
        if policy["state"] not in {"FALLBACK_PENDING", "FALLBACK_5_3", "RESTORING"}:
            return TransitionResult(
                key_id,
                policy["state"],
                policy["state"],
                False,
                "invalid_action",
                "restore is only allowed while fallback is active",
            )
        return self._restore(policy, now, "manual_restore", actor, require_due=False)

    def recapture(self, key_id: str, actor: str) -> dict:
        policy = self.store.get_policy(key_id)
        if policy is None:
            raise KeyError(key_id)
        if policy["state"] not in {"NORMAL", "MANUAL_HOLD"}:
            raise ValueError("cannot recapture while fallback is active")
        current = self.client.get_key(key_id)
        if (
            current.max_budget is None
            or current.max_budget <= 0
            or not current.budget_duration
            or _parse_time(current.budget_reset_at) is None
            or not public_generation_models(current)
        ):
            raise ValueError("current key is not a valid periodic-budget baseline")
        updated = self.store.update_policy(
            key_id,
            key_alias=current.key_alias,
            state="NORMAL",
            enabled=True,
            original_models=list(current.models),
            original_aliases=current.aliases,
            original_max_budget=current.max_budget,
            original_budget_duration=current.budget_duration or "",
            original_budget_reset_at=current.budget_reset_at or "",
            original_blocked=current.blocked,
            original_config_fingerprint=managed_fingerprint(current),
            fallback_config_fingerprint="",
            fallback_entered_at="",
            last_error="",
            updated_by=actor,
        )
        self.store.append_event(
            key_id, "baseline_recaptured", {"key_alias": current.key_alias}, actor
        )
        return updated

    def _observe(self, policy, current: KeySnapshot, now: datetime) -> None:
        changes = {
            "last_observed_spend": current.spend,
            "last_observed_at": now.astimezone(UTC).isoformat(),
        }
        if policy["state"] == "NORMAL" and current.budget_reset_at:
            changes["original_budget_reset_at"] = current.budget_reset_at
        self.store.update_policy(policy["key_id"], **changes)

    def _manual_hold(self, policy, error: str, current=None) -> TransitionResult:
        from_state = policy["state"]
        self.store.update_policy(
            policy["key_id"], state="MANUAL_HOLD", last_error=error
        )
        detail = {"error": error}
        if current is not None:
            detail["observed_fingerprint"] = managed_fingerprint(current)
        self.store.append_event(policy["key_id"], "manual_hold", detail)
        return TransitionResult(
            policy["key_id"], from_state, "MANUAL_HOLD", True, "manual_hold", error
        )

    def _run_normal(self, policy, now: datetime) -> TransitionResult:
        current = self.client.get_key(policy["key_id"])
        self._observe(policy, current, now)
        if current.blocked:
            return self._manual_hold(policy, "key is blocked", current)
        if managed_fingerprint(current) != policy["original_config_fingerprint"]:
            return self._manual_hold(policy, "original key configuration changed", current)
        if utilization_percent(current) < float(policy.get("threshold_percent") or 98):
            return TransitionResult(policy["key_id"], "NORMAL", "NORMAL", False)
        return self._switch(policy, now, "automatic_switch", "system", current)

    def _switch(
        self,
        policy,
        now: datetime,
        event_type: str,
        actor: str,
        current: KeySnapshot | None = None,
    ) -> TransitionResult:
        from_state = policy["state"]
        current = current or self.client.get_key(policy["key_id"])
        if current.blocked:
            return self._manual_hold(policy, "key is blocked", current)
        if managed_fingerprint(current) != policy["original_config_fingerprint"]:
            return self._manual_hold(policy, "original key configuration changed", current)
        health = self.client.check_fallback_model(force_refresh=True)
        if not health.available or not health.zero_cost:
            error = health.error or "fallback model is unavailable or not zero-cost"
            self.store.update_policy(policy["key_id"], last_error=error)
            self.store.append_event(policy["key_id"], "switch_failed", {"error": error})
            return TransitionResult(
                policy["key_id"], from_state, from_state, False, "switch_failed", error
            )
        fields = fallback_fields(current)
        expected = managed_fingerprint(fields)
        self.store.update_policy(
            policy["key_id"],
            state="FALLBACK_PENDING",
            fallback_config_fingerprint=expected,
            last_error="",
        )
        try:
            updated = self.client.update_key(policy["key_id"], **fields)
        except LiteLLMBudgetError as exc:
            updated = self.client.get_key(policy["key_id"])
            if managed_fingerprint(updated) != expected:
                error = str(exc)
                self.store.update_policy(
                    policy["key_id"], state=from_state, last_error=error
                )
                self.store.append_event(
                    policy["key_id"], "switch_failed", {"error": error}
                )
                return TransitionResult(
                    policy["key_id"], from_state, from_state, False, "switch_failed", error
                )
        if managed_fingerprint(updated) != expected:
            return self._manual_hold(policy, "fallback verification mismatch", updated)
        self.store.update_policy(
            policy["key_id"],
            state="FALLBACK_5_3",
            fallback_config_fingerprint=expected,
            fallback_entered_at=now.astimezone(UTC).isoformat(),
            last_error="",
            updated_by=actor,
        )
        self.store.append_event(
            policy["key_id"],
            event_type,
            {
                "before": _managed_fields(current),
                "after": _managed_fields(updated),
            },
            actor,
        )
        return TransitionResult(
            policy["key_id"], from_state, "FALLBACK_5_3", True, event_type
        )

    def _resume_pending(self, policy, now: datetime) -> TransitionResult:
        current = self.client.get_key(policy["key_id"])
        if managed_fingerprint(current) == policy["fallback_config_fingerprint"]:
            self.store.update_policy(
                policy["key_id"],
                state="FALLBACK_5_3",
                fallback_entered_at=policy.get("fallback_entered_at")
                or now.astimezone(UTC).isoformat(),
                last_error="",
            )
            return TransitionResult(
                policy["key_id"], "FALLBACK_PENDING", "FALLBACK_5_3", True
            )
        if managed_fingerprint(current) == policy["original_config_fingerprint"]:
            return self._switch(policy, now, "automatic_switch", "system", current)
        return self._manual_hold(policy, "pending switch configuration conflict", current)

    def _run_fallback(self, policy, now: datetime) -> TransitionResult:
        current = self.client.get_key(policy["key_id"])
        self._observe(policy, current, now)
        if managed_fingerprint(current) != policy["fallback_config_fingerprint"]:
            return self._manual_hold(policy, "fallback key configuration changed", current)
        health = self.client.check_fallback_model()
        if not health.available or not health.zero_cost:
            error = health.error or "fallback model is unavailable or not zero-cost"
            self.store.update_policy(policy["key_id"], last_error=error)
            self.store.append_event(
                policy["key_id"], "fallback_unhealthy", {"error": error}
            )
            return TransitionResult(
                policy["key_id"],
                "FALLBACK_5_3",
                "FALLBACK_5_3",
                False,
                "fallback_unhealthy",
                error,
            )
        if policy.get("last_error"):
            self.store.update_policy(policy["key_id"], last_error="")
        reset_at = _parse_time(policy.get("original_budget_reset_at"))
        if reset_at is None:
            return self._manual_hold(policy, "saved reset time is missing", current)
        if now.astimezone(UTC) < reset_at:
            return TransitionResult(
                policy["key_id"], "FALLBACK_5_3", "FALLBACK_5_3", False
            )
        return self._restore(policy, now, "automatic_restore", "system", current=current)

    def _restore(
        self,
        policy,
        now: datetime,
        event_type: str,
        actor: str,
        current: KeySnapshot | None = None,
        require_due: bool = True,
    ) -> TransitionResult:
        from_state = policy["state"]
        current = current or self.client.get_key(policy["key_id"])
        if policy.get("fallback_config_fingerprint") and managed_fingerprint(current) != policy["fallback_config_fingerprint"]:
            return self._manual_hold(policy, "fallback key configuration changed", current)
        reset_at = _parse_time(policy.get("original_budget_reset_at"))
        if require_due and reset_at and now.astimezone(UTC) < reset_at:
            return TransitionResult(policy["key_id"], from_state, from_state, False)
        self.store.update_policy(policy["key_id"], state="RESTORING", last_error="")
        fields = {
            "models": list(policy["original_models"]),
            "aliases": dict(policy["original_aliases"]),
            "max_budget": policy["original_max_budget"],
            "budget_duration": policy["original_budget_duration"] or None,
            "spend": 0,
            "blocked": bool(policy.get("original_blocked")),
        }
        try:
            updated = self.client.update_key(policy["key_id"], **fields)
        except LiteLLMBudgetError as exc:
            observed = self.client.get_key(policy["key_id"])
            if managed_fingerprint(observed) != policy["original_config_fingerprint"]:
                error = str(exc)
                self.store.update_policy(
                    policy["key_id"], state="FALLBACK_5_3", last_error=error
                )
                self.store.append_event(
                    policy["key_id"], "restore_failed", {"error": error}
                )
                return TransitionResult(
                    policy["key_id"], from_state, "FALLBACK_5_3", False, "restore_failed", error
                )
            updated = observed
        if managed_fingerprint(updated) != policy["original_config_fingerprint"]:
            return self._restore_failed_cost_safe(
                policy, from_state, updated, "restored configuration verification mismatch"
            )
        new_reset = _parse_time(updated.budget_reset_at)
        if updated.spend > 0.000001 or new_reset is None or new_reset <= now.astimezone(UTC):
            return self._restore_failed_cost_safe(
                policy, from_state, updated, "restored budget did not start a new period"
            )
        self.store.update_policy(
            policy["key_id"],
            state="NORMAL",
            original_budget_reset_at=updated.budget_reset_at or "",
            fallback_config_fingerprint="",
            fallback_entered_at="",
            last_observed_spend=updated.spend,
            last_observed_at=now.astimezone(UTC).isoformat(),
            last_error="",
            updated_by=actor,
        )
        self.store.append_event(
            policy["key_id"],
            event_type,
            {
                "after": _managed_fields(updated),
                "budget_reset_at": updated.budget_reset_at,
                "restore_delay_seconds": max(
                    0,
                    (now.astimezone(UTC) - reset_at).total_seconds(),
                ) if reset_at else 0,
                "fallback_duration_seconds": max(
                    0,
                    (
                        now.astimezone(UTC)
                        - (_parse_time(policy.get("fallback_entered_at")) or now.astimezone(UTC))
                    ).total_seconds(),
                ),
            },
            actor,
        )
        return TransitionResult(policy["key_id"], from_state, "NORMAL", True, event_type)

    def _restore_failed_cost_safe(
        self,
        policy,
        from_state: str,
        observed: KeySnapshot,
        error: str,
    ) -> TransitionResult:
        try:
            fields = fallback_fields(observed)
            rolled_back = self.client.update_key(policy["key_id"], **fields)
            if managed_fingerprint(rolled_back) != policy["fallback_config_fingerprint"]:
                error += "; fallback rollback verification mismatch"
        except Exception as exc:
            error += f"; fallback rollback failed: {exc}"
        self.store.update_policy(
            policy["key_id"], state="FALLBACK_5_3", last_error=error
        )
        self.store.append_event(
            policy["key_id"], "restore_failed", {"error": error}
        )
        return TransitionResult(
            policy["key_id"],
            from_state,
            "FALLBACK_5_3",
            False,
            "restore_failed",
            error,
        )
