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


# --------------------------------------------------------------------------- #
# named-groups 模式（2026-09-25 新增）—— 「全部 gpt 系列末端统一成 X」用的。
# anchor 模式在这个诉求下不合用：53 条在范围内的链末端有 **7 种**不同的腿，
# 按 anchor 扫会连带改到共享同一末端的非 gpt 链（光 deepseek-v4-pro-responses
# 结尾的就有 36 条）。
# --------------------------------------------------------------------------- #
TARGET = "openrouter-deepseek-v4.1-flash"


def _plan_named(fallbacks, groups):
    return mod.plan_fallbacks(fallbacks, ANCHOR, TARGET, set(groups))


def test_named_groups_ignores_anchor_entirely():
    # 链末端不是 anchor，anchor 模式会跳过；点名模式必须照改。
    fb = [{"gpt-6-astra": ["chatgpt-gpt-5.6-sol", "sa-grok-4.6"]}]
    new, changes = _plan_named(fb, ["gpt-6-astra"])
    assert new == [{"gpt-6-astra": ["chatgpt-gpt-5.6-sol", "sa-grok-4.6", TARGET]}]
    assert len(changes) == 1


def test_named_groups_does_not_touch_unnamed_even_if_anchor_tail():
    # 这条是本模式存在的理由：同样以 anchor 结尾的非 gpt 链必须纹丝不动。
    fb = [
        {"gpt-5.4": ["chatgpt-gpt-5.6-luna", ANCHOR]},
        {"some-non-gpt-group": ["whatever", ANCHOR]},
    ]
    new, changes = _plan_named(fb, ["gpt-5.4"])
    assert new[1] == {"some-non-gpt-group": ["whatever", ANCHOR]}
    assert [g for g, _, _ in changes] == ["gpt-5.4"]


def test_named_groups_moves_midchain_target_to_tail_instead_of_duplicating():
    # gpt-5.6-sol 的真实形状：target 已经在链中段。诉求是「最后一个用它」，
    # 所以要**搬到末尾**，不是再追加一份（追加会让同一条腿在链里出现两次）。
    fb = [{"gpt-5.6-sol": ["sa-grok-4.6", TARGET, ANCHOR, APPEND]}]
    new, changes = _plan_named(fb, ["gpt-5.6-sol"])
    assert new == [{"gpt-5.6-sol": ["sa-grok-4.6", ANCHOR, APPEND, TARGET]}]
    assert new[0]["gpt-5.6-sol"].count(TARGET) == 1
    assert len(changes) == 1


def test_named_groups_idempotent_when_target_already_last():
    fb = [{"gpt-5.4": ["chatgpt-gpt-5.6-luna", TARGET]}]
    new, changes = _plan_named(fb, ["gpt-5.4"])
    assert new == fb
    assert changes == []


def test_named_groups_empty_chain_is_not_given_a_chain():
    # 空链 = 这个组实际上没有兜底。给它凭空造一条是**新的路由决定**，不是编辑。
    fb = [{"gpt-weird": []}]
    new, changes = _plan_named(fb, ["gpt-weird"])
    assert new == fb
    assert changes == []


def test_plan_misses_reports_groups_with_no_fallback_row():
    fb = [{"gpt-5.4": ["x"]}]
    assert mod.plan_misses(fb, {"gpt-5.4", "gpt-nonexistent", "gpt-also-missing"}) == [
        "gpt-also-missing",
        "gpt-nonexistent",
    ]


def test_named_groups_does_not_mutate_input():
    fb = [{"gpt-5.4": ["chatgpt-gpt-5.6-luna", ANCHOR]}]
    snapshot = [dict((k, list(v)) for k, v in e.items()) for e in fb]
    _plan_named(fb, ["gpt-5.4"])
    assert fb == snapshot


def test_production_lane_is_selected_by_route_label_not_deployment_name():
    # 🔴 标签在 Pod 上；这条钉住 selector 不许退回 app=litellm-proxy。
    assert mod.PROXY_SELECTOR == "carher.net/litellm-production-route=enabled"
