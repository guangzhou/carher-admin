from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from backend.budget_fallback import (
    BudgetFallbackController,
    fallback_fields,
    managed_fingerprint,
    utilization_percent,
)
from backend.litellm_budget_client import (
    FALLBACK_MODEL_GROUP,
    KeySnapshot,
    LiteLLMBudgetError,
    ModelHealth,
)


NOW = datetime(2026, 7, 13, 12, tzinfo=UTC)


def key(
    spend=20,
    budget=100,
    reset="2026-07-14T00:00:00+00:00",
    blocked=False,
    models=None,
    aliases=None,
):
    return KeySnapshot(
        key_id="hash-1",
        key_alias="cursor-alice",
        models=tuple(
            models
            or ("gpt-5.5", "gpt-5.4", "wangsu-gpt-5.5", "BAAI/bge-m3")
        ),
        aliases=dict(aliases or {"gpt-5.5": "chatgpt-pool-gpt-5.5"}),
        max_budget=budget,
        budget_duration="1d" if budget else None,
        budget_reset_at=reset,
        spend=spend,
        blocked=blocked,
    )


class FakeStore:
    def __init__(self, policy):
        self.policy = dict(policy)
        self.events = []

    def get_policy(self, key_id):
        return dict(self.policy) if self.policy.get("key_id") == key_id else None

    def update_policy(self, key_id, **changes):
        assert key_id == self.policy["key_id"]
        self.policy.update(changes)
        return dict(self.policy)

    def append_event(self, key_id, event_type, detail, actor="system"):
        self.events.append((event_type, detail, actor))
        return len(self.events)


class FakeClient:
    def __init__(self, current, health=None):
        self.current = current
        self.health = health or ModelHealth(True, True, 2)
        self.updates = []
        self.update_error = None
        self.after_update = None

    def get_key(self, key_id):
        assert key_id == self.current.key_id
        return self.current

    def check_fallback_model(self, force_refresh=False):
        self.health_force_refresh = force_refresh
        return self.health

    def update_key(self, key_id, **fields):
        self.updates.append(fields)
        if self.update_error:
            error = self.update_error
            self.update_error = None
            raise error
        if self.after_update is not None:
            self.current = self.after_update
            self.after_update = None
            return self.current
        self.current = replace(
            self.current,
            models=tuple(fields["models"]),
            aliases=dict(fields["aliases"]),
            max_budget=fields["max_budget"],
            budget_duration=fields["budget_duration"],
            spend=(fields["spend"] if fields.get("spend") is not None else self.current.spend),
            blocked=(fields["blocked"] if fields.get("blocked") is not None else self.current.blocked),
            budget_reset_at=(
                (NOW + timedelta(days=1)).isoformat()
                if fields["budget_duration"]
                else None
            ),
        )
        return self.current


def policy_for(snapshot, state="NORMAL"):
    return {
        "key_id": snapshot.key_id,
        "key_alias": snapshot.key_alias,
        "enabled": True,
        "state": state,
        "threshold_percent": 98,
        "original_models": list(snapshot.models),
        "original_aliases": dict(snapshot.aliases),
        "original_max_budget": snapshot.max_budget,
        "original_budget_duration": snapshot.budget_duration,
        "original_budget_reset_at": snapshot.budget_reset_at,
        "original_blocked": snapshot.blocked,
        "original_config_fingerprint": managed_fingerprint(snapshot),
        "fallback_config_fingerprint": "",
        "automation_paused": False,
        "last_observed_spend": snapshot.spend,
        "last_observed_at": "",
        "last_error": "",
    }


def fallback_snapshot(original):
    fields = fallback_fields(original)
    return replace(
        original,
        models=tuple(fields["models"]),
        aliases=fields["aliases"],
        max_budget=None,
        budget_duration=None,
        budget_reset_at=None,
    )


def test_utilization_is_division_safe_and_not_clamped():
    assert utilization_percent(key(spend=98)) == 98
    assert utilization_percent(key(spend=120)) == 120
    assert utilization_percent(key(budget=0)) == 0


