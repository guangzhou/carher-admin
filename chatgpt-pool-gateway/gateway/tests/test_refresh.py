"""refresh_token 状态机 + concurrency 测试 (T5)。

不真打 auth.openai.com; 用 fake session 模拟成功/invalid_grant/network fail。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.auth import AuthBundle, atomic_write_auth
from app.refresh import (
    _BUNDLE_CACHE,
    cache_bundle,
    invalidate_bundle,
    load_bundle,
    refresh_if_needed,
)
from app.registry import Registry
from app.state import AccountState


class _FakeResponse:
    def __init__(self, status: int, body: dict | str = ""):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        return json.loads(self._body)

    @property
    def text(self):
        if isinstance(self._body, dict):
            return json.dumps(self._body)
        return self._body


class _FakeSession:
    def __init__(self, response: _FakeResponse, *, delay: float = 0, raise_exc: Exception | None = None,
                 hits: list | None = None):
        self.response = response
        self.delay = delay
        self.raise_exc = raise_exc
        self.hits = hits if hits is not None else []

    async def post(self, url, data=None, timeout=None):
        self.hits.append({"url": url, "data": data})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc:
            raise self.raise_exc
        return self.response

    async def close(self):
        pass


def _factory(*, response=None, raise_exc=None, hits=None, delay=0):
    @asynccontextmanager
    async def _f():
        sess = _FakeSession(
            response or _FakeResponse(200, {}),
            raise_exc=raise_exc, hits=hits, delay=delay,
        )
        try:
            yield sess
        finally:
            await sess.close()
    return _f


@pytest.fixture(autouse=True)
def _clear_cache():
    _BUNDLE_CACHE.clear()
    yield
    _BUNDLE_CACHE.clear()


def _seed(tmp_path: Path, name: str, *, expires_at: float = 0.0, last_refresh: float = 0.0) -> Registry:
    reg = Registry(tmp_path / "r.db")
    auth_path = tmp_path / f"{name}.json"
    atomic_write_auth(auth_path, AuthBundle(
        access_token="oldAT", refresh_token="oldRT",
        expires_at=expires_at, last_refresh_at=last_refresh,
        account_id="acc-id-1",
    ))
    reg.add(name, auth_path=str(auth_path))
    return reg


async def test_refresh_success_writes_new_token(tmp_path):
    reg = _seed(tmp_path, "acct-1", expires_at=1.0)
    hits = []
    f = _factory(
        response=_FakeResponse(200, {"access_token": "newAT", "refresh_token": "newRT", "expires_in": 3600}),
        hits=hits,
    )
    ok = await refresh_if_needed(reg, "acct-1", f, client_id="cid", force=True)
    assert ok is True
    bundle = AuthBundle.from_file(reg.auth_path("acct-1"))
    assert bundle.access_token == "newAT"
    assert bundle.refresh_token == "newRT"
    assert len(hits) == 1
    assert hits[0]["data"]["grant_type"] == "refresh_token"


async def test_refresh_invalid_grant_marks_token_invalidated(tmp_path):
    reg = _seed(tmp_path, "acct-1", expires_at=1.0)
    f = _factory(response=_FakeResponse(400, '{"error":"invalid_grant"}'))
    ok = await refresh_if_needed(reg, "acct-1", f, client_id="cid", force=True)
    assert ok is False
    assert reg.get("acct-1").state is AccountState.TOKEN_INVALIDATED


async def test_refresh_network_fail_does_not_invalidate(tmp_path):
    reg = _seed(tmp_path, "acct-1", expires_at=1.0)
    f = _factory(raise_exc=ConnectionError("dial fail"))
    ok = await refresh_if_needed(reg, "acct-1", f, client_id="cid", force=True)
    assert ok is False
    assert reg.get("acct-1").state is AccountState.HEALTHY  # 没动状态


async def test_refresh_double_check_under_lock(tmp_path):
    """T5: 并发 5 个 refresh, 实际只跑 1 次 /oauth/token。"""
    import time as _time
    now = _time.time()
    reg = _seed(tmp_path, "acct-1", expires_at=now + 10)  # < 60s 寿命
    hits = []
    f = _factory(
        response=_FakeResponse(200, {"access_token": "newAT", "refresh_token": "newRT", "expires_in": 3600}),
        delay=0.05, hits=hits,
    )
    tasks = [refresh_if_needed(reg, "acct-1", f, client_id="cid") for _ in range(5)]
    results = await asyncio.gather(*tasks)
    assert results.count(True) == 1  # 只 1 个真 refresh
    assert len(hits) == 1


async def test_refresh_not_needed_when_far_from_expiry(tmp_path):
    import time as _time
    reg = _seed(tmp_path, "acct-1", expires_at=_time.time() + 3600)
    hits = []
    f = _factory(response=_FakeResponse(200, {"access_token": "X"}), hits=hits)
    ok = await refresh_if_needed(reg, "acct-1", f, client_id="cid")
    assert ok is False
    assert hits == []


async def test_refresh_keeps_old_rt_when_idp_doesnt_rotate(tmp_path):
    reg = _seed(tmp_path, "acct-1", expires_at=1.0)
    f = _factory(response=_FakeResponse(200, {"access_token": "newAT", "expires_in": 600}))
    ok = await refresh_if_needed(reg, "acct-1", f, client_id="cid", force=True)
    assert ok is True
    b = AuthBundle.from_file(reg.auth_path("acct-1"))
    assert b.refresh_token == "oldRT"  # IdP 没轮换, 保留旧 RT
