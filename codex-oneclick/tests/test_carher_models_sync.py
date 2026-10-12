"""carher_models_sync.py 的回归测试：每一条都对应 2026-10-09 实测踩过的坑。

跑法：python3 -m pytest codex-oneclick/tests -q
"""

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "carher_models_sync.py"
spec = importlib.util.spec_from_file_location("carher_models_sync", SRC)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def official(slug, **kw):
    """模拟 codex debug models --bundled 里的一条官方模型。"""
    m = {
        "slug": slug, "display_name": slug, "description": slug, "visibility": "list",
        "supported_in_api": True, "priority": 1, "context_window": 272000,
        "max_context_window": 872000, "tool_mode": "code_mode_only", "use_responses_lite": True,
        "apply_patch_tool_type": "freeform", "multi_agent_version": "v2",
        "additional_speed_tiers": ["fast"], "service_tiers": [{"id": "priority"}],
        "upgrade": {"model": "x"}, "availability_nux": {"x": 1},
    }
    m.update(kw)
    return m


TEMPLATES = [
    official("gpt-5.6-terra"),
    official("gpt-5.5", tool_mode=None, use_responses_lite=False, multi_agent_version=None),
    official("gpt-5.4", visibility="hide"),
]


def build(ids, limits=None):
    out = sync.build_catalog(ids, TEMPLATES, limits or {})
    return {m["slug"]: m for m in out["models"]}


def test_non_openai_model_gets_plain_function_tools():
    # 套 5.6 的 code_mode_only ⇒ grok 拿不到 shell；freeform ⇒ claude/kimi 写不出 patch
    m = build(["grok-4.7"])["grok-4.7"]
    assert m["tool_mode"] is None
    assert m["use_responses_lite"] is False
    assert m["apply_patch_tool_type"] is None
    assert m["multi_agent_version"] is None
    assert m["service_tiers"] == [] and m["additional_speed_tiers"] == []
    assert "upgrade" not in m and "availability_nux" not in m


def test_official_models_keep_official_metadata():
    m = build(["gpt-5.6-terra"])["gpt-5.6-terra"]
    assert m["tool_mode"] == "code_mode_only"
    assert m["apply_patch_tool_type"] == "freeform"


def test_visibility_follows_key_allowlist():
    cat = build(["gpt-5.4", "grok-4.7"])
    assert cat["gpt-5.4"]["visibility"] == "list"       # 官方隐藏但 key 能用 ⇒ 显示
    assert cat["gpt-5.6-terra"]["visibility"] == "hide"  # key 没有 ⇒ 隐藏


def test_image_and_review_models_not_in_picker():
    cat = build(["gpt-image-2", "grok-imagine-image-2.0", "codex-auto-review", "grok-4.7"])
    assert "gpt-image-2" not in cat and "grok-imagine-image-2.0" not in cat
    assert "codex-auto-review" not in cat


def test_context_window_capped_by_gateway_never_raised():
    cat = build(["claude-haiku-4-5", "jev"], {"claude-haiku-4-5": 200000, "jev": 1000000})
    assert cat["claude-haiku-4-5"]["context_window"] == 200000
    assert cat["claude-haiku-4-5"]["max_context_window"] == 200000
    assert cat["jev"]["context_window"] == 272000  # 网关更大也不放大


@pytest.mark.parametrize("path", [
    r"C:\Users\O'Brien\.codex\carher-models.json",
    r"C:\Users\张三\.codex\carher-models.json",
    "/Users/a b/.codex/carher-models.json",
])
def test_catalog_path_roundtrips_through_toml(path):
    text = sync.ensure_config_line('model = "x"\n\n[model_providers.p]\nname = "p"\n', path)
    assert tomllib.loads(text)["model_catalog_json"] == path
    assert str(sync.catalog_path_from_config(text)) == str(Path(path))
    # 顶层键必须落在第一个 [table] 之前，否则会被吃进那个表
    assert text.index("model_catalog_json") < text.index("[model_providers.p]")


def test_legacy_single_quoted_catalog_line_still_read():
    assert sync.catalog_path_from_config("model_catalog_json = '/x/y.json'\n") == Path("/x/y.json")


def test_generated_catalog_is_json_serialisable_utf8():
    out = sync.build_catalog(["grok-4.7"], TEMPLATES, {})
    raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
    assert "CarHer 网关模型".encode("utf-8") in raw
