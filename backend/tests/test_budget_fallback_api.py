from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.budget_fallback import TransitionResult, managed_fingerprint
from backend.budget_fallback_api import (
    get_budget_client,
    get_budget_controller,
    get_budget_store,
    router,
)
from backend.litellm_budget_client import KeySnapshot, ModelHealth


NOW = datetime.now(UTC)


def snapshot(
    key_id="hash-1",
    alias="cursor-alice",
    budget=100,
    duration="1d",
    reset=None,
    blocked=False,
):
    return KeySnapshot(
        key_id=key_id,
        key_alias=alias,
        models=("gpt-5.5", "gpt-5.4"),
        aliases={"gpt-5.5": "chatgpt-pool-gpt-5.5"},
        max_budget=budget,
        budget_duration=duration,
        budget_reset_at=reset or (NOW + timedelta(days=1)).isoformat(),
        spend=20,
        blocked=blocked,
    )


class FakeClient:
    def __init__(self, rows=None, health=None):
        self.rows = rows or [snapshot()]
        self.health = health or ModelHealth(True, True, 2)

    def list_budgeted_keys(self):
        return list(self.rows)

    def get_key(self, key_id):
        return next(row for row in self.rows if row.key_id == key_id)

    def check_fallback_model(self):
        return self.health


class FakeStore:
    def __init__(self):
        self.policies = {}
        self.events = {}

    def list_policies(self):
        return list(self.policies.values())

    def get_policy(self, key_id):
        return self.policies.get(key_id)

    def enable_policy(self, data, actor):
        row = {
            "key_id": data["key_id"],
            "key_alias": data["key_alias"],
            "enabled": True,
            "state": "NORMAL",
            "threshold_percent": 98,
            "original_models": data["models"],
            "original_aliases": data["aliases"],
            "original_max_budget": data["max_budget"],
            "original_budget_duration": data["budget_duration"],
            "original_budget_reset_at": data["budget_reset_at"],
            "original_blocked": data["blocked"],
            "original_config_fingerprint": data["config_fingerprint"],
            "fallback_config_fingerprint": "",
            "automation_paused": False,
            "last_observed_spend": data.get("spend", 0),
            "last_error": "",
        }
        self.policies[data["key_id"]] = row
        return row

    def update_policy(self, key_id, **changes):
        self.policies[key_id].update(changes)
        return self.policies[key_id]

    def disable_policy(self, key_id, actor):
        return self.update_policy(key_id, enabled=False)

    def list_events(self, key_id, limit=100):
        return self.events.get(key_id, [])[:limit]


class FakeController:
    def __init__(self, store):
        self.store = store

    def force_fallback(self, key_id, actor, now):
        self.store.update_policy(key_id, state="FALLBACK_5_3")
        return TransitionResult(key_id, "NORMAL", "FALLBACK_5_3", True, "manual_switch")

    def force_restore(self, key_id, actor, now):
        self.store.update_policy(key_id, state="NORMAL")
        return TransitionResult(key_id, "FALLBACK_5_3", "NORMAL", True, "manual_restore")

    def recapture(self, key_id, actor):
        return self.store.get_policy(key_id)


def make_client(client=None, store=None):
    client = client or FakeClient()
    store = store or FakeStore()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_budget_client] = lambda: client
    app.dependency_overrides[get_budget_store] = lambda: store
    app.dependency_overrides[get_budget_controller] = lambda: FakeController(store)
    return TestClient(app), client, store


def enable(http, key_id="hash-1"):
    return http.post(
        f"/api/litellm/budget-fallback/keys/{key_id}/enable",
        json={"key_id": key_id},
        headers={"x-test-actor": "admin"},
    )


def test_list_never_returns_plaintext_token():
    http, _, _ = make_client()

    response = http.get("/api/litellm/budget-fallback/keys")

    assert response.status_code == 200
    body = response.json()
    assert body["keys"][0]["key_id"] == "hash-1"
    assert "api_key" not in str(body).lower()
    assert "sk-" not in str(body)


def test_enable_captures_periodic_key_baseline():
    http, _, store = make_client()

    response = enable(http)

    assert response.status_code == 200
    assert response.json()["policy"]["state"] == "NORMAL"
    assert store.policies["hash-1"]["original_config_fingerprint"] == managed_fingerprint(snapshot())


def test_enable_rejects_non_periodic_key_with_422():
    http, _, _ = make_client(FakeClient([snapshot(duration=None)]))

    response = enable(http)

    assert response.status_code == 422


def test_enable_rejects_non_zero_cost_fallback_model_with_409():
    client = FakeClient(health=ModelHealth(True, False, 2, "not zero"))
    http, _, _ = make_client(client)

    response = enable(http)

    assert response.status_code == 409


def test_manual_fallback_returns_resulting_state():
    http, _, _ = make_client()
    assert enable(http).status_code == 200

    response = http.post(
        "/api/litellm/budget-fallback/keys/hash-1/fallback", json={"reason": "test"}
    )

    assert response.status_code == 200
    assert response.json()["result"]["to_state"] == "FALLBACK_5_3"


def test_disable_requires_explicit_restore_boolean():
    http, _, _ = make_client()
    assert enable(http).status_code == 200

    response = http.post(
        "/api/litellm/budget-fallback/keys/hash-1/disable", json={}
    )

    assert response.status_code == 422


def test_pause_and_resume_update_policy():
    http, _, store = make_client()
    assert enable(http).status_code == 200

    assert http.post("/api/litellm/budget-fallback/keys/hash-1/pause").status_code == 200
    assert store.policies["hash-1"]["automation_paused"] is True
    assert http.post("/api/litellm/budget-fallback/keys/hash-1/resume").status_code == 200
    assert store.policies["hash-1"]["automation_paused"] is False


def test_event_detail_is_returned_redacted_by_store_contract():
    http, _, store = make_client()
    store.events["hash-1"] = [
        {"id": 1, "detail": {"authorization": "[REDACTED]"}, "event_type": "test"}
    ]

    response = http.get("/api/litellm/budget-fallback/keys/hash-1/events")

    assert response.status_code == 200
    assert "secret" not in str(response.json()).lower()
