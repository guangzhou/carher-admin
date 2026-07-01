"""probe wham/usage 状态机测试 (T6)。

mock returns:
  primary=80, secondary=10, allowed=True   -> HEALTHY (no-op or recover)
  primary=100, secondary=10, allowed=True  -> COOLING
  primary=10, secondary=10, allowed=False  -> OFFLINE
  HTTP 401 x N                              -> TOKEN_INVALIDATED
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.auth import AuthBundle, atomic_write_auth
from app.config import CONSECUTIVE_401_THRESHOLD
from app.probe import probe_once
from app.refresh import _BUNDLE_CACHE
from app.registry import Registry
from app.state import AccountState


class _FakeResponse:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _FakeSession:
    def __init__(self, response):
        self.response = response

    async def get(self, url, headers=None, timeout=None):
        return self.response

    async def close(self):
        pass


def _factory(response):
    @asynccontextmanager
    async def _f():
        s = _FakeSession(response)
        try:
            yield s
        finally:
            await s.close()
    return _f


@pytest.fixture(autouse=True)
def _clear_cache():
    _BUNDLE_CACHE.clear()
    yield
    _BUNDLE_CACHE.clear()


def _seed(tmp_path: Path) -> Registry:
    reg = Registry(tmp_path / "r.db")
    auth = tmp_path / "a.json"
    atomic_write_auth(auth, AuthBundle(
        access_token="AT", refresh_token="RT",
        account_id="acc-1",
    ))
    reg.add("acct-1", auth_path=str(auth))
    return reg


async def test_probe_healthy_stays_healthy(tmp_path):
    reg = _seed(tmp_path)
    body = {"rate_limit": {
        "primary_window": {"used_percent": 30, "reset_at": 100},
        "secondary_window": {"used_percent": 5, "reset_at": 200},
    }, "allowed": True}
    await probe_once(reg, "acct-1", _factory(_FakeResponse(200, body)))
    s = reg.get("acct-1")
    assert s.state is AccountState.HEALTHY
    assert s.primary_used_pct == 30
    assert s.secondary_used_pct == 5


async def test_probe_primary_100_moves_to_cooling(tmp_path):
    reg = _seed(tmp_path)
    body = {"rate_limit": {
        "primary_window": {"used_percent": 100},
        "secondary_window": {"used_percent": 10},
    }, "allowed": True}
    await probe_once(reg, "acct-1", _factory(_FakeResponse(200, body)))
    s = reg.get("acct-1")
    assert s.state is AccountState.COOLING
    assert "primary>=100" in s.last_state_reason


async def test_probe_allowed_false_moves_to_offline(tmp_path):
    """[[chatgpt_dead_end_judge_by_allowed_not_pct]]: allowed=False 才真停服."""
    reg = _seed(tmp_path)
    body = {"rate_limit": {
        "primary_window": {"used_percent": 10},
        "secondary_window": {"used_percent": 10},
    }, "allowed": False}
    await probe_once(reg, "acct-1", _factory(_FakeResponse(200, body)))
    assert reg.get("acct-1").state is AccountState.OFFLINE


async def test_probe_cooling_recovers_to_healthy(tmp_path):
    reg = _seed(tmp_path)
    s = reg.get("acct-1")
    from app.state import transition
    transition(s, AccountState.COOLING, "test")
    body = {"rate_limit": {
        "primary_window": {"used_percent": 50},
        "secondary_window": {"used_percent": 5},
    }, "allowed": True}
    await probe_once(reg, "acct-1", _factory(_FakeResponse(200, body)))
    assert reg.get("acct-1").state is AccountState.HEALTHY


async def test_probe_consecutive_401_marks_token_invalidated(tmp_path):
    reg = _seed(tmp_path)
    for _ in range(CONSECUTIVE_401_THRESHOLD):
        await probe_once(reg, "acct-1", _factory(_FakeResponse(401, None)))
    assert reg.get("acct-1").state is AccountState.TOKEN_INVALIDATED


async def test_probe_single_401_doesnt_invalidate(tmp_path):
    reg = _seed(tmp_path)
    await probe_once(reg, "acct-1", _factory(_FakeResponse(401, None)))
    assert reg.get("acct-1").state is AccountState.HEALTHY  # only 1 < threshold
    assert reg.get("acct-1").consecutive_401 == 1


async def test_probe_resets_401_count_on_success(tmp_path):
    reg = _seed(tmp_path)
    reg.get("acct-1").consecutive_401 = 2
    body = {"rate_limit": {
        "primary_window": {"used_percent": 30},
        "secondary_window": {"used_percent": 5},
    }, "allowed": True}
    await probe_once(reg, "acct-1", _factory(_FakeResponse(200, body)))
    assert reg.get("acct-1").consecutive_401 == 0


async def test_probe_missing_account_id_skips(tmp_path):
    reg = Registry(tmp_path / "r.db")
    auth = tmp_path / "a.json"
    atomic_write_auth(auth, AuthBundle(
        access_token="AT", refresh_token="RT", account_id=None,
    ))
    reg.add("acct-1", auth_path=str(auth))
    body = {"rate_limit": {"primary_window": {"used_percent": 99}}, "allowed": True}
    await probe_once(reg, "acct-1", _factory(_FakeResponse(200, body)))
    # 没 account_id 直接跳, primary_used_pct 不应被更新
    assert reg.get("acct-1").primary_used_pct == 0
