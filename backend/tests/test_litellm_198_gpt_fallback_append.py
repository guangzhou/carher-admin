from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "litellm-198-gpt-fallback-append.py"
)
SPEC = importlib.util.spec_from_file_location("litellm_198_gpt_fallback_append", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

ANCHOR = "deepseek-v4-flash-responses"
APPEND = "deepseek-v4-pro-responses"


def _plan(fallbacks):
    return mod.plan_fallbacks(fallbacks, ANCHOR, APPEND)


def test_appends_only_when_chain_ends_with_anchor():
    fb = [{"gpt-5.6-sol": ["gpt-5.6-codex", ANCHOR]}]
    new, changes = _plan(fb)
    assert new == [{"gpt-5.6-sol": ["gpt-5.6-codex", ANCHOR, APPEND]}]
    assert changes == [
        ("gpt-5.6-sol", ["gpt-5.6-codex", ANCHOR], ["gpt-5.6-codex", ANCHOR, APPEND])
    ]


def test_anchor_present_but_not_last_is_untouched():
    # anchor in the middle of the chain: not the tail -> no append.
    fb = [{"weird": [ANCHOR, "gpt-5.6-codex"]}]
    new, changes = _plan(fb)
    assert new == fb
    assert changes == []


def test_chain_without_anchor_tail_is_untouched():
    fb = [
        {"chatgpt-vip-5.6": ["wangsu7-gpt-5.6"]},
        {"wangsu7-gpt-5.5": ["chatgpt-pool-gpt-5.5"]},
    ]
    new, changes = _plan(fb)
    assert new == fb
    assert changes == []


def test_idempotent_when_already_appended():
    # Running the script twice must be a no-op: append is now the tail, anchor is not.
    fb = [{"gpt-5.6-sol": ["gpt-5.6-codex", ANCHOR, APPEND]}]
    new, changes = _plan(fb)
    assert new == fb
    assert changes == []


def test_no_double_append_if_target_present_elsewhere():
    # If APPEND already appears anywhere in the chain, do not add a duplicate,
    # even if the chain still ends with the anchor.
    fb = [{"odd": [APPEND, "gpt-5.6-codex", ANCHOR]}]
    new, changes = _plan(fb)
    assert new == fb
    assert changes == []


def test_empty_chain_does_not_crash():
    fb = [{"empty": []}]
    new, changes = _plan(fb)
    assert new == [{"empty": []}]
    assert changes == []


def test_mixed_set_touches_only_targets_and_preserves_order():
    fb = [
        {"gpt-5.6-sol": ["gpt-5.6-codex", ANCHOR]},          # -> append
        {"chatgpt-vip-5.6": ["wangsu7-gpt-5.6"]},            # untouched (no anchor)
        {"claude-gpt-5.6": ["gpt-5.6", ANCHOR]},             # -> append
        {"already": ["gpt-5.6-codex", ANCHOR, APPEND]},      # idempotent
        {"weird": [ANCHOR, "gpt-5.6-codex"]},                # anchor not last
    ]
    new, changes = _plan(fb)
    assert new == [
        {"gpt-5.6-sol": ["gpt-5.6-codex", ANCHOR, APPEND]},
        {"chatgpt-vip-5.6": ["wangsu7-gpt-5.6"]},
        {"claude-gpt-5.6": ["gpt-5.6", ANCHOR, APPEND]},
        {"already": ["gpt-5.6-codex", ANCHOR, APPEND]},
        {"weird": [ANCHOR, "gpt-5.6-codex"]},
    ]
    assert [c[0] for c in changes] == ["gpt-5.6-sol", "claude-gpt-5.6"]


def test_does_not_mutate_input():
    original = [{"gpt-5.6-sol": ["gpt-5.6-codex", ANCHOR]}]
    snapshot = [dict((k, list(v)) for k, v in e.items()) for e in original]
    _plan(original)
    assert original == snapshot


def test_multi_key_entry_rejected():
    import pytest

    with pytest.raises(ValueError):
        _plan([{"a": [ANCHOR], "b": [ANCHOR]}])
