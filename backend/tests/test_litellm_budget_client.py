from backend.litellm_budget_client import (
    FALLBACK_MODEL_GROUP,
    LiteLLMBudgetClient,
    LiteLLMBudgetError,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request_json(self, method, path, payload=None, timeout=15):
        self.calls.append((method, path, payload, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_list_budgeted_keys_filters_periodic_positive_budgets():
    transport = FakeTransport(
        [
            [
                {
                    "token": "hash-1",
                    "key_alias": "cursor-alice",
                    "models": ["gpt-5.5"],
                    "aliases": {},
                    "max_budget": 100,
                    "budget_duration": "1d",
                    "budget_reset_at": "2026-07-14T00:00:00Z",
                    "spend": 20,
                    "blocked": False,
                },
                {"token": "hash-2", "key_alias": "unlimited", "max_budget": None},
                {
                    "token": "hash-3",
                    "key_alias": "lifetime",
                    "max_budget": 50,
                    "budget_duration": None,
                },
            ]
        ]
    )

    rows = LiteLLMBudgetClient(transport).list_budgeted_keys()

    assert [row.key_id for row in rows] == ["hash-1"]
    assert rows[0].budget_reset_at == "2026-07-14T00:00:00+00:00"


def test_get_key_accepts_info_wrapper():
    transport = FakeTransport(
        [
            {
                "info": {
                    "token": "hash-1",
                    "key_alias": "cursor-alice",
                    "models": ["gpt-5.5"],
                    "aliases": {},
                    "max_budget": 100,
                    "budget_duration": "1d",
                    "budget_reset_at": "2026-07-14T00:00:00+00:00",
                    "spend": 20,
                    "blocked": False,
                }
            }
        ]
    )

    row = LiteLLMBudgetClient(transport).get_key("hash-1")

    assert row.key_alias == "cursor-alice"
    assert "%2D" not in transport.calls[0][1]


def test_update_key_posts_all_managed_fields_and_rereads():
    updated = {
        "info": {
            "token": "hash-1",
            "key_alias": "cursor-alice",
            "models": ["gpt-5.5", FALLBACK_MODEL_GROUP],
            "aliases": {"gpt-5.5": FALLBACK_MODEL_GROUP},
            "max_budget": None,
            "budget_duration": None,
            "budget_reset_at": None,
            "spend": 98,
            "blocked": False,
        }
    }
    transport = FakeTransport([{"status": "ok"}, updated])

    row = LiteLLMBudgetClient(transport).update_key(
        "hash-1",
        models=["gpt-5.5", FALLBACK_MODEL_GROUP],
        aliases={"gpt-5.5": FALLBACK_MODEL_GROUP},
        max_budget=None,
        budget_duration=None,
    )

    assert transport.calls[0][1] == "/key/update"
    assert transport.calls[0][2] == {
        "key": "hash-1",
        "models": ["gpt-5.5", FALLBACK_MODEL_GROUP],
        "aliases": {"gpt-5.5": FALLBACK_MODEL_GROUP},
        "max_budget": None,
        "budget_duration": None,
    }
    assert row.max_budget is None


def test_update_key_includes_spend_and_blocked_when_requested():
    response = {
        "info": {
            "token": "hash-1",
            "key_alias": "cursor-alice",
            "models": ["gpt-5.5"],
            "aliases": {},
            "max_budget": 100,
            "budget_duration": "1d",
            "budget_reset_at": "2026-07-15T00:00:00Z",
            "spend": 0,
            "blocked": False,
        }
    }
    transport = FakeTransport([{}, response])

    LiteLLMBudgetClient(transport).update_key(
        "hash-1",
        models=["gpt-5.5"],
        aliases={},
        max_budget=100,
        budget_duration="1d",
        spend=0,
        blocked=False,
    )

    assert transport.calls[0][2]["spend"] == 0
    assert transport.calls[0][2]["blocked"] is False


def test_fallback_health_requires_every_cost_field_to_be_zero():
    transport = FakeTransport(
        [
            {
                "data": [
                    {
                        "model_name": FALLBACK_MODEL_GROUP,
                        "litellm_params": {
                            "input_cost_per_token": 0,
                            "output_cost_per_token": 0,
                            "cache_read_input_token_cost": 0,
                        },
                    },
                    {
                        "model_name": FALLBACK_MODEL_GROUP,
                        "litellm_params": {
                            "input_cost_per_token": 0,
                            "output_cost_per_token": 0,
                            "cache_read_input_token_cost": 0,
                        },
                    },
                ]
            }
        ]
    )

    health = LiteLLMBudgetClient(transport).check_fallback_model()

    assert health.available is True
    assert health.zero_cost is True
    assert health.deployment_count == 2


def test_fallback_health_rejects_nonzero_or_missing_costs():
    transport = FakeTransport(
        [
            {
                "data": [
                    {
                        "model_name": FALLBACK_MODEL_GROUP,
                        "litellm_params": {
                            "input_cost_per_token": 0,
                            "output_cost_per_token": 0.01,
                        },
                    }
                ]
            }
        ]
    )

    health = LiteLLMBudgetClient(transport).check_fallback_model()

    assert health.available is True
    assert health.zero_cost is False


def test_transport_errors_are_sanitized():
    transport = FakeTransport([RuntimeError("Authorization: Bearer sk-secret")])

    try:
        LiteLLMBudgetClient(transport).list_budgeted_keys()
    except LiteLLMBudgetError as exc:
        assert "sk-secret" not in str(exc)
    else:
        raise AssertionError("expected LiteLLMBudgetError")
