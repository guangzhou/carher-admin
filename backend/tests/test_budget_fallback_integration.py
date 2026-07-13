from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.budget_fallback import BudgetFallbackController, managed_fingerprint
from backend.budget_fallback_store import BudgetFallbackStore
from backend.litellm_budget_client import (
    FALLBACK_MODEL_GROUP,
    LiteLLMBudgetClient,
)


class StatefulLiteLLM:
    def __init__(self, now: datetime):
        self.now = now
        self.audit_tokens = 0
        self.row = {
            "token": "hash-integration",
            "key_alias": "cursor-integration",
            "models": ["gpt-5.5", "gpt-5.4", "wangsu-gpt-5.5"],
            "aliases": {"gpt-5.5": "chatgpt-pool-gpt-5.5"},
            "max_budget": 100,
            "budget_duration": "1d",
            "budget_reset_at": (now + timedelta(hours=1)).isoformat(),
            "spend": 98,
            "blocked": False,
        }

    def request_json(self, method, path, payload=None, timeout=15):
        if path.startswith("/key/info"):
            return {"info": dict(self.row)}
        if path == "/key/update":
            for field in (
                "models", "aliases", "max_budget", "budget_duration", "spend", "blocked"
            ):
                if field in payload:
                    self.row[field] = payload[field]
            self.row["budget_reset_at"] = (
                (self.now + timedelta(days=1)).isoformat()
                if payload.get("budget_duration")
                else None
            )
            return {"status": "ok"}
        if path == "/v1/model/info":
            return {
                "data": [
                    {
                        "model_name": FALLBACK_MODEL_GROUP,
                        "litellm_params": {
                            "input_cost_per_token": 0,
                            "output_cost_per_token": 0,
                            "cache_read_input_token_cost": 0,
                        },
                    }
                ]
            }
        if path == f"/health?model={FALLBACK_MODEL_GROUP}":
            return {"healthy_count": 1, "unhealthy_count": 0}
        raise AssertionError(f"unexpected request: {method} {path}")

    def simulate_fallback_usage(self, tokens: int):
        assert set(self.row["aliases"].values()) == {FALLBACK_MODEL_GROUP}
        self.audit_tokens += tokens
        # The isolated model group records tokens but has zero monetary cost.
        assert self.row["spend"] == 98


def test_full_budget_switch_and_restore_cycle(db):
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)
    transport = StatefulLiteLLM(now)
    client = LiteLLMBudgetClient(transport)
    store = BudgetFallbackStore(db)
    controller = BudgetFallbackController(store, client)
    original = client.get_key("hash-integration")
    store.enable_policy(
        {
            "key_id": original.key_id,
            "key_alias": original.key_alias,
            "models": list(original.models),
            "aliases": original.aliases,
            "max_budget": original.max_budget,
            "budget_duration": original.budget_duration,
            "budget_reset_at": original.budget_reset_at,
            "blocked": original.blocked,
            "spend": original.spend,
            "config_fingerprint": managed_fingerprint(original),
        },
        actor="integration-test",
    )

    switched = controller.run_policy(original.key_id, now)

    assert switched.to_state == "FALLBACK_5_3"
    live_fallback = client.get_key(original.key_id)
    assert "gpt-5.5" in live_fallback.models
    assert live_fallback.aliases["gpt-5.5"] == FALLBACK_MODEL_GROUP
    assert "wangsu-gpt-5.5" not in live_fallback.models
    assert live_fallback.max_budget is None

    transport.simulate_fallback_usage(50_000)
    assert transport.audit_tokens == 50_000
    assert client.get_key(original.key_id).spend == 98

    store.update_policy(
        original.key_id,
        original_budget_reset_at=(now - timedelta(seconds=1)).isoformat(),
    )
    transport.now = now + timedelta(seconds=1)
    restored = controller.run_policy(original.key_id, transport.now)

    assert restored.to_state == "NORMAL"
    live_restored = client.get_key(original.key_id)
    assert live_restored.models == original.models
    assert live_restored.aliases == original.aliases
    assert live_restored.max_budget == 100
    assert live_restored.budget_duration == "1d"
    assert live_restored.spend == 0
    assert datetime.fromisoformat(live_restored.budget_reset_at) > transport.now
    assert [event["event_type"] for event in store.list_events(original.key_id)] == [
        "automatic_restore",
        "automatic_switch",
        "policy_enabled",
    ]
