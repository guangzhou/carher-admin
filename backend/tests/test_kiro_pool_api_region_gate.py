"""门禁：`kiro-pool.py add` 拒收非 us-east-1 的 `apiRegion`。

2026-10-16 id147：IdC 导出带 `apiRegion: eu-central-1`，add 成功、落盘正常，
但 kiro.rs 按凭据级 apiRegion 打 `q.eu-central-1`，同一 token 在那里
getUsageLimits / ListAvailableModels 全 403「User is not authorized to make this call.」
（`q.us-east-1` 同 token 两个都 200）。症状只在 Admin 看余额时出现。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(HERE, "scripts", "kiro-pool.py")


def _mod():
    spec = importlib.util.spec_from_file_location("kiro_pool", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write(tmp_path, api_region):
    src = {"refreshToken": "rt-x", "authMethod": "IdC", "clientId": "cid",
           "clientSecret": "csec", "region": "us-east-1", "authRegion": "us-east-1",
           "email": "a@example.com"}
    if api_region:
        src["apiRegion"] = api_region
    p = tmp_path / "x.json"
    p.write_text(json.dumps([src]))
    return str(p)


class _Reached(Exception):
    """闸门之后的第一步（判重）被调用 = 闸门放行了。"""


def _run(m, path, allow=False):
    def stop(*_a, **_k):
        raise _Reached()
    m.dup_check = stop
    m.cmd_add(argparse.Namespace(file=path, priority=2, allow_api_region=allow))


def test_non_us_east_1_api_region_is_rejected(tmp_path):
    m = _mod()
    with pytest.raises(SystemExit) as e:
        _run(m, _write(tmp_path, "eu-central-1"))
    assert "eu-central-1" in str(e.value.code)


@pytest.mark.parametrize("region", ["us-east-1", None])
def test_us_east_1_or_missing_passes_gate(tmp_path, region):
    """阳性对照：正常形状必须走过闸门，否则上面那条是恒红。"""
    m = _mod()
    with pytest.raises(_Reached):
        _run(m, _write(tmp_path, region))


def test_explicit_allow_passes_gate(tmp_path):
    m = _mod()
    with pytest.raises(_Reached):
        _run(m, _write(tmp_path, "eu-central-1"), allow=True)
