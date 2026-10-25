"""
test_kiro_slots_identity.py — 只有发往 kiro-rs-slots 的那一跳才带 key 身份头，其它 provider 原样不动。
本机或 litellm pod 内都能跑（没装 litellm 时用桩替代 CustomLogger）：

    python3 k8s/litellm-callbacks/tests/test_kiro_slots_identity.py [path/to/kiro_slots_identity.py]
"""
import asyncio
import importlib.util
import os
import sys
import types

if "litellm" not in sys.modules:
    try:
        import litellm  # noqa: F401
    except ImportError:
        lm = types.ModuleType("litellm")
        integ = types.ModuleType("litellm.integrations")
        cl = types.ModuleType("litellm.integrations.custom_logger")

        class CustomLogger:  # 桩
            pass

        cl.CustomLogger = CustomLogger
        sys.modules.update({"litellm": lm, "litellm.integrations": integ, "litellm.integrations.custom_logger": cl})

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "kiro_slots_identity.py")
spec = importlib.util.spec_from_file_location("kiro_slots_identity", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
H = mod.kiro_slots_identity

SLOTS = "http://kiro-rs-slots.kiro-rs.svc.cluster.local:8990"
MD = {"user_api_key_hash": "h123", "user_api_key_alias": "cursor-alice"}


def run(kwargs):
    return asyncio.run(H.async_pre_call_deployment_hook(kwargs, None))


def test_adds_headers_only_for_slots_target():
    out = run({"litellm_params": {"api_base": SLOTS}, "litellm_metadata": dict(MD), "headers": {"x-keep": "1"}})
    assert out["headers"] == {"x-keep": "1", mod.HDR_HASH: "h123", mod.HDR_ALIAS: "cursor-alice"}


def test_other_providers_untouched():
    for base in (
        "http://10.43.109.5:8990",  # 老 kiro-rs
        "https://api.anthropic.com",
        "http://sa-grok.litellm-dev.svc.cluster.local:8080",
        "http://kiro-rs-slots.evil.example.com:8990",  # 只认完整 host
        None,
    ):
        kw = {"litellm_params": {"api_base": base} if base else {}, "litellm_metadata": dict(MD), "headers": {"x-keep": "1"}}
        assert run(kw) is None, base  # None = 不改任何东西


def test_no_identity_no_change():
    assert run({"litellm_params": {"api_base": SLOTS}, "litellm_metadata": {}}) is None


def test_does_not_mutate_input():
    hdr = {"x-keep": "1"}
    kw = {"litellm_params": {"api_base": SLOTS}, "metadata": dict(MD), "headers": hdr}
    out = run(kw)
    assert hdr == {"x-keep": "1"} and kw["headers"] is hdr, "原 kwargs 不被就地修改（兜底重试复用它时不会带上）"
    assert out["headers"][mod.HDR_HASH] == "h123"


def test_kill_switch():
    os.environ["KIRO_SLOTS_IDENTITY_HOSTS"] = ""
    try:
        assert run({"litellm_params": {"api_base": SLOTS}, "litellm_metadata": dict(MD)}) is None
    finally:
        del os.environ["KIRO_SLOTS_IDENTITY_HOSTS"]


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
