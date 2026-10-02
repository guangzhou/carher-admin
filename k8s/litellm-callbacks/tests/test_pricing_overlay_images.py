"""test_pricing_overlay_images.py — sub2api 出图组按张记账的回归。

钉住 2026-09-29 的 $0 记账病：价必须登记在**后端键** ``openai/<name>`` 上
（default_image_cost_calculator 只查这个），而不是 model_id；且出图登记失败
不能连累 chatgpt 改价与 ×1.3 乘子。真 LiteLLM 下的端到端（含阳性对照）在 198
pod 里跑过，见 commit message。

用法::

    cd k8s/litellm-callbacks/tests
    .venv/bin/python -m unittest test_pricing_overlay_images -v
"""
import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pricing_overlay.py")


def _fake_litellm(register_model):
    lm = types.ModuleType("litellm")
    lm.model_cost = {"gpt-5.5": {"input_cost_per_token": 1e-6, "cache_read_input_token_cost": 1e-7}}
    lm.register_model = register_model
    lm.response_cost_calculator = lambda *a, **k: 1.0
    lm.completion_cost = lambda *a, **k: 1.0
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = type("CustomLogger", (), {})
    return {"litellm": lm, "litellm.integrations": integrations,
            "litellm.integrations.custom_logger": custom_logger}


def _load(register_model):
    mods = _fake_litellm(register_model)
    with mock.patch.dict(sys.modules, mods):
        spec = importlib.util.spec_from_file_location("pricing_overlay_under_test", SRC)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    return m, mods["litellm"]


class ImagePriceTest(unittest.TestCase):
    def test_prices_land_on_backend_keys_at_official_list_price(self):
        calls = []
        _, _ = _load(calls.append)
        self.assertEqual(len(calls), 1)
        got = {k: v["input_cost_per_image"] for k, v in calls[0].items()}
        self.assertEqual(got, {
            "openai/grok-imagine": 0.05,
            "openai/grok-imagine-image": 0.02,
            "openai/grok-imagine-image-quality": 0.05,
            "openai/grok-imagine-image-2.0": 0.04,
        })
        for v in calls[0].values():
            self.assertEqual(v["mode"], "image_generation")
            self.assertEqual(v["litellm_provider"], "openai")
        # 不许登记到 model_id（sa/...）上 —— 那正是读不到的位置
        self.assertFalse(any(k.startswith("sa/") for k in calls[0]))

    def test_image_failure_does_not_break_multiplier_or_chatgpt_patch(self):
        def boom(_):
            raise RuntimeError("register down")
        _, lm = _load(boom)
        self.assertTrue(getattr(lm, "_pricing_overlay_multiplier_installed", False))
        self.assertAlmostEqual(lm.completion_cost(), 1.3)
        e = lm.model_cost["gpt-5.5"]
        self.assertEqual(e["cache_read_input_token_cost"], e["input_cost_per_token"])


if __name__ == "__main__":
    unittest.main()
