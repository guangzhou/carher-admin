from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "litellm-add-deepseek-v4-pro-official.py"
)
SPEC = importlib.util.spec_from_file_location("litellm_add_deepseek_v4_pro_official", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def test_chat_payload_shape_and_pricing():
    p = mod.build_model_new_payload(
        model_name="deepseek-v4-pro",
        litellm_model="custom_openai/deepseek-v4-pro",
        mode="chat",
        model_id="deepseek-official/deepseek-v4-pro",
        api_key="sk-secret",
    )
    assert p["model_name"] == "deepseek-v4-pro"
    assert p["litellm_params"]["model"] == "custom_openai/deepseek-v4-pro"
    assert p["litellm_params"]["api_base"] == "https://api.deepseek.com/v1"
    assert p["litellm_params"]["merge_reasoning_content_in_choices"] is False
    assert p["model_info"]["id"] == "deepseek-official/deepseek-v4-pro"
    assert p["model_info"]["mode"] == "chat"
    # cost double-written on both litellm_params and model_info
    for scope in ("litellm_params", "model_info"):
        assert p[scope]["input_cost_per_token"] == 0.000000435
        assert p[scope]["output_cost_per_token"] == 0.00000087
    assert p["model_info"]["max_input_tokens"] == 393216
    assert p["model_info"]["max_output_tokens"] == 65536


def test_responses_payload_uses_openai_prefix_and_responses_mode():
    p = mod.build_model_new_payload(
        model_name="deepseek-v4-pro-responses",
        litellm_model="openai/deepseek-v4-pro",
        mode="responses",
        model_id="deepseek-official/deepseek-v4-pro-responses",
        api_key="sk-secret",
    )
    assert p["litellm_params"]["model"] == "openai/deepseek-v4-pro"
    assert p["model_info"]["mode"] == "responses"
    assert p["model_info"]["id"] == "deepseek-official/deepseek-v4-pro-responses"


def test_redact_hides_api_key_and_does_not_mutate_input():
    p = mod.build_model_new_payload(
        model_name="deepseek-v4-pro",
        litellm_model="custom_openai/deepseek-v4-pro",
        mode="chat",
        model_id="deepseek-official/deepseek-v4-pro",
        api_key="sk-super-secret",
    )
    red = mod._redact(p)
    assert red["litellm_params"]["api_key"] == "***REDACTED***"
    # original is untouched
    assert p["litellm_params"]["api_key"] == "sk-super-secret"


def test_group_member_ids_filters_by_group_name():
    rows = [
        {"model_name": "deepseek-v4-pro", "model_info": {"id": "openrouter/deepseek-v4-pro"}},
        {"model_name": "deepseek-v4-pro", "model_info": {"id": "deepseek-official/deepseek-v4-pro"}},
        {"model_name": "deepseek-v4-flash", "model_info": {"id": "deepseek-official/deepseek-v4-flash"}},
    ]
    ids = mod._group_member_ids(rows, "deepseek-v4-pro")
    assert ids == ["openrouter/deepseek-v4-pro", "deepseek-official/deepseek-v4-pro"]
    assert [i for i in ids if i.startswith("openrouter/")] == ["openrouter/deepseek-v4-pro"]
