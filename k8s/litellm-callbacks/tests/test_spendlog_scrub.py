"""test_spendlog_scrub.py — SpendLogs 行清洗（null_byte_sanitize.py + sitecustomize.py 两层）。

钉住 2026-10-20 / 10-25 的丢行根因不再出现：对话里有**字面** ``\\u0000`` /
``r'[\\ud800-\\udfff]'`` 时，JSON 列里是 ``\\\\u0000``；旧清洗按文本删掉后 6 个字符，
剩一个孤立的 ``\\`` ⇒ 非法转义 ⇒ prisma DataError ⇒ 这一行（以及这个会话之后每一轮）丢掉。

同时钉住原本要防的东西不能丢：真 NUL / ``\\u0000`` 转义 / 孤立代理对（码点和转义两种形态）
仍被删掉，成对代理（emoji）保留；两层行为逐字节一致；幂等；合法 JSON 进、合法 JSON 出。

两层按生产叠法装在同一个桩 ``PrismaClient`` 上（先 sitecustomize，再 null_byte_sanitize），
底下是 1.105.1 原版 ``jsonify_object``。

用法::

    cd k8s/litellm-callbacks/tests
    .venv/bin/python -m unittest test_spendlog_scrub -v
"""
import copy
import importlib.util
import io
import json
import os
import random
import re
import sys
import types
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _upstream_jsonify_object(self, data):
    """litellm v1.105.1 proxy/utils.py:4623，逐字照抄。"""
    db_data = copy.deepcopy(dict(data))
    for k, v in db_data.items():
        if isinstance(v, dict):
            try:
                db_data[k] = json.dumps(v)
            except Exception:
                db_data[k] = "failed-to-serialize-json"
    return db_data


