import asyncio
import time

import pytest

from app.registry import Registry
from app.state import AccountState


def test_add_and_get(tmp_path):
    reg = Registry(tmp_path / "r.db")
    s = reg.add("acct-1", auth_path=str(tmp_path / "a1.json"), priority=50)
    assert s.name == "acct-1"
    assert s.priority == 50
    assert reg.get("acct-1") is not None
    assert reg.get("missing") is None


def test_persist_then_reload(tmp_path):
    p = tmp_path / "r.db"
    reg = Registry(p)
    s = reg.add("acct-1", auth_path=str(tmp_path / "a1.json"))
    s.state = AccountState.COOLING
    s.primary_used_pct = 75.0
    s.last_state_reason = "primary_window>=100"
    reg.persist(s, from_state=AccountState.HEALTHY)

    reg2 = Registry(p)
    loaded = reg2.get("acct-1")
    assert loaded is not None
    assert loaded.state is AccountState.COOLING
    assert loaded.primary_used_pct == 75.0


def test_per_account_lock_serializes_refreshes(tmp_path):
    reg = Registry(tmp_path / "r.db")
    reg.add("acct-1", auth_path=str(tmp_path / "a.json"))
    order: list[str] = []

    async def task(name: str):
        async with reg.lock("acct-1"):
            order.append(f"start:{name}")
            await asyncio.sleep(0.02)
            order.append(f"end:{name}")

    async def runner():
        await asyncio.gather(task("a"), task("b"))

    asyncio.run(runner())
    # 必须 a 完了 b 才进
    assert order == ["start:a", "end:a", "start:b", "end:b"] or \
           order == ["start:b", "end:b", "start:a", "end:a"]


def test_remove_clears_lock(tmp_path):
    reg = Registry(tmp_path / "r.db")
    reg.add("acct-x", auth_path="x")
    reg.lock("acct-x")
    reg.remove("acct-x")
    assert reg.get("acct-x") is None
    assert "acct-x" not in reg._locks


def test_persist_writes_state_log(tmp_path):
    reg = Registry(tmp_path / "r.db")
    s = reg.add("acct-1", auth_path=str(tmp_path / "a.json"))
    s.state = AccountState.OFFLINE
    s.last_state_reason = "7d_window"
    reg.persist(s, from_state=AccountState.HEALTHY)
    # 重读检查
    import sqlite3
    conn = sqlite3.connect(tmp_path / "r.db")
    rows = list(conn.execute("SELECT from_state, to_state, reason FROM state_log"))
    conn.close()
    assert rows == [("healthy", "offline", "7d_window")]
