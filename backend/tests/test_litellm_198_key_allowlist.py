from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-198-key-allowlist.py"
SPEC = importlib.util.spec_from_file_location("litellm_198_key_allowlist", SCRIPT)
assert SPEC and SPEC.loader
allowlist = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(allowlist)


def key(**overrides):
    value = {
        "key_alias": "cursor-test-aaaa",
        "token": "a" * 64,
        "models": ["gpt-5.6-sol", "sa-grok-4.6", "zai-glm-5.2"],
        "aliases": {"gpt-6-astra": "chatgpt-gpt-6-astra"},
    }
    value.update(overrides)
    return value


def test_add_appends_without_dropping_existing_entries():
    change = allowlist.plan_key(key(), add_models=["sa-kimi-k3"])
    assert change == {"models": ["gpt-5.6-sol", "sa-grok-4.6", "zai-glm-5.2", "sa-kimi-k3"]}


def test_add_is_idempotent():
    value = key(models=["gpt-5.6-sol", "sa-kimi-k3"])
    assert allowlist.plan_key(value, add_models=["sa-kimi-k3"]) is None


def test_unrestricted_key_is_never_narrowed():
    # models == [] means "all models allowed"; writing a list would revoke access.
    assert allowlist.plan_key(key(models=[]), add_models=["sa-kimi-k3"]) is None
    assert allowlist.plan_key(key(models=[]), rm_models=["sa-kimi-k3"]) is None


def test_unrestricted_key_still_accepts_an_alias_only_patch():
    # An alias narrows nothing, so it is safe on an unrestricted key -- and the
    # patch must not carry a "models" field that would restrict it.
    change = allowlist.plan_key(key(models=[]), set_aliases={"kimi-k3": "sa-kimi-k3"})
    assert change == {"aliases": {"gpt-6-astra": "chatgpt-gpt-6-astra",
                                  "kimi-k3": "sa-kimi-k3"}}


def test_remove_drops_only_the_named_models():
    change = allowlist.plan_key(key(), rm_models=["sa-grok-4.6"])
    assert change == {"models": ["gpt-5.6-sol", "zai-glm-5.2"]}


def test_remove_is_idempotent():
    assert allowlist.plan_key(key(), rm_models=["not-present"]) is None


def test_add_multiple_models_at_once():
    change = allowlist.plan_key(key(models=["a"]), add_models=["b", "c"])
    assert change == {"models": ["a", "b", "c"]}


def test_rename_is_one_patch_with_remove_add_and_alias():
    # The whole point of a single patch: no window where the old name is gone
    # and the new one is not usable yet.
    change = allowlist.plan_key(
        key(models=["gpt-5.6-sol", "sa-kimi-k3", "sa-kimi-k3-responses"]),
        add_models=["kimi-k3"],
        rm_models=["sa-kimi-k3", "sa-kimi-k3-responses"],
        set_aliases={"kimi-k3": "sa-kimi-k3-responses"},
    )
    assert change == {
        "models": ["gpt-5.6-sol", "kimi-k3"],
        "aliases": {"gpt-6-astra": "chatgpt-gpt-6-astra",
                    "kimi-k3": "sa-kimi-k3-responses"},
    }


def test_alias_write_preserves_the_other_alias_entries():
    value = key(aliases={"gpt-6-astra": "chatgpt-gpt-6-astra",
                         "kimi-k2.7-code": "claude-kimi-k2.7-code"})
    change = allowlist.plan_key(value, set_aliases={"kimi-k3": "sa-kimi-k3"})
    assert change["aliases"] == {
        "gpt-6-astra": "chatgpt-gpt-6-astra",
        "kimi-k2.7-code": "claude-kimi-k2.7-code",
        "kimi-k3": "sa-kimi-k3",
    }
    assert "models" not in change


def test_alias_is_idempotent_and_retarget_is_detected():
    value = key(aliases={"kimi-k3": "sa-kimi-k3"})
    assert allowlist.plan_key(value, set_aliases={"kimi-k3": "sa-kimi-k3"}) is None
    change = allowlist.plan_key(value, set_aliases={"kimi-k3": "sa-kimi-k3-responses"})
    assert change == {"aliases": {"kimi-k3": "sa-kimi-k3-responses"}}


