"""test_codex_ua_pool_route.py — 按 UA 在池子之间选路。

替代第一版的 `ua-split-*` 复制组 —— 那个做法给 acct 池拍快照，acct 号一轮换
（155~159 → 160~164）复制品跟着消失，Desktop 静默全跑去 zerokey。
本模块只改写 model 名，池子是真池子，轮换免疫。

用法::

    python3 -m unittest test_codex_ua_pool_route -v
"""
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

DESKTOP = "Codex Desktop/0.147.0-alpha.1.2 (Mac OS 26.2.0; arm64) unknown (Codex Desktop; 26.7)"
WORK_DESKTOP = "codex_work_desktop/0.146.0-alpha.9.2 (Mac OS 26.5.2; arm64) unknown"
CLI = "codex-tui/0.146.1 (Mac OS 26.2.0; arm64) unknown (codex-tui; 0.146.1)"
VSCODE = "codex_vscode/0.146.0 (Mac OS 26.2.0; arm64) unknown"
SDK = "OpenAI/Python 2.24.0"


def _stub_litellm():
    if "litellm" in sys.modules:
        return
    litellm = types.ModuleType("litellm")
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
    path = os.path.join(HERE, "..", "codex_ua_pool_route.py")
    spec = importlib.util.spec_from_file_location("uaroute", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()


KEY_ALIAS = "cursor-liuguoxian02-7w7g"


def _req(model, ua, key_alias=KEY_ALIAS):
    md = {"user_api_key_alias": key_alias}
    if ua:
        md["user_agent"] = ua
    return {"model": model, "metadata": md}


class DesktopDetection(unittest.TestCase):

    def test_both_desktop_variants(self):
        self.assertTrue(M.is_desktop(DESKTOP))
        self.assertTrue(M.is_desktop(WORK_DESKTOP))

    def test_non_desktop(self):
        for ua in (CLI, VSCODE, SDK, "curl/8.4", ""):
            self.assertFalse(M.is_desktop(ua), ua)

    def test_desktop_must_be_at_the_start(self):
        """别把 'my Codex Desktop clone/1.0' 这种误判成 Desktop。"""
        self.assertFalse(M.is_desktop("evil/1.0 (Codex Desktop/9.9)"))


class Routing(unittest.TestCase):

    def test_desktop_goes_to_acct_pool(self):
        for base in ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex"):
            out = M.route(_req(base, DESKTOP), "t")
            self.assertTrue(out["model"].startswith("chatgpt-gpt-"),
                            f"{base} -> {out['model']}")

    def test_everything_else_goes_to_zerokey(self):
        for ua in (CLI, VSCODE, SDK, ""):
            out = M.route(_req("gpt-5.6-terra", ua), "t")
            self.assertEqual(out["model"], "zerokey-pool-gpt-5.6-terra", ua)

    def test_pool_names_themselves_are_routable(self):
        """用户可能直接点名池子；Desktop 点 zerokey 也该被扳到 acct。"""
        self.assertEqual(M.route(_req("zerokey-pool-gpt-5.6-sol", DESKTOP), "t")["model"],
                         "chatgpt-gpt-5.6-sol")
        self.assertEqual(M.route(_req("chatgpt-gpt-5.6-sol", CLI), "t")["model"],
                         "zerokey-pool-gpt-5.6-sol")

    def test_already_correct_is_not_rewritten(self):
        """已经在目标池上就别写了 —— 少一条日志、少一次无意义改动。"""
        self.assertIsNone(M.pick_pool("chatgpt-gpt-5.6-sol", DESKTOP))
        self.assertIsNone(M.pick_pool("zerokey-pool-gpt-5.6-sol", CLI))

    def test_unlisted_models_untouched(self):
        """表里没有的一律不碰 —— 这是别的 key/别的模型不受影响的保证。"""
        for model in ("deepseek-v4-flash-responses", "claude-glm-5.2",
                      "anthropic.claude-opus-4-8", "glm-5.2", "image-2"):
            out = M.route(_req(model, DESKTOP), "t")
            self.assertEqual(out["model"], model, model)

    def test_no_metadata_does_not_crash(self):
        # 没 metadata -> 拿不到 key alias -> 不在白名单 -> 原样放行
        self.assertEqual(M.route({"model": "gpt-5.6-sol"}, "t")["model"], "gpt-5.6-sol")
        self.assertEqual(M.route({}, "t"), {})

    def test_litellm_metadata_variant(self):
        d = {"model": "gpt-5.6-terra",
             "litellm_metadata": {"user_agent": DESKTOP, "user_api_key_alias": KEY_ALIAS}}
        self.assertEqual(M.route(d, "t")["model"], "chatgpt-gpt-5.6-terra")


class KeyGate(unittest.TestCase):
    """没有门控的后果（2026-08-07 实测）：master key 点名 chatgpt-gpt-5.6-terra 被
    改写去 zerokey；而其它 546 把 cursor key 的 gpt 经全局 alias 后也正好是
    chatgpt-gpt-*，等于全员流量被扳走。"""

    def test_other_keys_untouched(self):
        for alias in ("cursor-zhouqifeng-d255", "claude-code-liuguoxian02-7w7g", "", None):
            out = M.route(_req("chatgpt-gpt-5.6-terra", CLI, key_alias=alias), "t")
            self.assertEqual(out["model"], "chatgpt-gpt-5.6-terra", repr(alias))

    def test_master_key_pointing_at_acct_pool_stays(self):
        out = M.route({"model": "chatgpt-gpt-5.6-terra",
                       "metadata": {"user_agent": CLI}}, "t")
        self.assertEqual(out["model"], "chatgpt-gpt-5.6-terra")

    def test_allowed_key_still_routes(self):
        self.assertEqual(M.route(_req("gpt-5.6-sol", DESKTOP), "t")["model"],
                         "chatgpt-gpt-5.6-sol")


class RotationImmunity(unittest.TestCase):
    """第一版栽在这里：复制 deployment = 给 acct 池拍快照，号一轮换就静默失效。"""

    def test_table_points_at_real_pools_not_copies(self):
        for base, (acct, zk) in M.TABLE.items():
            self.assertFalse(acct.startswith("ua-split-") or acct.startswith("uasplit-"),
                             f"{base}: acct 目标是复制品 {acct}")
            self.assertFalse(zk.startswith("ua-split-") or zk.startswith("uasplit-"),
                             f"{base}: zk 目标是复制品 {zk}")
            self.assertTrue(acct.startswith("chatgpt-gpt-"), acct)
            self.assertTrue(zk.startswith("zerokey-pool-"), zk)

    def test_no_deployment_ids_anywhere(self):
        """表里只能出现 group 名，绝不能出现具体机器 id（那才会被轮换掉）。"""
        for base, pair in M.TABLE.items():
            for name in (base,) + pair:
                self.assertNotRegex(name, r"-(acct|zk)-\d+-",
                                    f"{name} 看起来是 deployment id 而不是 group 名")


if __name__ == "__main__":
    unittest.main()
