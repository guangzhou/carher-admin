"""pytest 收集钩子:本目录和 scripts/ai-portal/ 各有一个 test_server.py。

默认 prepend 导入模式下两个文件都叫顶层模块 `test_server` ⇒ 一起跑
(`pytest scripts/ai-portal ... scripts/account-store`)时后收集的那个报 import file mismatch。
这里在**收集本目录的模块之前**把别处导入的同名模块(`test_server` / `server`)从 sys.modules 摘掉
(已收集的测试持有自己的模块对象,不受影响),本目录的文件就按自己的路径导入。
不改文件名 ⇒ 不和正在改本目录的同事撞。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _drop_foreign(name):
    m = sys.modules.get(name)
    if m is not None and Path(getattr(m, "__file__", "") or "").resolve().parent != _HERE:
        sys.modules.pop(name, None)


def pytest_collectstart(collector):
    p = getattr(collector, "path", None)
    if p is not None and Path(p).resolve().parent == _HERE and Path(p).name.startswith("test_"):
        _drop_foreign("test_server")
        _drop_foreign("server")