def test_rm_alias_drops_only_the_named_source():
    value = key(aliases={"kimi-k3": "sa-kimi-k3", "gpt-6-astra": "chatgpt-gpt-6-astra"})
    change = allowlist.plan_key(value, rm_aliases=["kimi-k3"])
    assert change == {"aliases": {"gpt-6-astra": "chatgpt-gpt-6-astra"}}
    assert allowlist.plan_key(value, rm_aliases=["never-there"]) is None


def test_select_keys_filters_by_prefix_and_skips_blocked():
    keys = [
        key(key_alias="cursor-alice-1"),
        key(key_alias="claude-code-bob-2"),
        key(key_alias="carher-153"),
        key(key_alias="cursor-blocked-3", blocked=True),
    ]
    selected = [k["key_alias"] for k in allowlist.select_keys(keys, ("cursor-", "claude-"))]
    assert selected == ["cursor-alice-1", "claude-code-bob-2"]


def test_select_keys_include_blocked_is_opt_in():
    # Revoking a model has to reach blocked keys too: they keep their allowlist,
    # so unblocking one later would restore the model that was just taken away.
    keys = [
        key(key_alias="cursor-alice-1"),
        key(key_alias="carher-153"),
        key(key_alias="cursor-blocked-3", blocked=True),
    ]
    selected = [
        k["key_alias"]
        for k in allowlist.select_keys(keys, ("cursor-",), include_blocked=True)
    ]
    assert selected == ["cursor-alice-1", "cursor-blocked-3"]


def test_select_keys_exclude_carves_out_exact_aliases():
    """「全部 cursor key，除了我自己那几把」—— 别用 600 个 --only 拼命令行。

    那串参数经 ssh 会被远端重新分词，argparse 只认第一个，其余变成裸位置参数，
    结果是**只写了一把**。所以扫描范围要用 prefix，挖洞要用 exclude。
    """
    keys = [
        key(key_alias="cursor-alice-1"),
        key(key_alias="cursor-liuguoxian-std"),
        key(key_alias="cursor-bob-2"),
    ]
    selected = [
        k["key_alias"]
        for k in allowlist.select_keys(keys, ("cursor-",),
                                       exclude=("cursor-liuguoxian-std",))
    ]
    assert selected == ["cursor-alice-1", "cursor-bob-2"]


def test_select_keys_exclude_prefix_catches_the_suffixed_twin():
    """🔴 发 key 的流水线会给 alias 追加 4 位随机后缀。

    按**精确名**排除 `cursor-liuguoxian` 会漏掉 `cursor-liuguoxian-l08v`，
    而那正是要留下的那把 —— 漏掉 = 写进去了。所以这种场合必须按前缀挖洞。
    """
    keys = [
        key(key_alias="cursor-liuguoxian-l08v"),
        key(key_alias="cursor-liuguoxian02-7w7g"),
        key(key_alias="cursor-liuguoxian-std"),
        key(key_alias="cursor-alice-1"),
    ]
    by_exact = [
        k["key_alias"]
        for k in allowlist.select_keys(keys, ("cursor-",), exclude=("cursor-liuguoxian",))
    ]
    # 阳性对照：精确名这把尺子在这里是**坏的**，三把都漏网。
    assert by_exact == ["cursor-liuguoxian-l08v", "cursor-liuguoxian02-7w7g",
                        "cursor-liuguoxian-std", "cursor-alice-1"]

    by_prefix = [
        k["key_alias"]
        for k in allowlist.select_keys(keys, ("cursor-",),
                                       exclude_prefixes=("cursor-liuguoxian",))
    ]
    assert by_prefix == ["cursor-alice-1"]


