import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "litellm-budget-fallback-sync.py"
SPEC = importlib.util.spec_from_file_location("budget_fallback_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


def source_row(account=18, api_base=None):
    return {
        "model_name": sync.SOURCE_GROUP,
        "litellm_params": {
            "model": "openai/gpt-5-3",
            "api_base": api_base
            or f"http://zero-{account}.litellm-product.svc.cluster.local:8200/v1",
            "api_key": "os.environ/ZEROKEY_POOL_KEY",
        },
        "model_info": {
            "id": f"zk-{account}-gpt-5.3-codex",
            "mode": "responses",
        },
    }


def target_row(account=18, *, output_cost=0):
    row = sync.target_payload(source_row(account))
    row["litellm_params"]["output_cost_per_token"] = output_cost
    return row


def test_target_payload_is_isolated_zero_cost_and_preserves_upstream():
    payload = sync.target_payload(source_row())

    assert payload["model_name"] == sync.TARGET_GROUP
    assert payload["model_info"] == {
        "id": "budget-fallback/zk-18-gpt-5.3-codex",
        "mode": "responses",
    }
    assert payload["litellm_params"] == {
        "model": "openai/gpt-5-3",
        "api_base": "http://zero-18.litellm-product.svc.cluster.local:8200/v1",
        "api_key": "os.environ/ZEROKEY_POOL_KEY",
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
        "cache_read_input_token_cost": 0,
    }


def test_plan_replaces_drifted_members_and_removes_stale_members():
    source = [source_row(18), source_row(19)]
    target = [target_row(18, output_cost=1), target_row(27)]

    plan = sync.build_plan(source, target)

    assert [row["model_info"]["id"] for row in plan.create] == [
        "budget-fallback/zk-18-gpt-5.3-codex",
        "budget-fallback/zk-19-gpt-5.3-codex",
    ]
    assert plan.delete == [
        "budget-fallback/zk-18-gpt-5.3-codex",
        "budget-fallback/zk-27-gpt-5.3-codex",
    ]


def test_plan_is_empty_when_target_matches_source():
    source = [source_row(18), source_row(19)]
    target = [target_row(18), target_row(19)]

    plan = sync.build_plan(source, target)

    assert plan.create == []
    assert plan.delete == []


def test_source_filter_rejects_non_zerokey_or_duplicate_ids():
    rows = [source_row(18), source_row(18)]

    try:
        sync.source_members(rows)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate source IDs must be rejected")


def test_target_filter_rejects_rows_not_owned_by_the_sync_script():
    row = target_row(18)
    row["model_info"]["id"] = "manual-operator-row"

    try:
        sync.target_members([row])
    except ValueError as exc:
        assert "unexpected target" in str(exc)
    else:
        raise AssertionError("unowned target IDs must never be deleted")


def test_probe_payload_uses_public_chat_completions_contract():
    assert sync.probe_payload() == {
        "model": sync.TARGET_GROUP,
        "messages": [{"role": "user", "content": "Reply exactly pong"}],
        "max_tokens": 8,
    }


def test_probe_validation_requires_choices_and_owned_deployment():
    sync.validate_probe(
        {"choices": [{"message": {"content": "pong"}}]},
        {"x-litellm-model-id": "budget-fallback/zk-18-gpt-5.3-codex"},
    )

    for response, headers, expected in (
        ({"choices": []}, {}, "choices"),
        (
            {"choices": [{"message": {"content": "pong"}}]},
            {"x-litellm-model-id": "local/paid-fallback"},
            "unexpected deployment",
        ),
    ):
        try:
            sync.validate_probe(response, headers)
        except RuntimeError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("invalid probe must be rejected")


def test_replacement_creates_temporary_member_before_deleting_live_member():
    calls = []

    class Api:
        def request(self, method, path, payload=None, timeout=45):
            calls.append((path, payload))
            if path == "/model/new":
                return {"model_id": payload["model_info"]["id"]}
            return {}

    desired = sync.target_payload(source_row(18))
    plan = sync.SyncPlan(
        create=[desired],
        delete=[desired["model_info"]["id"]],
    )

    sync.apply_sync(Api(), plan)

    assert calls[0][0] == "/model/new"
    assert calls[0][1]["model_info"]["id"].endswith("/sync-staging")
    assert calls[1] == (
        "/model/delete",
        {"id": "budget-fallback/zk-18-gpt-5.3-codex"},
    )
    assert calls[2][1]["model_info"]["id"] == (
        "budget-fallback/zk-18-gpt-5.3-codex"
    )
    assert calls[3][0] == "/model/delete"
