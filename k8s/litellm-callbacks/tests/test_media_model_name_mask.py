"""media_model_name_mask 单测。每项都带**阳性对照**：先证明不打补丁时是漏的。

跑法（pod 内或本机只要能 import litellm）：
    python3 -m pytest k8s/litellm-callbacks/tests/test_media_model_name_mask.py -q
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _Resp:
    """最小回包对象：带 model 属性，像 LiteLLM 的 VideoResponse。"""

    def __init__(self, model):
        self.model = model


def _hook():
    import media_model_name_mask as m
    return m, m.proxy_handler_instance


def _run(inst, data, resp):
    return asyncio.get_event_loop().run_until_complete(
        inst.async_post_call_success_hook(data, None, resp))


def test_positive_control_without_patch_leaks():
    """阳性对照：不调 hook 时，回包就是 muse-video（证明这个测试测的是真问题）。"""
    r = _Resp("muse-video")
    assert r.model == "muse-video"


def test_video_submit_masked():
    _, inst = _hook()
    r = _run(inst, {"model": "gemini-video", "call_type": "avideo_generation"}, _Resp("muse-video"))
    assert r.model == "gemini-video"


def test_video_retrieve_masked():
    _, inst = _hook()
    r = _run(inst, {"model": "gemini-video", "call_type": "video_retrieve"}, _Resp("muse-video"))
    assert r.model == "gemini-video"


def test_image_masked():
    _, inst = _hook()
    r = _run(inst, {"model": "gemini-image", "call_type": "aimage_generation"}, _Resp("muse-image"))
    assert r.model == "gemini-image"


def test_dict_response_masked():
    _, inst = _hook()
    r = _run(inst, {"model": "gemini-video", "call_type": "video_retrieve"}, {"model": "muse-video", "id": "x"})
    assert r["model"] == "gemini-video" and r["id"] == "x"


def test_non_media_untouched():
    """非媒体 call_type ⇒ 逐字节不变，哪怕回包里真有 muse 字样。"""
    _, inst = _hook()
    r = _run(inst, {"model": "gpt-5.6-sol", "call_type": "acompletion"}, _Resp("muse-video"))
    assert r.model == "muse-video"


def test_already_public_untouched():
    """回包本来就是公开名 ⇒ 不动。"""
    _, inst = _hook()
    r = _run(inst, {"model": "gemini-video", "call_type": "video_retrieve"}, _Resp("web-gemini-video"))
    assert r.model == "web-gemini-video"


def test_operator_calling_muse_directly_not_masked():
    """运维自己按 muse 名调 ⇒ 照实回，不骗自己人。"""
    _, inst = _hook()
    r = _run(inst, {"model": "muse-video", "call_type": "video_retrieve"}, _Resp("muse-video"))
    assert r.model == "muse-video"


def test_kill_switch():
    os.environ["MEDIA_NAME_MASK_DISABLED"] = "1"
    try:
        _, inst = _hook()
        r = _run(inst, {"model": "gemini-video", "call_type": "video_retrieve"}, _Resp("muse-video"))
        assert r.model == "muse-video", "kill switch 必须让它完全不动"
    finally:
        del os.environ["MEDIA_NAME_MASK_DISABLED"]


def test_fail_open_on_weird_response():
    """回包是个没法设属性的对象 ⇒ 原样返回，不抛。"""
    _, inst = _hook()
    r = _run(inst, {"model": "gemini-video", "call_type": "video_retrieve"}, "not-an-object")
    assert r == "not-an-object"


def test_missing_call_type_still_masks():
    """有些端点不塞 call_type ⇒ 仍然要能遮（否则 retrieve 照漏）。"""
    _, inst = _hook()
    r = _run(inst, {"model": "gemini-video"}, _Resp("muse-video"))
    assert r.model == "gemini-video"
