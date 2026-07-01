"""共享 fixture：纯函数测试不依赖网络/curl_cffi/httpx 真发请求。"""
from __future__ import annotations

import pytest

from app.state import AccountState, AccountStatus


@pytest.fixture
def make_account():
    def _make(name="acct-1", state=AccountState.HEALTHY, priority=100,
              primary=0.0, secondary=0.0, cooldown_until=0.0,
              last_probe_at=1.0):
        return AccountStatus(
            name=name, state=state, priority=priority,
            primary_used_pct=primary, secondary_used_pct=secondary,
            cooldown_until=cooldown_until, last_probe_at=last_probe_at,
        )
    return _make
