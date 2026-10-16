"""v1.105.1 烘焙补丁脚本：在合成目标上验证「打上 / 重跑 SKIP / 锚点不对拒写」三种出口。

真实 1.105.1 源文件上的验证在 198 构建时做（Dockerfile 里的 grep 门禁 + md5 join），
这里只钉住脚本自身的出口语义，防止以后改脚本时把「宁可红，不许半打」改丢。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCHES = HERE.parent / "patches" / "v1.105.1"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), PATCHES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_key_alias_auth_compat_applies_once_and_refuses_unknown_shape(tmp_path):
    mod = _load("litellm-patch-key-alias-auth-compat")
    target = tmp_path / "proxy" / "auth" / "auth_checks.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n" + mod.ANCHOR + "y = 2\n", encoding="utf-8")
    assert mod.main(str(tmp_path)) == 0
    patched = target.read_text(encoding="utf-8")
    assert mod.MARKER in patched and "return None" in patched and mod.ANCHOR not in patched
    assert mod.main(str(tmp_path)) == 1  # 幂等：第二次 SKIP
    target.write_text("def key_model_aliases_for_auth_check(t):\n    return t\n", encoding="utf-8")
    assert mod.main(str(tmp_path)) == 2  # 锚点不在：不写
    assert target.read_text(encoding="utf-8") == "def key_model_aliases_for_auth_check(t):\n    return t\n"


def test_stream_alias_pricing_refuses_partial_application(tmp_path):
    mod = _load("litellm-patch-stream-alias-pricing")
    logging_py = tmp_path / "litellm_core_utils" / "litellm_logging.py"
    proxy_py = tmp_path / "proxy" / "proxy_server.py"
    logging_py.parent.mkdir(parents=True)
    proxy_py.parent.mkdir(parents=True)
    logging_src = "".join(old for _n, _w, old, _new in mod.LOGGING_EDITS)
    # proxy 侧缺锚点 ⇒ 两个文件都不许动
    logging_py.write_text(logging_src, encoding="utf-8")
    proxy_py.write_text("from typing import Final\nLiteLLMLoggingObj = object\n", encoding="utf-8")
    assert mod.main(str(tmp_path)) == 2
    assert logging_py.read_text(encoding="utf-8") == logging_src
    # 锚点齐全 ⇒ 打上，且重跑 SKIP
    proxy_src = "from typing import Final\nLiteLLMLoggingObj = object\n" + mod.PROXY_EDITS[0][2]
    proxy_py.write_text(proxy_src, encoding="utf-8")
    assert mod.main(str(tmp_path)) == 0
    assert "client_facing_stream_model" in logging_py.read_text(encoding="utf-8")
    assert "model_copy(update={\"model\": target_model})" in proxy_py.read_text(encoding="utf-8")
    assert mod.main(str(tmp_path)) == 1


def test_sse_lifecycle_v1051_keeps_note_yielded_event_semantics():
    src = (PATCHES / "litellm-patch-sse-lifecycle.py").read_text(encoding="utf-8")
    mod = _load("litellm-patch-sse-lifecycle")
    names = [name for name, _old, _new in mod.EDITS]
    assert names == ["async-loop-head", "async-loop-tail", "sync-loop-head", "sync-loop-tail", "adopt-method"]
    for name, old, new in mod.EDITS:
        if name.endswith("head"):
            # 从队列吐事件也必须走 _note_yielded_event（1.105 靠它记 _output_started 决定能否 midstream 兜底）
            assert "self._note_yielded_event(_carher_ev)" in new
        if name.endswith("tail"):
            assert "self._note_yielded_event(result)" in old and "_carher_adopt_bare_terminal" in new
    assert "if self.completed_response is not None:" in src  # 只补记，不覆盖上游自己记的终止事件


def test_dockerfile_gates_every_patch():
    dockerfile = (PATCHES / "Dockerfile").read_text(encoding="utf-8")
    for marker in ("selected model is at capacity", "_BARE_STATUS_MAP", "client_facing_stream_model",
                   "carher-patch: key aliases do not narrow auth"):
        assert marker in dockerfile
    for n in range(1, 5):
        assert f"python3 /tmp/p{n}.py" in dockerfile
    assert os.path.basename(str(PATCHES)) == "v1.105.1"
