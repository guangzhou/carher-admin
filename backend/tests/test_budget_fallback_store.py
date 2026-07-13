import sqlite3
from datetime import UTC, datetime, timedelta

from backend.budget_fallback_store import BudgetFallbackStore


def snapshot() -> dict:
    return {
        "key_id": "hash-1",
        "key_alias": "cursor-alice",
        "models": ["gpt-5.5", "gpt-5.4"],
        "aliases": {"gpt-5.5": "chatgpt-pool-gpt-5.5"},
        "max_budget": 100.0,
        "budget_duration": "1d",
        "budget_reset_at": "2026-07-14T00:00:00+00:00",
        "blocked": False,
        "config_fingerprint": "original-fingerprint",
    }


def test_schema_contains_budget_fallback_tables(db):
    conn = sqlite3.connect(str(db.DB_PATH))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()

    assert {
        "litellm_budget_fallback_policies",
        "litellm_budget_fallback_events",
    } <= tables


def test_enable_policy_persists_original_snapshot(db):
    store = BudgetFallbackStore(db)

    row = store.enable_policy(snapshot(), actor="admin")

    assert row["enabled"] is True
    assert row["state"] == "NORMAL"
    assert row["original_models"] == ["gpt-5.5", "gpt-5.4"]
    assert row["original_aliases"] == {"gpt-5.5": "chatgpt-pool-gpt-5.5"}
    assert row["original_max_budget"] == 100.0
    assert row["original_config_fingerprint"] == "original-fingerprint"


def test_update_policy_round_trips_json_and_boolean_fields(db):
    store = BudgetFallbackStore(db)
    store.enable_policy(snapshot(), actor="admin")

    row = store.update_policy(
        "hash-1",
        state="FALLBACK_5_3",
        fallback_config_fingerprint="fallback-fingerprint",
        automation_paused=True,
        last_error="none",
    )

    assert row["state"] == "FALLBACK_5_3"
    assert row["fallback_config_fingerprint"] == "fallback-fingerprint"
    assert row["automation_paused"] is True


def test_lease_allows_only_one_owner_until_expiry(db):
    store = BudgetFallbackStore(db)
    store.enable_policy(snapshot(), actor="admin")
    now = datetime(2026, 7, 13, tzinfo=UTC)

    assert store.acquire_lease("hash-1", "worker-a", now, 30) is True
    assert (
        store.acquire_lease("hash-1", "worker-b", now + timedelta(seconds=5), 30)
        is False
    )
    assert (
        store.acquire_lease("hash-1", "worker-b", now + timedelta(seconds=31), 30)
        is True
    )


def test_release_lease_requires_matching_owner(db):
    store = BudgetFallbackStore(db)
    store.enable_policy(snapshot(), actor="admin")
    now = datetime(2026, 7, 13, tzinfo=UTC)
    store.acquire_lease("hash-1", "worker-a", now, 30)

    store.release_lease("hash-1", "worker-b")
    assert store.get_policy("hash-1")["lease_owner"] == "worker-a"

    store.release_lease("hash-1", "worker-a")
    assert store.get_policy("hash-1")["lease_owner"] == ""


def test_events_redact_secret_shaped_fields(db):
    store = BudgetFallbackStore(db)
    store.enable_policy(snapshot(), actor="admin")

    store.append_event(
        "hash-1",
        "switched",
        {
            "api_key": "sk-secret",
            "nested": {"authorization": "Bearer secret"},
            "state": "FALLBACK_5_3",
        },
    )

    event = store.list_events("hash-1")[0]
    assert "sk-secret" not in str(event)
    assert "Bearer secret" not in str(event)
    assert event["detail"]["state"] == "FALLBACK_5_3"


def test_disable_policy_keeps_saved_snapshot(db):
    store = BudgetFallbackStore(db)
    store.enable_policy(snapshot(), actor="admin")

    row = store.disable_policy("hash-1", actor="admin")

    assert row["enabled"] is False
    assert row["original_models"] == ["gpt-5.5", "gpt-5.4"]


def test_metrics_snapshot_counts_states_events_and_fallback_age(db):
    store = BudgetFallbackStore(db)
    store.enable_policy(snapshot(), actor="admin")
    store.update_policy(
        "hash-1",
        state="FALLBACK_5_3",
        fallback_entered_at="2026-07-13T00:00:00+00:00",
    )
    store.append_event("hash-1", "automatic_switch", {})
    store.append_event(
        "hash-1",
        "automatic_restore",
        {"restore_delay_seconds": 12, "fallback_duration_seconds": 3600},
    )
    store.append_event("hash-1", "restore_failed", {"error": "failed"})

    metrics = store.metrics_snapshot(datetime(2026, 7, 13, 1, tzinfo=UTC))

    assert metrics["enabled_policies"] == 1
    assert metrics["states"] == {"FALLBACK_5_3": 1}
    assert metrics["transitions"]["automatic_switch"] == 1
    assert metrics["failures"] == 1
    assert metrics["current_fallback_seconds"] == 3600
    assert metrics["average_restore_delay_seconds"] == 12