def test_fallback_fields_preserve_public_names_but_remove_internal_routes():
    fields = fallback_fields(key())

    assert fields["models"] == ["gpt-5.5", "gpt-5.4", FALLBACK_MODEL_GROUP]
    assert fields["aliases"] == {
        "gpt-5.5": FALLBACK_MODEL_GROUP,
        "gpt-5.4": FALLBACK_MODEL_GROUP,
    }
    assert fields["max_budget"] is None
    assert fields["budget_duration"] is None


def test_90_percent_only_records_observation():
    current = key(spend=90)
    store = FakeStore(policy_for(current))
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.changed is False
    assert result.to_state == "NORMAL"
    assert client.updates == []
    assert store.policy["last_observed_spend"] == 90


def test_normal_observation_tracks_the_current_budget_reset_deadline():
    current = key(spend=20, reset="2026-07-15T00:00:00+00:00")
    policy = policy_for(current)
    policy["original_budget_reset_at"] = "2026-07-14T00:00:00+00:00"
    store = FakeStore(policy)
    client = FakeClient(current)

    BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert store.policy["original_budget_reset_at"] == current.budget_reset_at


def test_98_percent_switches_and_verifies_fallback():
    current = key(spend=98)
    store = FakeStore(policy_for(current))
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "FALLBACK_5_3"
    assert result.event_type == "automatic_switch"
    assert client.health_force_refresh is True
    assert client.updates[0]["models"] == [
        "gpt-5.5",
        "gpt-5.4",
        FALLBACK_MODEL_GROUP,
    ]
    assert set(client.updates[0]["aliases"].values()) == {FALLBACK_MODEL_GROUP}
    assert store.policy["fallback_config_fingerprint"] == managed_fingerprint(
        client.current
    )


def test_manual_fallback_rejects_an_already_active_fallback():
    original = key(spend=98)
    current = fallback_snapshot(original)
    policy = policy_for(original, "FALLBACK_5_3")
    policy["fallback_config_fingerprint"] = managed_fingerprint(current)
    store = FakeStore(policy)
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).force_fallback(
        "hash-1", "admin", NOW
    )

    assert result.to_state == "FALLBACK_5_3"
    assert result.changed is False
    assert result.event_type == "invalid_action"
    assert client.updates == []


def test_blocked_key_enters_manual_hold_without_update():
    current = key(spend=98, blocked=True)
    store = FakeStore(policy_for(current))
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "MANUAL_HOLD"
    assert client.updates == []


def test_non_zero_cost_fallback_does_not_switch():
    current = key(spend=98)
    store = FakeStore(policy_for(current))
    client = FakeClient(current, ModelHealth(True, False, 2, "not zero"))

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "NORMAL"
    assert result.event_type == "switch_failed"
    assert client.updates == []


def test_fallback_waits_until_saved_reset_deadline():
    original = key(spend=98)
    current = fallback_snapshot(original)
    policy = policy_for(original, "FALLBACK_5_3")
    policy["fallback_config_fingerprint"] = managed_fingerprint(current)
    store = FakeStore(policy)
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).run_policy(
        "hash-1", NOW - timedelta(hours=1)
    )

    assert result.changed is False
    assert result.to_state == "FALLBACK_5_3"
    assert client.updates == []


def test_unhealthy_fallback_stays_cost_safe_and_reports_event():
    original = key(spend=98)
    current = fallback_snapshot(original)
    policy = policy_for(original, "FALLBACK_5_3")
    policy["fallback_config_fingerprint"] = managed_fingerprint(current)
    store = FakeStore(policy)
    client = FakeClient(current, ModelHealth(False, False, 0, "upstream missing"))

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "FALLBACK_5_3"
    assert result.event_type == "fallback_unhealthy"
    assert "upstream missing" in result.error
    assert client.updates == []
    assert store.policy["last_error"] == "upstream missing"


def test_restore_sets_spend_zero_and_original_fields():
    original = key(spend=98)
    current = fallback_snapshot(original)
    policy = policy_for(original, "FALLBACK_5_3")
    policy["fallback_config_fingerprint"] = managed_fingerprint(current)
    policy["original_budget_reset_at"] = (NOW - timedelta(seconds=1)).isoformat()
    store = FakeStore(policy)
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "NORMAL"
    assert result.event_type == "automatic_restore"
    assert client.updates[0]["models"] == list(original.models)
    assert client.updates[0]["aliases"] == original.aliases
    assert client.updates[0]["max_budget"] == 100
    assert client.updates[0]["budget_duration"] == "1d"
    assert client.updates[0]["spend"] == 0
    assert store.policy["original_budget_reset_at"] > NOW.isoformat()


