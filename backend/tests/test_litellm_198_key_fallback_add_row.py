"""litellm-198-key-fallback-add-row.py 的纯逻辑：选哪些 key、追加形状、审计分组。

钉住 2026-10-15 的事实：per-key fallbacks 非空时整表替换全局表，所以
「全局加了行」对这些 key 无效；空表 key 读全局表，不能往它们身上写（写了反而冻结）。
"""
import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "litellm-198-key-fallback-add-row.py")
_spec = importlib.util.spec_from_file_location("kfb_add_row", _PATH)
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

G = "wangsu-deepseek-v4.1-flash"
T = ["deepseek-v4.1-flash", "zai-coding-glm-5.3-flash"]


def k(alias, models=(), aliases=None, table=None):
    return {"key_alias": alias, "models": list(models), "aliases": aliases or {},
            "router_settings": {"fallbacks": table if table is not None else []}}


KEYS = [
    k("via-alias", ["x"], {"deepseek-v4.1-flash": G}, [{"x": ["y"]}]),
    k("via-models", [G], {}, [{"x": ["y"]}]),
    k("empty-table", ["x"], {"deepseek-v4.1-flash": G}, []),
    k("no-reach", ["x"], {}, [{"x": ["y"]}]),
    k("has-row", ["x"], {"q": G}, [{"x": ["y"]}, {G: ["z"]}]),
]


def test_plan_picks_only_reachable_keys_with_nonempty_table_missing_row():
    got = {key["key_alias"]: new for key, new in M.plan(KEYS, G, T)}
    assert set(got) == {"via-alias", "via-models"}


def test_plan_appends_without_touching_existing_rows_or_input():
    got = {key["key_alias"]: new for key, new in M.plan(KEYS, G, T)}
    assert got["via-alias"] == [{"x": ["y"]}, {G: T}]
    assert KEYS[0]["router_settings"]["fallbacks"] == [{"x": ["y"]}]


def test_plan_is_idempotent_after_apply():
    applied = [dict(key, router_settings={"fallbacks": new}) for key, new in M.plan(KEYS, G, T)]
    assert M.plan(applied, G, T) == []


def test_for_alias_source_selects_by_public_name_before_retarget():
    keys = [k("hp", ["her-pro"], {"her-pro": "old-group"}, [{"x": ["y"]}]),
            k("hp-empty", ["her-pro"], {"her-pro": "old-group"}, []),
            k("other", ["her-pro"], {}, [{"x": ["y"]}])]
    assert [key["key_alias"] for key, _ in M.plan(keys, "new-group", T, "her-pro")] == ["hp"]
    assert M.plan(keys, "new-group", T) == []


def test_audit_groups_by_which_table_the_key_reads():
    r = M.audit(KEYS, G)
    assert r["global_table"] == ["empty-table"]
    assert r["per_key_has_row"] == ["has-row"]
    assert sorted(r["missing"]) == ["via-alias", "via-models"]
