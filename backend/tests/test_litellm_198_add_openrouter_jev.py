"""Unit tests for scripts/litellm-198-add-openrouter-jev.py (payload builder)."""
import importlib.util
import pathlib

SCRIPT = (pathlib.Path(__file__).resolve().parents[2]
          / "scripts" / "litellm-198-add-openrouter-jev.py")


def _load():
    spec = importlib.util.spec_from_file_location("add_or_jev", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_public_name_is_the_bare_name_user_asked_for():
    assert mod.PUBLIC_NAME == "jev"


def test_upstream_is_the_only_slug_that_exists_on_openrouter():
    # typesafe/jev-latest and typesafe/jev both 404 (2026-09-28).
    assert mod.UPSTREAM == "typesafe/jev-router"


def test_model_uses_openrouter_provider_prefix():
    # `openai/...` would skip the transformation that injects usage.include
    # and reads back usage.cost, so dynamic pricing would be lost.
    p = mod.build_model_new_payload()
    assert p["litellm_params"]["model"] == "openrouter/typesafe/jev-router"


def test_api_key_is_an_env_reference_never_a_literal():
    p = mod.build_model_new_payload()
    key = p["litellm_params"]["api_key"]
    assert key == "os.environ/OPENROUTER_API_KEY"
    assert key.startswith("os.environ/"), "a literal key here is a committed credential"


def test_no_cost_fields_because_pricing_is_dynamic():
    # OpenRouter prices this model as "-1"/"-1"; response_cost_calculator returns
    # the provider-reported usage.cost before completion_cost ever runs, so a
    # hardcoded per-token price is never used -- it only misreports /model/info.
    p = mod.build_model_new_payload()
    flat = list(p["litellm_params"]) + list(p["model_info"])
    assert [k for k in flat if "cost" in k] == []


def test_mode_is_explicitly_chat():
    # A model whose mode cannot be determined gets skipped by the probes and
    # reported as "未测", which is shaped exactly like a broken model.
    assert mod.build_model_new_payload()["model_info"]["mode"] == "chat"


def test_model_info_id_follows_the_public_name_not_the_upstream():
    # Renaming the group later means old+new rows coexist; ids must not collide.
    assert mod.build_model_new_payload()["model_info"]["id"] == "openrouter/jev"


def test_no_supports_claims_for_a_router():
    # Capabilities depend on whichever upstream jev picks per request
    # (8/8 landed on stealth/space-bunny-alpha, but that can change).
    info = mod.build_model_new_payload()["model_info"]
    assert [k for k in info if k.startswith("supports_")] == []


def test_max_input_tokens_matches_openrouter_context_length():
    assert mod.build_model_new_payload()["model_info"]["max_input_tokens"] == 1_000_000


def test_no_max_output_tokens_because_upstream_reports_null():
    # top_provider.max_completion_tokens is null; guessing a number would be a
    # gate nobody measured.
    assert "max_output_tokens" not in mod.build_model_new_payload()["model_info"]


def test_payload_has_exactly_the_three_top_level_keys():
    assert set(mod.build_model_new_payload()) == {
        "model_name", "litellm_params", "model_info"}


# --- the verify ruler itself ------------------------------------------------
# 2026-09-28: the first version asked "is there a key containing 'cost'", which
# measures /model/info's response *schema* (it returns all ~75 cost fields for
# every model, nearly all None) instead of what we wrote. It reported BAD on a
# correctly-registered model. Positive control below pins both directions.

def test_hardcoded_cost_ignores_the_null_schema_fields():
    lp = {"model": "openrouter/typesafe/jev-router"}
    mi = {"input_cost_per_token": None, "output_cost_per_token": None,
          "cache_read_input_token_cost": None, "ocr_cost_per_page": None}
    assert mod.hardcoded_cost(lp, mi) == {}


def test_hardcoded_cost_ignores_zero_which_is_the_shape_we_want():
    # LiteLLM fills these in as 0 by itself; 0 means "not billed per token,
    # use the provider-reported usage.cost".
    mi = {"input_cost_per_token": 0, "output_cost_per_token": 0}
    assert mod.hardcoded_cost({}, mi) == {}


def test_hardcoded_cost_catches_a_real_price_positive_control():
    # Without this the ruler could pass by always returning {}.
    mi = {"input_cost_per_token": 1.25e-06, "output_cost_per_token": None}
    assert mod.hardcoded_cost({}, mi) == {"input_cost_per_token": 1.25e-06}


def test_hardcoded_cost_looks_at_litellm_params_too():
    # cost fields get double-written in the usual onboarding shape, so a stray
    # price can hide in litellm_params while model_info looks clean.
    assert mod.hardcoded_cost({"output_cost_per_token": 3e-05}, {}) == {
        "output_cost_per_token": 3e-05}
