from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "litellm-198-fallback-insert-before.py"
)
SPEC = importlib.util.spec_from_file_location("litellm_198_fallback_insert_before", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

ANCHOR = "sa-grok-4.6"
INSERT = "sa-grok-4.20"


def _plan(fallbacks, groups):
    return mod.plan_insert_before(fallbacks, groups, ANCHOR, INSERT)


def test_inserts_immediately_before_anchor():
    fb = [{"gpt-5.5": ["chatgpt-gpt-5.6-luna", ANCHOR, "deepseek-v4-flash-responses"]}]
    new, changes, skipped = _plan(fb, ["gpt-5.5"])
    assert new == [
        {"gpt-5.5": ["chatgpt-gpt-5.6-luna", INSERT, ANCHOR, "deepseek-v4-flash-responses"]}
    ]
    assert [c[0] for c in changes] == ["gpt-5.5"]
    assert skipped == []


def test_entries_not_named_are_copied_through_unchanged():
    # 全局 fallbacks 有 91 行；点名 6 行时，其余 85 行必须逐字节原样。
    fb = [
        {"gpt-5.5": ["chatgpt-gpt-5.6-luna", ANCHOR]},
        {"sa-grok-4.6": ["openrouter-deepseek-v4.1-flash"]},
        {"claude-max-opus": ["anthropic.wangsu.claude-opus-4-8"]},
    ]
    new, changes, _ = _plan(fb, ["gpt-5.5"])
    assert new[1:] == fb[1:]
    assert len(changes) == 1


def test_idempotent_when_target_already_in_chain():
    fb = [{"gpt-5.5": ["chatgpt-gpt-5.6-luna", INSERT, ANCHOR]}]
    new, changes, skipped = _plan(fb, ["gpt-5.5"])
    assert new == fb
    assert changes == []
    assert skipped == ["gpt-5.5"]


def test_inserts_before_first_anchor_occurrence():
    fb = [{"g": ["a", ANCHOR, "b", ANCHOR]}]
    new, _, _ = _plan(fb, ["g"])
    assert new == [{"g": ["a", INSERT, ANCHOR, "b", ANCHOR]}]


def test_named_group_missing_anchor_is_refused():
    # 🔴 阳性对照：静默跳过会和"成功"完全同形，所以这里必须是硬红。
    fb = [{"g": ["deepseek-v4-flash-responses"]}]
    with pytest.raises(SystemExit) as e:
        _plan(fb, ["g"])
    assert "no anchor" in str(e.value)


def test_named_group_absent_from_fallbacks_is_refused():
    fb = [{"other": [ANCHOR]}]
    with pytest.raises(SystemExit) as e:
        _plan(fb, ["ghost"])
    assert "absent from fallbacks" in str(e.value)


def test_duplicate_group_in_fallbacks_is_refused():
    # 同名两行 = 后一行赢，改第一行是静默无效。
    fb = [{"g": [ANCHOR]}, {"g": [ANCHOR]}]
    with pytest.raises(ValueError):
        _plan(fb, ["g"])


def test_multi_key_entry_rejected():
    with pytest.raises(ValueError):
        _plan([{"a": [ANCHOR], "b": [ANCHOR]}], ["a"])


def test_duplicate_names_in_groups_arg_are_deduped():
    fb = [{"g": [ANCHOR]}]
    new, changes, _ = _plan(fb, ["g", "g"])
    assert new == [{"g": [INSERT, ANCHOR]}]
    assert len(changes) == 1


def test_does_not_mutate_input():
    original = [{"g": ["a", ANCHOR]}]
    snapshot = [dict((k, list(v)) for k, v in e.items()) for e in original]
    _plan(original, ["g"])
    assert original == snapshot


def test_entry_count_is_preserved():
    fb = [{"g1": [ANCHOR]}, {"g2": [ANCHOR]}, {"g3": ["x"]}]
    new, _, _ = _plan(fb, ["g1", "g2"])
    assert len(new) == len(fb)


def test_production_lane_is_selected_by_route_label_not_a_hard_coded_name():
    """🔴 门禁腿：2026-09-21 生产车道是 litellm-proxy-gray，不是 litellm-proxy。

    兄弟脚本 litellm-198-gpt-fallback-append.py 把 selector 写死成
    `app=litellm-proxy`（1 副本闲置车道）—— 拿它验会验到不在服务的 pod（假绿），
    `rollout restart` 会滚错 Deployment。本脚本必须按 Service
    litellm-proxy-nodeport（nodePort 30402）的 selector 选 pod。
    """
    sel = mod.PROD_POD_SELECTOR
    assert sel == "carher.net/litellm-production-route=enabled"
    assert not sel.startswith("app="), "别写死 Deployment 名，车道会搬家"


def test_verify_preserved_flags_a_dropped_top_level_key():
    """整块重写 param_value 会丢顶层键（model_group_alias 就这么被抹过）。"""
    before = {"fallbacks": [], "model_group_alias": {"a": "b"}, "num_retries": 3}
    after = {"fallbacks": [], "num_retries": 3}
    problems = mod._verify_preserved(before, after, [])
    assert any("keys changed" in p for p in problems)


def test_verify_preserved_flags_a_mutated_sibling_key():
    before = {"fallbacks": [], "model_group_alias": {"a": "b"}}
    after = {"fallbacks": [], "model_group_alias": {}}
    problems = mod._verify_preserved(before, after, [])
    assert any("non-fallbacks key mutated" in p for p in problems)


def test_verify_preserved_is_silent_on_a_clean_write():
    # 阴性对照：干净的写必须读出零 problem，否则上面两条断言没有分辨力。
    before = {"fallbacks": [{"g": [ANCHOR]}], "model_group_alias": {"a": "b"}}
    after = {"fallbacks": [{"g": [INSERT, ANCHOR]}], "model_group_alias": {"a": "b"}}
    assert mod._verify_preserved(before, after, [{"g": [INSERT, ANCHOR]}]) == []
