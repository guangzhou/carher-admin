from __future__ import annotations

import collections
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-198-key-error-triage.py"
SPEC = importlib.util.spec_from_file_location("litellm_198_key_error_triage", SCRIPT)
assert SPEC and SPEC.loader
triage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(triage)


def dep(model, api_base=None):
    return {"litellm_params": {"model": model, "api_base": api_base}}


def test_matches_upstream_sees_api_base_not_only_model_prefix():
    # openrouter-gpt-5.6-* 是 openai/ 前缀 + openrouter.ai base，只看 model 会漏
    assert triage.matches_upstream(dep("openai/gpt-5.6-sol", "https://openrouter.ai/api/v1"), "openrouter")
    assert triage.matches_upstream(dep("openrouter/z-ai/glm-5"), "OpenRouter")
    assert not triage.matches_upstream(dep("openai/deepseek-flash", "https://api.deepseek.com"), "openrouter")


def test_classify_error_shapes_seen_on_10_20():
    assert "欠费" in triage.classify_error("402", "OpenrouterException - Insufficient credits. Add more")
    assert "空请求" in triage.classify_error("400", "Input items array must not be empty")
    # 10-22：进网关 7.6MB、出去 0 条 = 我们 deepseek 裁剪删空的，不许再判成客户端
    big = triage.classify_error("400", "Input items array must not be empty", 7_618_191)
    assert "网关" in big and "客户端" not in big.split("——")[0]
    assert "客户端" in triage.classify_error("400", "Input items array must not be empty", 2_000)
    assert "连不上" in triage.classify_error("500", "OpenAIException - Connection error.")
    assert "白名单" in triage.classify_error("403", "key_model_access_denied")
    # 10-20 网关对外 403 的措辞（自家 error_sanitize 改写过）
    assert "白名单" in triage.classify_error("403", "The requested model 'jev' is not available for this API key")
    assert triage.classify_error("402", "something new") == "未归类，看原文"


def test_fallback_chain_per_key_table_replaces_global():
    glob = [{"a": ["g1"]}, {"b": ["g2"]}]
    assert triage.fallback_chain("a", None, glob) == ["g1"]
    # per-key 表非空 ⇒ 整表替换：全局里有的 b 也查不到
    assert triage.fallback_chain("b", {"fallbacks": [{"a": ["k1"]}]}, glob) == []
    assert triage.fallback_chain("a", {"fallbacks": [{"a": ["k1"]}]}, glob) == ["k1"]
    assert triage.fallback_chain("a", {"fallbacks": []}, glob) == ["g1"]


def test_key_exposure_resolves_alias_and_skips_blocked():
    dead = {"openrouter-glm-5.3-flash", "openrouter-claude-opus-4-6"}
    keys = [
        {"key_alias": "cursor-a-1", "models": ["glm-5.3-flash"], "aliases": {"glm-5.3-flash": "openrouter-glm-5.3-flash"}},
        {"key_alias": "cursor-b-1", "models": ["glm-5.3-flash"], "aliases": {"glm-5.3-flash": "zai-claude-glm-5.3-flash"}},
        {"key_alias": "cursor-c-1", "blocked": True, "models": ["openrouter-glm-5.3-flash"]},
        {"key_alias": "carher-1", "models": ["x"],
         "router_settings": {"fallbacks": [{"claude-opus-4-6": ["openrouter-claude-opus-4-6", "kiro-x"]}]}},
        {"key_alias": None, "models": []},
    ]
    ex = triage.key_exposure(keys, dead, {})
    assert ex["allow"] == {("glm-5.3-flash", "openrouter-glm-5.3-flash"): collections.Counter({"cursor": 1})}
    assert ex["pkfb"] == {("claude-opus-4-6", ("openrouter-claude-opus-4-6",), True): collections.Counter({"carher": 1})}
    assert ex["unrestricted"] == 1


def test_key_exposure_uses_global_alias_when_key_has_none():
    ex = triage.key_exposure([{"key_alias": "cursor-a", "models": ["m"]}], {"dead"}, {"m": "dead"})
    assert ("m", "dead") in ex["allow"]


def test_global_exposure_tags_missing_and_dead_sources():
    fb = [{"gone": ["dead"]}, {"dead": ["live"]}, {"live": ["x", "dead"]}, {"ok": ["x"]}]
    out = triage.global_exposure(fb, {"pub": "dead"}, {"dead"}, {"dead", "live", "ok", "x"})
    lines = [l for _, l in out]
    assert ("alias", "pub -> dead") in out
    assert any(l.startswith("gone") and "源组已不存在" in l for l in lines)
    assert any(l.startswith("dead") and "源组本身是死组" in l for l in lines)
    assert any(l.startswith("live") and "dead" in l for l in lines)
    assert not any(l.startswith("ok") for l in lines)


def test_traffic_splits_synthetic_direct_and_fallback():
    rows = [
        {"model_group": "g", "req_model": "g", "status": "failure", "api_key": "t1", "n": 5, "last": "x"},
        {"model_group": "g", "req_model": "deepseek-v4.1-flash-responses", "status": "failure", "api_key": "t2", "n": 3, "last": "x"},
        {"model_group": "g", "req_model": "g", "status": "failure", "api_key": "hz", "n": 1000, "last": "x"},
        {"model_group": "g", "req_model": "g", "status": "failure", "api_key": "deleted-probe", "n": 2, "last": "x"},
    ]
    out = triage.traffic(rows, {"t1": "cursor-a", "t2": "cursor-b", "hz": "carher-hangzhou"}, ("carher-hangzhou",))
    assert out["synthetic"]["g"] == 1000
    assert out["unknown"]["g"] == 2
    assert [x[0] for x in out["direct"]["g"]] == ["cursor-a"]
    assert [x[0] for x in out["via_fallback"]["g"]] == ["cursor-b"]
