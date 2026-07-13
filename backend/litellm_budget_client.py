"""Typed LiteLLM operations used by the budget fallback controller."""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from . import litellm_ops


FALLBACK_MODEL_GROUP = "chatgpt-budget-fallback-gpt-5.3"
FALLBACK_HEALTH_CACHE_SECONDS = 60


class Transport(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int = 15,
    ) -> Any: ...


class _DefaultTransport:
    request_json = staticmethod(litellm_ops.request_json)


class LiteLLMBudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeySnapshot:
    key_id: str
    key_alias: str
    models: tuple[str, ...]
    aliases: dict[str, str]
    max_budget: float | None
    budget_duration: str | None
    budget_reset_at: str | None
    spend: float
    blocked: bool


@dataclass(frozen=True)
class ModelHealth:
    available: bool
    zero_cost: bool
    deployment_count: int
    error: str = ""


_fallback_health_cache: tuple[float, ModelHealth] | None = None


def _sanitize_error(value: Exception | str) -> str:
    text = str(value)
    text = re.sub(r"Bearer\s+[^\s,;]+", "Bearer [REDACTED]", text, flags=re.I)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-[REDACTED]", text)
    return text[:500]


def _iso_utc(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _snapshot(row: dict) -> KeySnapshot:
    key_id = str(row.get("token") or row.get("key") or row.get("key_id") or "")
    if not key_id:
        raise LiteLLMBudgetError("LiteLLM key response is missing the token hash")
    return KeySnapshot(
        key_id=key_id,
        key_alias=str(row.get("key_alias") or key_id),
        models=tuple(str(model) for model in (row.get("models") or [])),
        aliases={str(key): str(value) for key, value in (row.get("aliases") or {}).items()},
        max_budget=(float(row["max_budget"]) if row.get("max_budget") is not None else None),
        budget_duration=(str(row["budget_duration"]) if row.get("budget_duration") else None),
        budget_reset_at=_iso_utc(row.get("budget_reset_at")),
        spend=float(row.get("spend") or 0),
        blocked=bool(row.get("blocked")),
    )


class LiteLLMBudgetClient:
    def __init__(self, transport: Transport | None = None):
        self.transport = transport or _DefaultTransport()

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: int = 15):
        try:
            return self.transport.request_json(method, path, payload, timeout)
        except Exception as exc:
            raise LiteLLMBudgetError(_sanitize_error(exc)) from exc

    def list_budgeted_keys(self, limit: int = 2000) -> list[KeySnapshot]:
        payload = self._request("GET", f"/spend/keys?limit={int(limit)}")
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        result = []
        for row in rows or []:
            if row.get("max_budget") is None or float(row.get("max_budget") or 0) <= 0:
                continue
            if not row.get("budget_duration"):
                continue
            try:
                result.append(_snapshot(row))
            except (TypeError, ValueError, LiteLLMBudgetError):
                continue
        return result

    def get_key(self, key_id: str) -> KeySnapshot:
        path = "/key/info?key=" + urllib.parse.quote(key_id, safe="")
        payload = self._request("GET", path)
        row = payload.get("info", payload) if isinstance(payload, dict) else payload
        if not isinstance(row, dict):
            raise LiteLLMBudgetError("LiteLLM key response is not an object")
        return _snapshot(row)

    def update_key(
        self,
        key_id: str,
        *,
        models: list[str],
        aliases: dict[str, str],
        max_budget: float | None,
        budget_duration: str | None,
        spend: float | None = None,
        blocked: bool | None = None,
    ) -> KeySnapshot:
        body: dict[str, Any] = {
            "key": key_id,
            "models": list(models),
            "aliases": dict(aliases),
            "max_budget": max_budget,
            "budget_duration": budget_duration,
        }
        if spend is not None:
            body["spend"] = spend
        if blocked is not None:
            body["blocked"] = blocked
        self._request("POST", "/key/update", body, timeout=45)
        return self.get_key(key_id)

    def check_fallback_model(self) -> ModelHealth:
        global _fallback_health_cache
        use_cache = isinstance(self.transport, _DefaultTransport)
        if use_cache and _fallback_health_cache is not None:
            cached_at, cached_health = _fallback_health_cache
            if time.monotonic() - cached_at < FALLBACK_HEALTH_CACHE_SECONDS:
                return cached_health
        try:
            payload = self._request("GET", "/v1/model/info")
            rows = payload.get("data", payload) if isinstance(payload, dict) else payload
            matches = [row for row in (rows or []) if row.get("model_name") == FALLBACK_MODEL_GROUP]
            if not matches:
                result = ModelHealth(False, False, 0, "fallback model group not found")
                if use_cache:
                    _fallback_health_cache = (time.monotonic(), result)
                return result
            required = (
                "input_cost_per_token",
                "output_cost_per_token",
                "cache_read_input_token_cost",
            )
            zero_cost = all(
                all(field in (row.get("litellm_params") or {}) and float(row["litellm_params"][field]) == 0 for field in required)
                for row in matches
            )
            if not zero_cost:
                result = ModelHealth(True, False, len(matches), "fallback cost is not zero")
                if use_cache:
                    _fallback_health_cache = (time.monotonic(), result)
                return result
            health_path = "/health?model=" + urllib.parse.quote(
                FALLBACK_MODEL_GROUP, safe=""
            )
            health_payload = self._request("GET", health_path, timeout=45)
            healthy_count = int(health_payload.get("healthy_count") or 0)
            if healthy_count <= 0:
                result = ModelHealth(
                    False,
                    True,
                    0,
                    "fallback model group has no healthy deployments",
                )
                if use_cache:
                    _fallback_health_cache = (time.monotonic(), result)
                return result
            result = ModelHealth(True, True, healthy_count)
            if use_cache:
                _fallback_health_cache = (time.monotonic(), result)
            return result
        except LiteLLMBudgetError as exc:
            result = ModelHealth(False, False, 0, str(exc))
            if use_cache:
                _fallback_health_cache = (time.monotonic(), result)
            return result
