"""cursor-key-notice-build.py 的纯逻辑单测。

钉住的是 10-15 / 10-19 两轮群发里真实踩过、或用户明确拍板的东西：
定稿卡片不许悄悄变样、每人只看到自己的模型、选 key 的规则、卡片里飞书不支持的写法。
"""

import importlib.util
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("notice", ROOT / "scripts" / "cursor-key-notice-build.py")
nb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nb)

FIXTURE = json.loads((ROOT / "backend/tests/fixtures/cursor_notice_std_v7.json").read_text())
STD_MODELS = FIXTURE["models"]
MAINSTREAM = [m for m in STD_MODELS
              if m not in {"gemini-4-argon", "gemini-3.5-flash-lite", "gemini-3.1-pro",
                           "gemini-image", "gemini-music", "gemini-video", "web-gemini-video",
                           "muse-image", "muse-spark"}]


def chips(card):
    return re.findall(r"<text_tag color='(?:green|neutral)'>([^<]+)</text_tag>",
                      json.dumps(card, ensure_ascii=False))


# ------------------------------------------------------------ 定稿卡片

def test_card_matches_the_version_user_signed_off():
    # 10-19 用户确认的骨架（11:17 + 11:23 自助查询 + 新看板链接）；10-27 加 terra（experience）和「刷模型」。
    # 改样式必须连夹具一起更新，并重新发给刘国现确认。
    assert nb.build_card(STD_MODELS) == FIXTURE["card"]


def test_every_card_passes_gate():
    for models in (STD_MODELS, MAINSTREAM):
        card = nb.build_card(models)
        assert nb.gate(card, json.dumps(card, ensure_ascii=False), models) == ""


def test_card_lists_exactly_this_persons_models():
    card = nb.build_card(MAINSTREAM)
    got = chips(card)
    # 主推模型在顶部再出现一次，其余每个模型恰好一次
    assert set(got) == set(MAINSTREAM)
    assert len(got) == len(MAINSTREAM) + len(nb.HIGHLIGHT & set(MAINSTREAM))
    assert "gemini-4-argon" not in json.dumps(card, ensure_ascii=False)   # std 有、同事没有


def test_header_count_is_per_person():
    blob = json.dumps(nb.build_card(MAINSTREAM), ensure_ascii=False)
    assert f"{len(MAINSTREAM)} 个模型" in blob


def test_no_highlight_block_when_person_has_none():
    card = nb.build_card(["gpt-5.5", "grok-4.7", "glm-5.3-flash", "jev", "qwen3-coder-next"])
    assert "本次主推" not in json.dumps(card, ensure_ascii=False)


def test_experience_label_only_on_terra_and_gate_still_matches_real_name():
    blob = json.dumps(nb.build_card(STD_MODELS), ensure_ascii=False)
    assert "gpt-6.1-terra</text_tag><font color='grey'>（experience）</font>" in blob
    assert blob.count("（experience）") == blob.count("gpt-6.1-terra</text_tag>")


def test_self_service_has_three_commands():
    blob = json.dumps(nb.build_card(MAINSTREAM), ensure_ascii=False)
    for c in ("`查余额`", "`cye`", "`测模型`", "`刷模型`"):
        assert c in blob


def test_media_models_collapsed():
    blob = json.dumps(nb.build_card(STD_MODELS), ensure_ascii=False)
    assert '"tag": "collapsible_panel"' in blob and "点击展开" in blob


@pytest.mark.parametrize("bad,why", [
    ("<u>", "卡片 2.0 不支持下划线，会原样显示成字"),
])
def test_gate_rejects_unsupported_markup(bad, why):
    card = nb.build_card(MAINSTREAM)
    blob = json.dumps(card, ensure_ascii=False).replace("10 月底下线", f"{bad}10 月底下线")
    assert "<u>" in nb.gate(card, blob, MAINSTREAM), why


def test_gate_rejects_someone_elses_models():
    card = nb.build_card(STD_MODELS)
    assert "不符" in nb.gate(card, json.dumps(card, ensure_ascii=False), MAINSTREAM)


def test_no_column_has_corner_radius():
    # column 没有 corner_radius，飞书 200621 拒收整张卡；圆角只能放在 interactive_container 上
    def walk(x):
        if isinstance(x, dict):
            assert not (x.get("tag") == "column" and "corner_radius" in x), x
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(nb.build_card(STD_MODELS))


def test_card_never_shows_internal_upstream_names():
    # 10-15 朱奕达 / 韩广通的老 key 白名单里直接写着 chatgpt-gpt-5.5、wangsu-gpt-5.4
    leaky = MAINSTREAM + ["chatgpt-gpt-5.5", "wangsu-gpt-5.4", "kiro-claude-opus-5"]
    blob = json.dumps(nb.build_card(leaky), ensure_ascii=False)
    for name in ("chatgpt-gpt-5.5", "wangsu-gpt-5.4", "kiro-claude-opus-5"):
        assert name not in blob
    assert f"{len(MAINSTREAM)} 个模型" in blob


def test_internal_upstream_names_are_stripped():
    keep, drop = nb.strip_internal(["gpt-5.5", "chatgpt-gpt-5.5", "wangsu-gpt-5.4", "kiro-claude-opus-5"])
    assert keep == ["gpt-5.5"] and len(drop) == 3


@pytest.mark.parametrize("name,fam", [
    ("claude-opus-5-5", "Claude"), ("codex-auto-review", "GPT"), ("gpt-reserve", "GPT"),
    ("composer-2.5-fast", "Grok / Composer"), ("grok-imagine", "出图"), ("web-gemini-video", "出图"),
    ("gemini-4-argon", "Gemini"), ("muse-spark", "其他"), ("kimi-k3", "其他"),
])
def test_family(name, fam):
    assert nb.family(name) == fam


# ------------------------------------------------------------ 选哪把 key

def test_owner_always_gets_std():
    rows = [{"key": "cursor-liuguoxian"}, {"key": "cursor-liuguoxian03"}, {"key": nb.MY_KEY}]
    assert nb.pick_key(nb.MY_OPEN_ID, rows)["key"] == nb.MY_KEY


def test_others_get_the_pinyin_named_key():
    rows = [{"key": "cursor-t1v"}, {"key": "cursor-baiyu"}]
    assert nb.pick_key("ou_other", rows)["key"] == "cursor-baiyu"


def test_org_shared_keys_are_recognised():
    assert nb.ORG_SHARE.match("cursor-meituan-10")
    assert nb.ORG_SHARE.match("cursor-beiqiyanjiuyuan-3")
    assert not nb.ORG_SHARE.match("cursor-zhouqifeng-d255")


def test_table_alias_without_suffix_resolves_to_db_key():
    db = {"cursor-buyitian-pwcf": {}, "cursor-buyitian02-abcd": {}}
    import collections
    by_base = collections.defaultdict(list)
    for k in db:
        by_base[nb.RANDOM_SUFFIX.sub("", k)].append(k)
    assert nb.resolve_db_key("cursor-buyitian", db, by_base) == "cursor-buyitian-pwcf"
    assert nb.resolve_db_key("cursor-nobody", db, by_base) is None