def _install_stubs():
    class PrismaClient:
        jsonify_object = _upstream_jsonify_object

    litellm = types.ModuleType("litellm")
    proxy = types.ModuleType("litellm.proxy")
    utils = types.ModuleType("litellm.proxy.utils")
    utils.PrismaClient = PrismaClient
    utils.jsonify_object = lambda data: _upstream_jsonify_object(None, data)
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = type("CustomLogger", (), {})
    core = types.ModuleType("litellm.litellm_core_utils")
    sjd = types.ModuleType("litellm.litellm_core_utils.safe_json_dumps")
    sjd.safe_dumps = lambda data, max_depth=10: json.dumps(data, default=str)
    litellm.proxy = proxy
    proxy.utils = utils
    for name, mod in {
        "litellm": litellm,
        "litellm.proxy": proxy,
        "litellm.proxy.utils": utils,
        "litellm.integrations": integrations,
        "litellm.integrations.custom_logger": custom_logger,
        "litellm.litellm_core_utils": core,
        "litellm.litellm_core_utils.safe_json_dumps": sjd,
    }.items():
        sys.modules[name] = mod
    return PrismaClient


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    with redirect_stderr(io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


PrismaClient = _install_stubs()
_saved_path = list(sys.path)
SITE = _load("sitecustomize_under_test", "sitecustomize.py")  # 生产里它先装
sys.path[:] = _saved_path
NBS = _load("null_byte_sanitize_under_test", "null_byte_sanitize.py")


def _site_scrub_str():
    """sitecustomize 的 _scrub_str 是闭包，从它包出来的 patched_jsonify_object 里取。"""
    fn = PrismaClient.jsonify_object  # null_byte_sanitize 那层
    inner = fn.__closure__
    site_layer = next(c.cell_contents for c in inner
                      if callable(c.cell_contents)
                      and getattr(c.cell_contents, "_spendlog_sanitize_patched", False))
    cells = {n: c.cell_contents for n, c in
             zip(site_layer.__code__.co_freevars, site_layer.__closure__)}
    scrub = cells["_scrub"]
    cells2 = {n: c.cell_contents for n, c in
              zip(scrub.__code__.co_freevars, scrub.__closure__)}
    return cells2["_scrub_str"]


SITE_SCRUB_STR = _site_scrub_str()
NBS_SCRUB_STR = NBS._scrub_str


# ── 修之前的实现，逐字照抄，只当阳性对照用 ────────────────────────────
_OLD_H = re.compile(r"\\u[dD][89aAbB][0-9a-fA-F]{2}(?!\\u[dD][c-fC-F][0-9a-fA-F]{2})")
_OLD_L = re.compile(r"(?<!\\u[dD][89aAbB][0-9a-fA-F]{2})\\u[dD][c-fC-F][0-9a-fA-F]{2}")


def _old_scrub_str(s):
    if "\x00" in s or "\\u0000" in s:
        s = s.replace("\x00", "").replace("\\u0000", "")
    s = re.sub(r"[\ud800-\udfff]", "", s)
    if "\\u" in s:
        s = _OLD_L.sub("", _OLD_H.sub("", s))
    return s


def _expected(value, paired_ok=True):
    """正确的清洗在**解析后的值**上应得到什么：删 NUL、删孤立代理，保留成对的。"""
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\x00":
            i += 1
            continue
        if (paired_ok and "\ud800" <= c <= "\udbff" and i + 1 < len(value)
                and "\udc00" <= value[i + 1] <= "\udfff"):
            out.append(chr(0x10000 + ((ord(c) - 0xD800) << 10) + (ord(value[i + 1]) - 0xDC00)))
            i += 2
            continue
        if "\ud800" <= c <= "\udfff":
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _row(messages_value):
    """生产形状：messages/response/proxy_server_request/metadata 进 jsonify_object 前已是 JSON 文本。"""
    body = json.dumps({"messages": [{"role": "user", "content": messages_value}]})
    return {
        "request_id": "req-1",
        "spend": 1.5,
        "messages": body,
        "response": json.dumps({"content": messages_value}),
        "proxy_server_request": body,
        "metadata": json.dumps({"user_api_key_alias": "k"}),
        "request_tags": "[]",
    }


# 本会话 10-20 首次丢行时的真实片段形状（代码里的字面转义）
CODE_SNIPPET = (
    "_NUL_ESCAPED = \"\\u0000\"\n"
    "_LONE_SURROGATE_RE = re.compile(r\"[\\ud800-\\udfff]\")\n"
    "path = C:\\\\users\\\\u0000x  emoji 😀 中文"
)


class ProductionRegression(unittest.TestCase):
    def _through_both_layers(self, row):
        return PrismaClient.jsonify_object(object(), row)

    def test_literal_backslash_u_survives_and_stays_valid_json(self):
        out = self._through_both_layers(_row(CODE_SNIPPET))
        for col in ("messages", "response", "proxy_server_request", "metadata"):
            json.loads(out[col])  # 旧实现在这里 Invalid \escape
        got = json.loads(out["messages"])["messages"][0]["content"]
        self.assertEqual(got, CODE_SNIPPET)  # 用户写的字面文本一字不丢

    def test_positive_control_old_scrub_breaks_the_same_row(self):
        col = _row(CODE_SNIPPET)["messages"]
        with self.assertRaises(json.JSONDecodeError):
            json.loads(_old_scrub_str(col))

    def test_non_text_fields_untouched(self):
        row = _row("hello")
        out = self._through_both_layers(row)
        self.assertEqual(out["spend"], 1.5)
        self.assertEqual(out["request_id"], "req-1")
        self.assertEqual(out["messages"], row["messages"])

    def test_dict_column_still_serialized_by_upstream(self):
        out = self._through_both_layers({"request_id": "r", "metadata": {"a": "x\x00y\ud83d"}})
        self.assertIsInstance(out["metadata"], str)
        self.assertEqual(json.loads(out["metadata"]), {"a": "xy"})


class StillStripsWhatItMustStrip(unittest.TestCase):
    def assertScrub(self, value, ensure_ascii=True):
        text = json.dumps({"c": value}, ensure_ascii=ensure_ascii)
        for name, f in (("null_byte_sanitize", NBS_SCRUB_STR), ("sitecustomize", SITE_SCRUB_STR)):
            r = f(text)
            got = json.loads(r)["c"]
            self.assertEqual(got, _expected(value, paired_ok=ensure_ascii), (name, value))
            got.encode("utf-8")  # 不再含孤立代理
            self.assertNotIn("\x00", r)

    def test_nul_byte(self):
        self.assertScrub("a\x00b")
        self.assertScrub("a\x00b", ensure_ascii=False)

    def test_lone_high_and_low_surrogate(self):
        self.assertScrub("x\ud83dy")
        self.assertScrub("x\ude00y")
        self.assertScrub("x\ud83dy", ensure_ascii=False)

    def test_paired_surrogate_escape_kept(self):
        text = json.dumps({"c": "😀"})
        self.assertIn("\\ud83d\\ude00", text)
        self.assertEqual(NBS_SCRUB_STR(text), text)
        self.assertEqual(SITE_SCRUB_STR(text), text)

    def test_raw_values_before_dumps(self):
        for f in (NBS_SCRUB_STR, SITE_SCRUB_STR):
            self.assertEqual(f("a\x00b\ud83dc😀"), "abc😀")

    def test_clean_string_is_same_object(self):
        s = json.dumps({"c": "中文 \\n plain"})
        self.assertIs(NBS_SCRUB_STR(s), s)
        self.assertIs(SITE_SCRUB_STR(s), s)


class Fuzz(unittest.TestCase):
    ALPHA = ["\\", "u", "0", "d", "8", "3", "c", "e", "\x00", "\ud83d", "\ude00",
             "a", '"', "中", "😀", "\n"]

    def test_valid_json_in_valid_json_out_and_layers_agree(self):
        rnd = random.Random(20261025)
        for _ in range(40000):
            v = "".join(rnd.choice(self.ALPHA) for _ in range(rnd.randint(0, 14)))
            for ea in (True, False):
                text = json.dumps({"c": v}, ensure_ascii=ea)
                a, b = NBS_SCRUB_STR(text), SITE_SCRUB_STR(text)
                self.assertEqual(a, b, (v, text))
                self.assertEqual(json.loads(a)["c"], _expected(v, paired_ok=ea), (v, text, a))
                self.assertEqual(NBS_SCRUB_STR(a), a)  # 幂等

    def test_layers_agree_on_arbitrary_plain_strings(self):
        rnd = random.Random(7)
        for _ in range(20000):
            v = "".join(rnd.choice(self.ALPHA) for _ in range(rnd.randint(0, 14)))
            self.assertEqual(NBS_SCRUB_STR(v), SITE_SCRUB_STR(v), v)


if __name__ == "__main__":
    unittest.main()