def test_select_keys_empty_exclude_changes_nothing():
    """空的排除清单不许把范围悄悄清空（默认参数的退化形状）。"""
    keys = [key(key_alias="cursor-alice-1"), key(key_alias="cursor-bob-2")]
    assert len(allowlist.select_keys(keys, ("cursor-",), exclude=(), exclude_prefixes=())) == 2


def test_parse_alias_rejects_malformed_specs():
    import pytest

    assert allowlist.parse_alias("a=b") == ("a", "b")
    assert allowlist.parse_alias("a=b=c") == ("a", "b=c")
    for bad in ("ab", "=b", "a="):
        with pytest.raises(Exception):
            allowlist.parse_alias(bad)


REF_MODELS = ["glm-5.3-flash", "gpt-5.6-sol", "sa-grok-4.6"]
REF_ALIASES = {"glm-5.3-flash": "zai-coding-glm-5.3-flash"}


def test_converge_replaces_both_fields_wholesale():
    plan = allowlist.plan_converge(
        key(models=["a", "b", "c", "d"], aliases={"a": "x", "b": "y"}),
        REF_MODELS, REF_ALIASES,
    )
    assert plan == {"models": REF_MODELS, "aliases": REF_ALIASES}


def test_converge_preserves_reference_order_so_rows_are_byte_identical():
    plan = allowlist.plan_converge(
        key(models=["sa-grok-4.6", "gpt-5.6-sol", "glm-5.3-flash"], aliases={}),
        REF_MODELS, REF_ALIASES,
    )
    # Same set, different order -> still rewritten, otherwise the acceptance
    # criterion "count(distinct md5(models::text))=1" can never reach 1.
    assert plan["models"] == REF_MODELS


def test_converge_is_idempotent():
    assert allowlist.plan_converge(
        key(models=list(REF_MODELS), aliases=dict(REF_ALIASES)),
        REF_MODELS, REF_ALIASES,
    ) is None


def test_converge_never_narrows_an_unrestricted_key():
    plan = allowlist.plan_converge(key(models=[], aliases={}), REF_MODELS, REF_ALIASES)
    assert plan == {"aliases": REF_ALIASES}


def test_upstream_changes_flags_only_names_that_stay_callable():
    changes = allowlist.upstream_changes(
        key(
            models=["glm-5.3-flash", "gpt-5.6-sol", "sa-grok-4.6", "gpt-5.4"],
            aliases={
                # survives in ref_models, target switches -> silent provider swap
                "glm-5.3-flash": "openrouter-glm-5.3-flash",
                # survives in ref_models, alias disappears -> falls to same-name group
                "gpt-5.6-sol": "chatgpt-gpt-5.6-sol",
                # name leaves the allowlist too -> 403, not silent
                "gpt-5.4": "chatgpt-gpt-5.4",
            },
        ),
        REF_MODELS, REF_ALIASES,
    )
    assert changes == [
        ("glm-5.3-flash", "openrouter-glm-5.3-flash", "zai-coding-glm-5.3-flash"),
        ("gpt-5.6-sol", "chatgpt-gpt-5.6-sol", None),
    ]


def test_upstream_changes_is_empty_when_aliases_already_match():
    assert allowlist.upstream_changes(
        key(models=list(REF_MODELS), aliases=dict(REF_ALIASES)),
        REF_MODELS, REF_ALIASES,
    ) == []


def test_upstream_changes_treats_unrestricted_key_as_fully_exposed():
    changes = allowlist.upstream_changes(
        key(models=[], aliases={"gpt-5.4": "chatgpt-gpt-5.4"}),
        REF_MODELS, REF_ALIASES,
    )
    # models=[] keeps every name callable, so even a name absent from the
    # reference allowlist still changes where that user lands.
    assert changes == [("gpt-5.4", "chatgpt-gpt-5.4", None)]