def test_manual_restore_rejects_normal_state_without_clearing_spend():
    current = key(spend=65)
    store = FakeStore(policy_for(current))
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).force_restore(
        "hash-1", "admin", NOW
    )

    assert result.to_state == "NORMAL"
    assert result.changed is False
    assert result.event_type == "invalid_action"
    assert client.updates == []


def test_recapture_rejects_active_fallback():
    original = key(spend=98)
    current = fallback_snapshot(original)
    policy = policy_for(original, "FALLBACK_5_3")
    policy["fallback_config_fingerprint"] = managed_fingerprint(current)
    store = FakeStore(policy)
    client = FakeClient(current)

    try:
        BudgetFallbackController(store, client).recapture("hash-1", "admin")
    except ValueError as exc:
        assert "fallback" in str(exc)
    else:
        raise AssertionError("recapture must reject an active fallback")

    assert store.policy["state"] == "FALLBACK_5_3"


def test_mismatched_fallback_fingerprint_enters_manual_hold():
    original = key(spend=98)
    current = fallback_snapshot(original)
    current = replace(current, aliases={"gpt-5.5": "someone-else"})
    policy = policy_for(original, "FALLBACK_5_3")
    policy["fallback_config_fingerprint"] = "expected"
    store = FakeStore(policy)
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "MANUAL_HOLD"
    assert client.updates == []


def test_write_timeout_rereads_before_deciding_switch_failed():
    original = key(spend=98)
    switched = fallback_snapshot(original)
    store = FakeStore(policy_for(original))
    client = FakeClient(original)
    client.update_error = LiteLLMBudgetError("timeout")

    def update_then_timeout(key_id, **fields):
        client.updates.append(fields)
        client.current = switched
        raise LiteLLMBudgetError("timeout")

    client.update_key = update_then_timeout

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "FALLBACK_5_3"
    assert result.event_type == "automatic_switch"


def test_failed_restore_keeps_fallback_state():
    original = key(spend=98)
    current = fallback_snapshot(original)
    policy = policy_for(original, "FALLBACK_5_3")
    policy["fallback_config_fingerprint"] = managed_fingerprint(current)
    policy["original_budget_reset_at"] = (NOW - timedelta(seconds=1)).isoformat()
    store = FakeStore(policy)
    client = FakeClient(current)
    client.update_error = LiteLLMBudgetError("write failed")

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "FALLBACK_5_3"
    assert result.event_type == "restore_failed"


def test_invalid_new_budget_period_rolls_live_key_back_to_fallback():
    original = key(spend=98)
    current = fallback_snapshot(original)
    policy = policy_for(original, "FALLBACK_5_3")
    policy["fallback_config_fingerprint"] = managed_fingerprint(current)
    policy["original_budget_reset_at"] = (NOW - timedelta(seconds=1)).isoformat()
    store = FakeStore(policy)
    client = FakeClient(current)

    restored_without_new_period = replace(
        original,
        spend=0,
        budget_reset_at=(NOW - timedelta(seconds=1)).isoformat(),
    )
    client.after_update = restored_without_new_period

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "FALLBACK_5_3"
    assert result.event_type == "restore_failed"
    assert len(client.updates) == 2
    assert client.updates[1]["max_budget"] is None
    assert set(client.updates[1]["aliases"].values()) == {FALLBACK_MODEL_GROUP}
    assert managed_fingerprint(client.current) == policy["fallback_config_fingerprint"]


def test_restart_from_fallback_pending_finishes_switch():
    original = key(spend=98)
    current = fallback_snapshot(original)
    policy = policy_for(original, "FALLBACK_PENDING")
    policy["fallback_config_fingerprint"] = managed_fingerprint(current)
    store = FakeStore(policy)
    client = FakeClient(current)

    result = BudgetFallbackController(store, client).run_policy("hash-1", NOW)

    assert result.to_state == "FALLBACK_5_3"
    assert client.updates == []
