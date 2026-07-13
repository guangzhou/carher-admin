"""Persistence for per-key LiteLLM budget fallback policies."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Literal


PolicyState = Literal[
    "NORMAL",
    "FALLBACK_PENDING",
    "FALLBACK_5_3",
    "RESTORING",
    "MANUAL_HOLD",
]

_JSON_COLUMNS = {"original_models", "original_aliases"}
_BOOL_COLUMNS = {"enabled", "original_blocked", "automation_paused"}
_SECRET_FIELDS = {
    "api_key",
    "key",
    "token",
    "authorization",
    "secret",
    "password",
}
_UPDATABLE_FIELDS = {
    "key_alias",
    "enabled",
    "state",
    "threshold_percent",
    "original_models",
    "original_aliases",
    "original_max_budget",
    "original_budget_duration",
    "original_budget_reset_at",
    "original_blocked",
    "original_config_fingerprint",
    "fallback_config_fingerprint",
    "fallback_entered_at",
    "last_observed_spend",
    "last_observed_at",
    "last_error",
    "automation_paused",
    "updated_by",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SECRET_FIELDS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class BudgetFallbackStore:
    def __init__(self, database_module):
        self.db = database_module

    @staticmethod
    def _decode_row(row) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        for column in _JSON_COLUMNS:
            data[column] = json.loads(data.get(column) or ("[]" if column.endswith("models") else "{}"))
        for column in _BOOL_COLUMNS:
            data[column] = bool(data.get(column))
        return data

    def list_policies(self) -> list[dict]:
        with self.db.get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM litellm_budget_fallback_policies ORDER BY key_alias"
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_policy(self, key_id: str) -> dict | None:
        with self.db.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM litellm_budget_fallback_policies WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        return self._decode_row(row)

    def enable_policy(self, key_snapshot: dict, actor: str) -> dict:
        now = _now_iso()
        values = {
            "key_id": key_snapshot["key_id"],
            "key_alias": key_snapshot.get("key_alias") or key_snapshot["key_id"],
            "models": _json(key_snapshot.get("models") or []),
            "aliases": _json(key_snapshot.get("aliases") or {}),
            "max_budget": key_snapshot.get("max_budget"),
            "budget_duration": key_snapshot.get("budget_duration") or "",
            "budget_reset_at": key_snapshot.get("budget_reset_at") or "",
            "blocked": int(bool(key_snapshot.get("blocked"))),
            "fingerprint": key_snapshot.get("config_fingerprint") or "",
        }
        with self.db.get_db() as conn:
            conn.execute(
                """
                INSERT INTO litellm_budget_fallback_policies (
                    key_id, key_alias, enabled, state, threshold_percent,
                    original_models, original_aliases, original_max_budget,
                    original_budget_duration, original_budget_reset_at,
                    original_blocked, original_config_fingerprint,
                    fallback_config_fingerprint, fallback_entered_at,
                    last_observed_spend, last_observed_at, last_error,
                    automation_paused, lease_owner, lease_expires_at,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, 1, 'NORMAL', 98, ?, ?, ?, ?, ?, ?, ?, '', '', 0, '', '', 0, '', '', ?, ?, ?, ?)
                ON CONFLICT(key_id) DO UPDATE SET
                    key_alias = excluded.key_alias,
                    enabled = 1,
                    state = 'NORMAL',
                    threshold_percent = 98,
                    original_models = excluded.original_models,
                    original_aliases = excluded.original_aliases,
                    original_max_budget = excluded.original_max_budget,
                    original_budget_duration = excluded.original_budget_duration,
                    original_budget_reset_at = excluded.original_budget_reset_at,
                    original_blocked = excluded.original_blocked,
                    original_config_fingerprint = excluded.original_config_fingerprint,
                    fallback_config_fingerprint = '',
                    fallback_entered_at = '',
                    last_error = '',
                    automation_paused = 0,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (
                    values["key_id"],
                    values["key_alias"],
                    values["models"],
                    values["aliases"],
                    values["max_budget"],
                    values["budget_duration"],
                    values["budget_reset_at"],
                    values["blocked"],
                    values["fingerprint"],
                    actor,
                    actor,
                    now,
                    now,
                ),
            )
        self.db.backup_to_nas()
        self.append_event(values["key_id"], "policy_enabled", {"key_alias": values["key_alias"]}, actor)
        return self.get_policy(values["key_id"])

    def update_policy(self, key_id: str, **changes) -> dict:
        invalid = set(changes) - _UPDATABLE_FIELDS
        if invalid:
            raise ValueError(f"unsupported policy fields: {sorted(invalid)}")
        if not changes:
            policy = self.get_policy(key_id)
            if policy is None:
                raise KeyError(key_id)
            return policy
        values = dict(changes)
        for column in _JSON_COLUMNS & values.keys():
            values[column] = _json(values[column])
        for column in _BOOL_COLUMNS & values.keys():
            values[column] = int(bool(values[column]))
        values["updated_at"] = _now_iso()
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self.db.get_db() as conn:
            cursor = conn.execute(
                f"UPDATE litellm_budget_fallback_policies SET {assignments} WHERE key_id = ?",
                (*values.values(), key_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(key_id)
        self.db.backup_to_nas()
        return self.get_policy(key_id)

    def disable_policy(self, key_id: str, actor: str) -> dict:
        row = self.update_policy(key_id, enabled=False, updated_by=actor)
        self.append_event(key_id, "policy_disabled", {}, actor)
        return row

    def append_event(
        self,
        key_id: str,
        event_type: str,
        detail: dict,
        actor: str = "system",
    ) -> int:
        with self.db.get_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO litellm_budget_fallback_events (key_id, event_type, actor, detail)
                VALUES (?, ?, ?, ?)
                """,
                (key_id, event_type, actor, _json(_redact(detail))),
            )
            event_id = int(cursor.lastrowid)
        self.db.backup_to_nas()
        return event_id

    def list_events(self, key_id: str, limit: int = 100) -> list[dict]:
        with self.db.get_db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM litellm_budget_fallback_events
                WHERE key_id = ? ORDER BY id DESC LIMIT ?
                """,
                (key_id, max(1, min(limit, 500))),
            ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.get("detail") or "{}")
            events.append(item)
        return events

    def acquire_lease(
        self,
        key_id: str,
        owner: str,
        now: datetime,
        ttl_seconds: int = 30,
    ) -> bool:
        now_iso = now.astimezone(UTC).isoformat()
        expires = (now.astimezone(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        with self.db.get_db() as conn:
            cursor = conn.execute(
                """
                UPDATE litellm_budget_fallback_policies
                SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE key_id = ?
                  AND (lease_owner = '' OR lease_owner = ? OR lease_expires_at <= ?)
                """,
                (owner, expires, now_iso, key_id, owner, now_iso),
            )
        if cursor.rowcount == 1:
            self.db.backup_to_nas()
            return True
        return False

    def release_lease(self, key_id: str, owner: str) -> None:
        with self.db.get_db() as conn:
            cursor = conn.execute(
                """
                UPDATE litellm_budget_fallback_policies
                SET lease_owner = '', lease_expires_at = '', updated_at = ?
                WHERE key_id = ? AND lease_owner = ?
                """,
                (_now_iso(), key_id, owner),
            )
        if cursor.rowcount:
            self.db.backup_to_nas()

    def metrics_snapshot(self, now: datetime | None = None) -> dict:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        policies = self.list_policies()
        enabled = [row for row in policies if row["enabled"]]
        states = Counter(row["state"] for row in enabled)
        with self.db.get_db() as conn:
            rows = conn.execute(
                "SELECT event_type, detail FROM litellm_budget_fallback_events"
            ).fetchall()
        transitions = Counter()
        failures = 0
        restore_delays = []
        for row in rows:
            event_type = row["event_type"]
            transitions[event_type] += 1
            if event_type in {"switch_failed", "restore_failed", "worker_failed", "fallback_unhealthy"}:
                failures += 1
            detail = json.loads(row["detail"] or "{}")
            if detail.get("restore_delay_seconds") is not None:
                restore_delays.append(float(detail["restore_delay_seconds"]))
        current_fallback_seconds = 0.0
        for policy in enabled:
            if policy["state"] != "FALLBACK_5_3" or not policy.get("fallback_entered_at"):
                continue
            entered = datetime.fromisoformat(policy["fallback_entered_at"])
            if entered.tzinfo is None:
                entered = entered.replace(tzinfo=UTC)
            current_fallback_seconds += max(0, (now - entered.astimezone(UTC)).total_seconds())
        return {
            "enabled_policies": len(enabled),
            "states": dict(states),
            "transitions": dict(transitions),
            "failures": failures,
            "current_fallback_seconds": round(current_fallback_seconds, 3),
            "average_restore_delay_seconds": round(
                sum(restore_delays) / len(restore_delays), 3
            ) if restore_delays else 0,
        }
