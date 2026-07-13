import asyncio
from datetime import UTC, datetime

from backend import budget_fallback_worker as worker
from backend.budget_fallback import TransitionResult


class FakeStore:
    def __init__(self, policies, lease=True):
        self.policies = policies
        self.lease = lease
        self.calls = []

    def list_policies(self):
        return list(self.policies)

    def acquire_lease(self, key_id, owner, now, ttl_seconds=30):
        self.calls.append(("lease", key_id))
        return self.lease

    def release_lease(self, key_id, owner):
        self.calls.append(("release", key_id))


class FakeController:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def run_policy(self, key_id, now):
        self.calls.append(key_id)
        if self.error:
            raise self.error
        return self.result


def result(event_type="automatic_switch", changed=True):
    return TransitionResult(
        key_id="hash-1",
        from_state="NORMAL",
        to_state="FALLBACK_5_3",
        changed=changed,
        event_type=event_type,
    )


def test_cycle_acquires_lease_before_running_policy():
    store = FakeStore([{"key_id": "hash-1", "enabled": True, "automation_paused": False}])
    controller = FakeController(result())

    results = asyncio.run(
        worker.run_budget_fallback_cycle(
            datetime(2026, 7, 13, tzinfo=UTC), store=store, controller=controller
        )
    )

    assert store.calls == [("lease", "hash-1"), ("release", "hash-1")]
    assert controller.calls == ["hash-1"]
    assert results[0].event_type == "automatic_switch"


def test_cycle_skips_disabled_paused_and_not_due_policies():
    policies = [
        {"key_id": "disabled", "enabled": False, "automation_paused": False},
        {"key_id": "paused", "enabled": True, "automation_paused": True},
        {
            "key_id": "recent",
            "enabled": True,
            "automation_paused": False,
            "state": "NORMAL",
            "last_observed_spend": 10,
            "original_max_budget": 100,
            "last_observed_at": "2026-07-13T00:00:00+00:00",
        },
    ]
    store = FakeStore(policies)
    controller = FakeController(result())

    asyncio.run(
        worker.run_budget_fallback_cycle(
            datetime(2026, 7, 13, 0, 0, 10, tzinfo=UTC),
            store=store,
            controller=controller,
        )
    )

    assert controller.calls == []


def test_notification_only_fires_for_edge_events(monkeypatch):
    sent = []
    monkeypatch.setattr(worker, "post_feishu_text", sent.append)

    worker.notify_transition(result(event_type="near_limit", changed=False))
    worker.notify_transition(result(event_type="automatic_switch", changed=True))

    assert len(sent) == 1
    assert "hash-1" in sent[0]


def test_cycle_returns_sanitized_failure_and_releases_lease():
    store = FakeStore([{"key_id": "hash-1", "enabled": True, "automation_paused": False}])
    controller = FakeController(error=RuntimeError("Bearer sk-secret"))

    results = asyncio.run(
        worker.run_budget_fallback_cycle(
            datetime(2026, 7, 13, tzinfo=UTC), store=store, controller=controller
        )
    )

    assert results[0].event_type == "worker_failed"
    assert "sk-secret" not in results[0].error
    assert store.calls[-1] == ("release", "hash-1")
