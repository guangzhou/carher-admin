"""`scripts/litellm-198-cursor-newnames-probe.py` 的判据纪律单测。

这支脚本是「这 6 个名字能不能进 Cursor 菜单」的**唯一尺子**，所以这里测的不是
它能不能跑通，而是**尺子的几个不许退化的性质**：只许打 Cursor 真线型
（chat + stream + tools）、阴性对照必须存在且能让整轮作废、空 models 必须拒跑
（空 models 在 LiteLLM 里等于全放行 = 绕开白名单 = 必然假绿）、临时 key 必须在
finally 里删。

(10-19 起装机包菜单按 key 动态拉取,不再有写死的 DEFAULT_MODELS,原来那三条跨文件对账腿已删。)
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "litellm-198-cursor-newnames-probe.py"
SETUP_JS = ROOT / "scripts" / "zk-cursor-web" / "cursor_team_setup.js"

SRC = SCRIPT.read_text(encoding="utf8")
# 去掉模块 docstring + 行注释后的**代码**。脚本头部的注释里正当地提到了
# `/v1/images/generations`（解释"为什么另起一把尺子"），那是纪律不是违例，
# 按整份原文断言会把说明文字读成"打了 image 端点"。
CODE = "\n".join(
    ln.split("#", 1)[0]
    for ln in SRC.split('"""', 2)[-1].splitlines()
)

# 用户 2026-09-20 点名的名字。当天点名 6 个，`sa-grok-imagine` 被探针判红
# （chat 线型恒 400 —— 出图专用）后同日下架，所以这里是 5 个。
# ⚠️ 它现在在 `RETIRED_MODELS` 里；加回这个集合会让
#    test_retired_name_is_not_in_the_probe_list 直接变红（那是**对的**，不是要修的）。
EXPECTED = {
    "sa-composer-2.5-fast",
    "sa-grok-4.20-0309-reasoning",
    "sa-grok-4.6-latest",
    "sa-grok-4.5-latest",
    "qwen3-coder-next",
}


def _load():
    spec = importlib.util.spec_from_file_location("cursor_newnames_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_exists_and_imports():
    assert SCRIPT.exists()
    mod = _load()
    assert mod.NAMES


def test_the_code_extraction_itself_works():
    """第 0 步：会「只看代码行」的断言必须先证明自己抠到了代码。

    docstring 分割一旦失配，CODE 会变成空串，下面所有 `not in CODE` 都恒绿。
    """
    assert "def main()" in CODE and "/v1/chat/completions" in CODE
    assert "本探针量的就是后者" not in CODE, "模块 docstring 没被剥掉"


def test_names_are_exactly_the_user_named_models():
    mod = _load()
    assert set(mod.NAMES) == EXPECTED
    assert len(mod.NAMES) == len(set(mod.NAMES))


def test_retired_name_is_not_in_the_probe_list():
    """退役名不该出现在「新增待探」清单里，否则读数方向自相矛盾。"""
    js = SETUP_JS.read_text(encoding="utf8")
    retired = set(re.findall(
        r'"([^"]+)"', js.split("const RETIRED_MODELS = [", 1)[1].split("]", 1)[0]))
    assert retired, "RETIRED_MODELS 抠空了（常量改名或换行了？）"
    mod = _load()
    assert retired & set(mod.NAMES) == set()


def test_every_name_is_probed_on_cursors_real_call_shape():
    """Cursor 只走 `/v1/chat/completions` + stream + tools。

    换端点（比如给 image 名挑 `/v1/images/generations`）会把「同事点一下就 400」
    的名字读成绿 —— 这把尺子量的就是 Cursor 那条线，不许放宽。
    """
    assert "/v1/chat/completions" in CODE
    assert "/v1/images/generations" not in CODE, \
        "这把尺子只量 Cursor 线型；按 mode 挑端点的那把是 grok6-cursor-probe"
    body = SRC.split("for n in NAMES:", 1)[1].split("# 阴性对照", 1)[0] \
        if "# 阴性对照" in SRC else SRC.split("for n in NAMES:", 1)[1]
    assert '"stream": True' in body
    assert '"tools": TOOLS' in body
    mod = _load()
    fn = mod.TOOLS[0]["function"]
    assert mod.TOOLS[0]["type"] == "function" and fn["name"] and fn["parameters"]


def test_status_200_alone_is_not_green_text_must_come_out():
    """`success` + 零输出是空壳；判「能用」必须要求出字。"""
    assert "got = bool(txt.strip())" in SRC
    assert "if st != 200 or not got:" in SRC


def test_negative_control_is_present_and_voids_the_run_if_it_greens():
    assert "sa-does-not-exist-" in SRC
    assert "RULER-BROKEN" in SRC
    assert "return 3" in SRC


def test_empty_models_shape_is_refused_not_silently_probed():
    """空 models 在 LiteLLM 里意思是**全放行** ⇒ 拿它当探针等于没有白名单闸门。"""
    assert "if not models:" in SRC
    head = SRC.split("if not models:", 1)[1].split("return 2", 1)[0]
    assert "全放行" in head or "空" in head


def test_probe_never_uses_master_key_for_the_model_calls():
    for line in SRC.splitlines():
        s = line.strip()
        if "/v1/chat/completions" in s:
            assert "master" not in s, s
    assert '"/key/generate"' in SRC
    assert '"/key/delete"' in SRC


def test_temp_key_is_deleted_in_finally():
    assert "finally:" in SRC
    assert "/key/delete" in SRC.split("finally:", 1)[1]


def test_nonce_is_unique_per_run_and_embedded_in_every_request():
    assert "random.randint" in SRC, "响应缓存是开的，固定 nonce 打不到上游"
    body = SRC.split("for n in NAMES:", 1)[1].split("st, body, _ = call", 1)[0]
    assert "nonce" in body
    # 阴性对照的假名也要带 nonce，否则第二次跑会命中上一轮的缓存
    assert '"sa-does-not-exist-" + nonce' in SRC


def test_sse_text_joins_deltas_and_survives_junk_lines():
    mod = _load()
    body = "\n".join([
        ": keep-alive",
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"content":"O"}}]}',
        "data: {not json",
        'data: {"choices":[{"delta":{"content":"K"}}]}',
        "data: [DONE]",
    ])
    assert mod.sse_text(body) == "OK"


def test_sse_text_returns_empty_for_a_stream_with_no_content():
    """裸流式没有 usage 是协议规定；但**一个字都没出**必须读成空 ⇒ 判红。"""
    mod = _load()
    assert mod.sse_text('data: {"choices":[{"delta":{}}]}\ndata: [DONE]') == ""
    assert mod.sse_text("") == ""


def test_no_hardcoded_credentials():
    """`os.environ.get("K", "<真key>")` 式兜底默认值等于已提交的凭据。"""
    assert 'os.environ.get("LITELLM_MASTER_KEY")' in SRC
    assert 'environ.get("LITELLM_MASTER_KEY", "' not in SRC
    assert not re.search(r'sk-[A-Za-z0-9_-]{8,}', SRC)


def test_transport_errors_are_visible_not_swallowed():
    """应急 helper 禁 2>/dev/null。只查**代码行**：注释里那句纪律不是违例。"""
    code = "\n".join(ln.split("#", 1)[0] for ln in SRC.splitlines())
    assert "2>/dev/null" not in code
    assert "return -1" in SRC, "非 HTTPError 的异常也要有独立读数"
