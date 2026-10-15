from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-198-fallback-set-rows.py"
SPEC = importlib.util.spec_from_file_location("litellm_198_fallback_set_rows", SCRIPT)
assert SPEC and SPEC.loader
setrows = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setrows)


FB = [
    {"sa-grok-4.6": ["openrouter-deepseek-v4.1-flash", "deepseek-v4-flash-responses"]},
    {"gpt-5.5": ["chatgpt-gpt-5.6-luna"]},
]


def rs(fb=None):
    return {"fallbacks": copy.deepcopy(fb if fb is not None else FB),
            "model_group_alias": {"gpt-5.5": "chatgpt-gpt-5.5"}, "num_retries": 2}


def test_replace_keeps_position_and_adds_append():
    spec = {"sa-grok-4.6": ["deepseek-v4.1-flash-responses", "deepseek-v4.1-flash"],
            "sa-grok-4.20": ["deepseek-v4.1-flash-responses", "deepseek-v4.1-flash"]}
    new, replaced, added = setrows.build(FB, spec)
    assert replaced == ["sa-grok-4.6"] and added == ["sa-grok-4.20"]
    assert new[0] == {"sa-grok-4.6": spec["sa-grok-4.6"]}
    assert new[1] == FB[1]
    assert new[2] == {"sa-grok-4.20": spec["sa-grok-4.20"]}


def test_build_is_idempotent():
    spec = {"gpt-5.5": ["chatgpt-gpt-5.6-luna"]}
    new, replaced, added = setrows.build(FB, spec)
    assert new == FB and replaced == [] and added == []


def test_duplicate_rows_are_refused():
    with pytest.raises(SystemExit):
        setrows.build(FB + [{"gpt-5.5": ["x"]}], {"gpt-5.5": ["y"]})


def test_readback_passes_on_exact_write():
    spec = {"gpt-image-2": ["sa-grok-imagine-image-2.0"]}
    new, _, added = setrows.build(FB, spec)
    setrows.assert_readback(rs(), rs(new), spec, added)


def test_readback_catches_touched_unrelated_row():
    spec = {"gpt-image-2": ["sa-grok-imagine-image-2.0"]}
    new, _, added = setrows.build(FB, spec)
    new[1] = {"gpt-5.5": ["something-else"]}
    with pytest.raises(SystemExit):
        setrows.assert_readback(rs(), rs(new), spec, added)


def test_readback_catches_changed_top_level_key():
    spec = {"gpt-image-2": ["sa-grok-imagine-image-2.0"]}
    new, _, added = setrows.build(FB, spec)
    back = rs(new)
    back["model_group_alias"] = {}
    with pytest.raises(SystemExit):
        setrows.assert_readback(rs(), back, spec, added)


def test_readback_catches_missing_added_row():
    spec = {"gpt-image-2": ["sa-grok-imagine-image-2.0"]}
    _, _, added = setrows.build(FB, spec)
    with pytest.raises(SystemExit):
        setrows.assert_readback(rs(), rs(), spec, added)