# --------------------------------------------------------------------------- #
# dropped_names: the LOUD half of a convergence. Until 2026-09-26 the tool only
# reported silent provider swaps, while the bigger user-facing impact of that
# day's 688-key sweep -- anthropic.claude-fable-5 leaving 9 keys' allowlists
# with no equivalent on the reference -- went unmentioned entirely.
# --------------------------------------------------------------------------- #
def test_dropped_names_lists_names_leaving_the_allowlist():
    value = key(models=["glm-5.3-flash", "sa-grok-4.6", "anthropic.claude-fable-5"])
    assert allowlist.dropped_names(value, REF_MODELS) == ["anthropic.claude-fable-5"]


def test_dropped_names_is_empty_once_converged():
    assert allowlist.dropped_names(key(models=list(REF_MODELS)), REF_MODELS) == []


def test_dropped_names_never_fires_on_an_unrestricted_key():
    # models == [] is never narrowed, so nothing leaves the allowlist.
    assert allowlist.dropped_names(key(models=[]), REF_MODELS) == []


def test_dropped_names_is_disjoint_from_upstream_changes():
    # The two reports must not double-count: a name that leaves `models` 403s
    # (loud), a name that stays and retargets is silent. Same key, both faces.
    value = key(
        models=["glm-5.3-flash", "anthropic.claude-fable-5"],
        aliases={"glm-5.3-flash": "openrouter-glm-5.3-flash",
                 "anthropic.claude-fable-5": "cursor-fable-5"},
    )
    loud = set(allowlist.dropped_names(value, REF_MODELS))
    silent = {src for src, _old, _new in
              allowlist.upstream_changes(value, REF_MODELS, REF_ALIASES)}
    assert loud == {"anthropic.claude-fable-5"}
    assert silent == {"glm-5.3-flash"}
    assert loud.isdisjoint(silent)


# --------------------------------------------------------------------------- #
# convergence_residual: answers "全部改完了吗" over the whole prefix scope.
# 2026-09-26: a sweep reported applied_ok=668 readback_ok=668/668 mismatches=0
# and was still 23 keys short of "every key". readback_ok cannot see a key that
# was never in scope -- it reads identically to a key that needed no change.
# --------------------------------------------------------------------------- #
def residual(keys, **kwargs):
    return allowlist.convergence_residual(
        keys, ("claude-code-",), "claude-code-std", REF_MODELS, REF_ALIASES, **kwargs,
    )


def converged_key(alias, **overrides):
    return key(key_alias=alias, models=list(REF_MODELS),
               aliases=dict(REF_ALIASES), **overrides)


def test_residual_is_clean_when_every_key_matches():
    out = residual([converged_key("claude-code-a"), converged_key("claude-code-b")])
    assert out == {"spared": [], "blocked": [], "unrestricted": [], "mismatch": []}


def test_residual_surfaces_blocked_keys_that_were_never_in_scope():
    # The exact false green of 2026-09-26: the write was flawless and 19 keys
    # still carried the pre-sweep shape, because blocked keys are out of scope
    # by default and an out-of-scope key looks just like a no-op.
    out = residual([
        converged_key("claude-code-a"),
        key(key_alias="claude-code-old", models=["legacy"], blocked=True),
    ])
    assert out["blocked"] == ["claude-code-old"]
    assert out["mismatch"] == []


def test_residual_counts_blocked_as_mismatch_once_they_are_in_scope():
    out = residual(
        [key(key_alias="claude-code-old", models=["legacy"], blocked=True)],
        include_blocked=True,
    )
    assert out["blocked"] == []
    assert out["mismatch"] == ["claude-code-old"]


def test_residual_buckets_unrestricted_keys_separately_from_failures():
    # models == [] can never equal the reference and that is by design, not a
    # failed write -- it must not be reported in the bucket that returns red.
    out = residual([key(key_alias="claude-code-canary", models=[],
                        aliases=dict(REF_ALIASES))])
    assert out["unrestricted"] == ["claude-code-canary"]
    assert out["mismatch"] == []


def test_residual_treats_carve_outs_and_the_reference_itself_as_intended():
    out = residual(
        [
            key(key_alias="claude-code-std", models=["whatever"]),
            key(key_alias="claude-code-mine-7w7g", models=["whatever"]),
            key(key_alias="claude-code-keep", models=["whatever"]),
        ],
        exclude=("claude-code-keep",),
        exclude_prefixes=("claude-code-mine",),
    )
    assert out["spared"] == ["claude-code-keep", "claude-code-mine-7w7g", "claude-code-std"]
    assert out["mismatch"] == []


