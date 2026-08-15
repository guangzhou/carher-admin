from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-cursor-deepseek-pro-split.py"
SPEC = importlib.util.spec_from_file_location("litellm_cursor_deepseek_pro_split", SCRIPT)
assert SPEC and SPEC.loader
split = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(split)


def key(**overrides):
    """A key in the post-2026-08-12 state: both aliases pinned to flash."""
    value = {
        "key_alias": "cursor-test-aaaa",
        "token": "a" * 64,
        "aliases": {
            "glm-5.2": "zai-glm-5.2",
            "deepseek-v4-pro": "deepseek-v4-flash",
            "claude-deepseek-v4-pro": "deepseek-v4-flash",
        },
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2"],
    }
    value.update(overrides)
    return value


def test_plan_removes_bare_alias_and_points_prefixed_at_pro():
    change = split.plan_key(key())
    assert change is not None
    assert "deepseek-v4-pro" not in change["aliases"]
    assert change["aliases"]["claude-deepseek-v4-pro"] == "deepseek-v4-pro"
    assert change["aliases"]["glm-5.2"] == "zai-glm-5.2"
    assert "deepseek-v4-pro" in change["models"]


def test_plan_unions_pro_into_models_when_missing():
    change = split.plan_key(key(models=["deepseek-v4-flash", "glm-5.2"]))
    assert change is not None
    assert sorted(change["models"]) == ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2"]


def test_plan_is_idempotent_after_split():
    value = key(
        aliases={"glm-5.2": "zai-glm-5.2", "claude-deepseek-v4-pro": "deepseek-v4-pro"},
        models=["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2"],
    )
    assert split.plan_key(value) is None


def test_plan_leaves_hand_customized_alias_values_alone():
    value = key(
        aliases={
            "deepseek-v4-pro": "some-custom-group",
            "claude-deepseek-v4-pro": "another-custom-group",
        },
        models=["deepseek-v4-flash", "deepseek-v4-pro"],
    )
    change = split.plan_key(value)
    # models already contain pro and aliases untouched -> no-op
    assert change is None


def test_plan_skips_unrestricted_key_without_narrowing_it():
    assert split.plan_key(key(models=[])) is None


def test_verify_flags_lingering_bare_alias_and_wrong_prefixed_target():
    before = [key()]
    after_bad = [key(aliases={
        "glm-5.2": "zai-glm-5.2",
        "deepseek-v4-pro": "deepseek-v4-flash",       # still present
        "claude-deepseek-v4-pro": "deepseek-v4-flash",  # not flipped
    })]
    problems = split.verify_keys(before, after_bad)
    reasons = {reason for _, reason in problems}
    assert "deepseek-v4-pro alias still present" in reasons
    assert "claude-deepseek-v4-pro not mapped to deepseek-v4-pro" in reasons


def test_verify_accepts_correct_split_and_guards_unrelated_fields():
    before = [key()]
    good = key(aliases={"glm-5.2": "zai-glm-5.2", "claude-deepseek-v4-pro": "deepseek-v4-pro"})
    assert split.verify_keys(before, [good]) == []

    tampered = key(aliases={"glm-5.2": "other", "claude-deepseek-v4-pro": "deepseek-v4-pro"})
    assert ("cursor-test-aaaa", "unrelated alias changed") in split.verify_keys(before, [tampered])

    shrunk = key(
        aliases={"glm-5.2": "zai-glm-5.2", "claude-deepseek-v4-pro": "deepseek-v4-pro"},
        models=["deepseek-v4-pro"],
    )
    assert ("cursor-test-aaaa", "unexpected model change") in split.verify_keys(before, [shrunk])
