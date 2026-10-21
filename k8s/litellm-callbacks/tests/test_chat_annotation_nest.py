"""test_chat_annotation_nest.py — chat 回包平铺 url_citation 补嵌套形
（杭州 gw litellm 1.101.0 pydantic 校验 ``annotations[i].url_citation`` 必须是 dict）。

用法::

    python3 -m unittest test_chat_annotation_nest -v
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# 10-21 从杭州机直打 198 /pro/v1 抓到的真实平铺形（67 条中的一条）
FLAT = {"type": "url_citation", "end_index": 0, "start_index": 0,
        "title": "https://finance.sina.com.cn/jjxw/2026-09-23/doc-inisuxhi1306701.shtml",
        "url": "https://finance.sina.com.cn/jjxw/2026-09-23/doc-inisuxhi1306701.shtml"}


def _stub_litellm():
    if "litellm.integrations.custom_logger" in sys.modules:
        return
    litellm = sys.modules.get("litellm") or types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:  # noqa: D401 - stub
        pass

    custom_logger.CustomLogger = CustomLogger
    integrations.custom_logger = custom_logger
    litellm.integrations = integrations
    sys.modules["litellm"] = litellm
    sys.modules["litellm.integrations"] = integrations
    sys.modules["litellm.integrations.custom_logger"] = custom_logger


def _load():
    _stub_litellm()
    path = os.path.join(HERE, "..", "chat_annotation_nest.py")
    spec = importlib.util.spec_from_file_location("chat_annotation_nest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


class _Obj:
    """模拟 litellm 的 pydantic Message/Choices/ModelResponse（属性访问）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _resp_dict(anns):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "x", "annotations": anns}}]}


class NestAnnotation(unittest.TestCase):
    def test_flat_gets_nested_copy_and_keeps_flat_fields(self):
        out = M.nest_annotation(dict(FLAT))
        self.assertEqual(out["url_citation"], {k: FLAT[k] for k in ("url", "title", "start_index", "end_index")})
        for k, v in FLAT.items():          # 加法：平铺字段原样保留
            self.assertEqual(out[k], v)

    def test_already_nested_is_untouched(self):
        a = {"type": "url_citation", "url_citation": {"url": "https://a", "title": "t"}}
        self.assertIs(M.nest_annotation(a), a)

    def test_other_type_untouched(self):
        a = {"type": "file_citation", "file_id": "f"}
        self.assertIs(M.nest_annotation(a), a)

    def test_flat_without_url_untouched(self):
        a = {"type": "url_citation", "title": "no url"}
        self.assertIs(M.nest_annotation(a), a)

    def test_none_fields_dropped_from_nested(self):
        out = M.nest_annotation({"type": "url_citation", "url": "https://a", "title": None})
        self.assertEqual(out["url_citation"], {"url": "https://a"})


class NestResponse(unittest.TestCase):
    def test_dict_response(self):
        r = _resp_dict([dict(FLAT), dict(FLAT)])
        self.assertEqual(M.nest_response(r), 2)
        self.assertIsInstance(r["choices"][0]["message"]["annotations"][1]["url_citation"], dict)

    def test_object_response(self):
        msg = _Obj(role="assistant", content="x", annotations=[dict(FLAT)])
        r = _Obj(choices=[_Obj(index=0, message=msg)])
        self.assertEqual(M.nest_response(r), 1)
        self.assertEqual(msg.annotations[0]["url_citation"]["url"], FLAT["url"])

    def test_idempotent(self):
        r = _resp_dict([dict(FLAT)])
        M.nest_response(r)
        self.assertEqual(M.nest_response(r), 0)

    def test_no_annotations_and_non_chat_shapes(self):
        self.assertEqual(M.nest_response(_resp_dict(None)), 0)
        self.assertEqual(M.nest_response({"choices": [{"delta": {}}]}), 0)    # 流式 chunk
        self.assertEqual(M.nest_response({"output": []}), 0)                  # Responses 形
        self.assertEqual(M.nest_response(None), 0)


class Hook(unittest.TestCase):
    def test_hook_returns_same_response_and_fixes_it(self):
        r = _resp_dict([dict(FLAT)])
        out = asyncio.run(M.chat_annotation_nest.async_post_call_success_hook({"model": "m"}, None, r))
        self.assertIs(out, r)
        self.assertIn("url_citation", r["choices"][0]["message"]["annotations"][0])

    def test_hook_swallows_errors(self):
        class Boom:
            @property
            def choices(self):
                raise RuntimeError("boom")
        b = Boom()
        out = asyncio.run(M.chat_annotation_nest.async_post_call_success_hook({}, None, b))
        self.assertIs(out, b)


if __name__ == "__main__":
    unittest.main()