def test_residual_ignores_keys_outside_the_prefix():
    out = residual([key(key_alias="cursor-somebody", models=["whatever"])])
    assert all(v == [] for v in out.values())


def test_residual_flags_a_genuinely_unconverged_in_scope_key():
    out = residual([key(key_alias="claude-code-a", models=["stale"], aliases={})])
    assert out["mismatch"] == ["claude-code-a"]


def test_residual_requires_alias_equality_not_just_model_equality():
    # A key can carry the right allowlist and still route somewhere else.
    out = residual([key(key_alias="claude-code-a", models=list(REF_MODELS),
                        aliases={"glm-5.3-flash": "openrouter-glm-5.3-flash"})])
    assert out["mismatch"] == ["claude-code-a"]


def test_residual_does_not_list_a_blocked_key_that_is_already_converged():
    # First revision bucketed by flag before testing identity, so 19 keys that
    # had just been converged with --include-blocked were counted in
    # converged_identical AND listed under `blocked` -- same key, two opposite
    # verdicts in one report. Buckets hold only keys that still differ.
    out = residual([converged_key("claude-code-old", blocked=True)])
    assert out["blocked"] == []
    assert all(v == [] for v in out.values())


def test_residual_buckets_partition_the_scope_with_the_identical_ones():
    keys = [
        converged_key("claude-code-ok"),
        converged_key("claude-code-ok-blocked", blocked=True),
        key(key_alias="claude-code-std", models=["ref-is-spared"]),
        key(key_alias="claude-code-stale", models=["stale"], aliases={}),
        key(key_alias="claude-code-blk", models=["stale"], blocked=True),
        key(key_alias="claude-code-free", models=[]),
    ]
    out = residual(keys)
    identical = sum(
        1 for k in keys
        if list(k.get("models") or []) == REF_MODELS
        and dict(k.get("aliases") or {}) == REF_ALIASES
    )
    assert identical == 2
    assert identical + sum(len(v) for v in out.values()) == len(keys)


# --only / --only-file: 2026-09-29 `--prefix carher-` hit 237 keys but the intended
# set was 227 (carher-hangzhou + 09-28 test keys had to be spared). An exact list
# was the fix; an exact list whose typo'd name silently drops out is the new trap.
def test_filter_only_keeps_exact_matches_and_reports_unmatched():
    keys = [key(key_alias="carher-1"), key(key_alias="carher-2"),
            key(key_alias="carher-hangzhou")]
    kept, unmatched = allowlist.filter_only(keys, ["carher-1", "carher-2", "carher-3"])
    assert [k["key_alias"] for k in kept] == ["carher-1", "carher-2"]
    assert unmatched == ["carher-3"]


def test_filter_only_is_exact_not_prefix():
    keys = [key(key_alias="carher-14"), key(key_alias="carher-14-probe-r3-1790566659")]
    kept, unmatched = allowlist.filter_only(keys, ["carher-14"])
    assert [k["key_alias"] for k in kept] == ["carher-14"]
    assert unmatched == []


def test_filter_only_reports_a_blocked_key_as_unmatched():
    # select_keys already dropped the blocked key; the name must not vanish quietly.
    scoped = allowlist.select_keys(
        [key(key_alias="carher-1"), key(key_alias="carher-2", blocked=True)], ("carher-",))
    _, unmatched = allowlist.filter_only(scoped, ["carher-1", "carher-2"])
    assert unmatched == ["carher-2"]


def test_read_only_file_skips_blank_and_comment_lines(tmp_path):
    path = tmp_path / "targets.txt"
    path.write_text("carher-1\n\n# spared: carher-hangzhou\n  carher-2  \n", encoding="utf-8")
    assert allowlist.read_only_file(str(path)) == ["carher-1", "carher-2"]
