from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-claudecode-deepseek-enable.py"
SPEC = importlib.util.spec_from_file_location("litellm_claudecode_deepseek_enable", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def key(**overrides):
    """A typical claude-code key pre-2026-08-15: pro alias present, no flash, no bare names."""
    value = {
        "key_alias": "claude-code-test-aaaa",
        "token": "b" * 64,
        "aliases": {
            "claude-glm-5.2": "openrouter-glm-5.2",
            "claude-deepseek-v4-pro": "deepseek-v4-pro",
        },
        "models": ["chatgpt-gpt-5.5", "claude-glm-5.2", "claude-deepseek-v4-pro"],
    }
    value.update(overrides)
    return value


def test_plan_adds_flash_alias_and_all_four_model_names():
    change = mod.plan_key(key())
    assert change is not None
    assert change["aliases"]["claude-deepseek-v4-flash"] == "deepseek-v4-flash"
    assert change["aliases"]["claude-deepseek-v4-pro"] == "deepseek-v4-pro"
    assert change["aliases"]["claude-glm-5.2"] == "openrouter-glm-5.2"
    for name in ("deepseek-v4-pro", "deepseek-v4-flash",
                 "claude-deepseek-v4-pro", "claude-deepseek-v4-flash"):
        assert name in change["models"], name


def test_plan_adds_missing_pro_alias():
    change = mod.plan_key(key(aliases={"claude-glm-5.2": "openrouter-glm-5.2"}))
    assert change is not None
    assert change["aliases"]["claude-deepseek-v4-pro"] == "deepseek-v4-pro"


def test_plan_converges_legacy_flash_pin_to_pro():
    change = mod.plan_key(key(aliases={"claude-deepseek-v4-pro": "deepseek-v4-flash"}))
    assert change is not None
    assert change["aliases"]["claude-deepseek-v4-pro"] == "deepseek-v4-pro"


def test_plan_leaves_hand_customized_alias_value():
    change = mod.plan_key(key(aliases={"claude-deepseek-v4-pro": "my-own-group"}))
    assert change is not None
    assert change["aliases"]["claude-deepseek-v4-pro"] == "my-own-group"
    assert change["aliases"]["claude-deepseek-v4-flash"] == "deepseek-v4-flash"


def test_plan_is_idempotent_after_enable():
    done = key(
        aliases={
            "claude-glm-5.2": "openrouter-glm-5.2",
            "claude-deepseek-v4-pro": "deepseek-v4-pro",
            "claude-deepseek-v4-flash": "deepseek-v4-flash",
        },
        models=sorted([
            "chatgpt-gpt-5.5", "claude-glm-5.2", "claude-deepseek-v4-pro",
            "claude-deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash",
        ]),
    )
    assert mod.plan_key(done) is None


def test_plan_skips_unrestricted_key():
    assert mod.plan_key(key(models=[])) is None


def test_verify_flags_missing_alias_and_guards_unrelated_fields():
    before = [key()]
    bad = key(aliases={
        "claude-glm-5.2": "openrouter-glm-5.2",
        "claude-deepseek-v4-pro": "deepseek-v4-pro",
        # flash alias missing
    })
    problems = mod.verify_keys(before, [bad])
    assert ("claude-code-test-aaaa",
            "claude-deepseek-v4-flash not mapped to deepseek-v4-flash") in problems

    good = key(
        aliases={
            "claude-glm-5.2": "openrouter-glm-5.2",
            "claude-deepseek-v4-pro": "deepseek-v4-pro",
            "claude-deepseek-v4-flash": "deepseek-v4-flash",
        },
        models=sorted(key()["models"] + [
            "claude-deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash"]),
    )
    assert mod.verify_keys(before, [good]) == []

    tampered = dict(good)
    tampered["aliases"] = dict(good["aliases"], **{"claude-glm-5.2": "other"})
    assert ("claude-code-test-aaaa", "unrelated alias changed") in mod.verify_keys(before, [tampered])
