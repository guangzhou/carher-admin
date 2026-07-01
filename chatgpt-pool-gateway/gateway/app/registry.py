"""SQLite WAL 账号注册表。

设计：
- WAL mode（并发读 + 单写）
- 内存 dict 是 source of truth（picker / health 探针读这里，O(1)）
- SQLite 仅作冷启动 source + 状态持久化
- refresh 写 token / 状态转移都 mirror 进 SQLite，但写盘失败不阻塞请求路径
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .state import AccountState, AccountStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    name              TEXT PRIMARY KEY,
    state             TEXT NOT NULL,
    priority          INTEGER NOT NULL DEFAULT 100,
    primary_used_pct  REAL NOT NULL DEFAULT 0,
    secondary_used_pct REAL NOT NULL DEFAULT 0,
    primary_reset_at  REAL NOT NULL DEFAULT 0,
    secondary_reset_at REAL NOT NULL DEFAULT 0,
    last_probe_at     REAL NOT NULL DEFAULT 0,
    last_state_change_at REAL NOT NULL DEFAULT 0,
    last_state_reason TEXT NOT NULL DEFAULT 'init',
    consecutive_401   INTEGER NOT NULL DEFAULT 0,
    cooldown_until    REAL NOT NULL DEFAULT 0,
    -- token 文件路径，不在 SQLite 存明文 access_token / refresh_token
    auth_path         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS state_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    name TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS state_log_name_ts ON state_log(name, ts);
"""


@dataclass
class AccountRecord:
    status: AccountStatus
    auth_path: str


class Registry:
    """内存 dict + SQLite mirror。线程不安全，全部走 async loop + per-account lock。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._accounts: dict[str, AccountRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._init_schema()
        self._load()

    # ---- SQLite ----
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _load(self) -> None:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM accounts")
            cols = [c[0] for c in cur.description]
            for row in cur:
                d = dict(zip(cols, row))
                status = AccountStatus(
                    name=d["name"],
                    state=AccountState(d["state"]),
                    priority=d["priority"],
                    primary_used_pct=d["primary_used_pct"],
                    secondary_used_pct=d["secondary_used_pct"],
                    primary_reset_at=d["primary_reset_at"],
                    secondary_reset_at=d["secondary_reset_at"],
                    last_probe_at=d["last_probe_at"],
                    last_state_change_at=d["last_state_change_at"],
                    last_state_reason=d["last_state_reason"],
                    consecutive_401=d["consecutive_401"],
                    cooldown_until=d["cooldown_until"],
                )
                self._accounts[d["name"]] = AccountRecord(status=status, auth_path=d["auth_path"])

    # ---- public ----
    def add(self, name: str, *, auth_path: str, priority: int = 100) -> AccountStatus:
        if name in self._accounts:
            raise ValueError(f"account {name} already registered")
        status = AccountStatus(name=name, priority=priority)
        self._accounts[name] = AccountRecord(status=status, auth_path=auth_path)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO accounts(name, state, priority, auth_path, last_state_change_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (name, status.state.value, priority, auth_path, time.time()),
            )
        return status

    def remove(self, name: str) -> None:
        self._accounts.pop(name, None)
        self._locks.pop(name, None)
        with self._conn() as conn:
            conn.execute("DELETE FROM accounts WHERE name=?", (name,))

    def get(self, name: str) -> AccountStatus | None:
        rec = self._accounts.get(name)
        return rec.status if rec else None

    def all(self) -> Iterable[AccountStatus]:
        return [r.status for r in self._accounts.values()]

    def auth_path(self, name: str) -> str:
        return self._accounts[name].auth_path

    def lock(self, name: str) -> asyncio.Lock:
        """per-account asyncio.Lock，refresh_token rotation 临界区。"""
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    def persist(self, status: AccountStatus, *, from_state: AccountState | None = None) -> None:
        """mirror 内存 status 到 SQLite。failure 不抛——内存仍是 source of truth。"""
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    UPDATE accounts SET
                        state=?, primary_used_pct=?, secondary_used_pct=?,
                        primary_reset_at=?, secondary_reset_at=?,
                        last_probe_at=?, last_state_change_at=?,
                        last_state_reason=?, consecutive_401=?, cooldown_until=?
                    WHERE name=?
                    """,
                    (
                        status.state.value, status.primary_used_pct, status.secondary_used_pct,
                        status.primary_reset_at, status.secondary_reset_at,
                        status.last_probe_at, status.last_state_change_at,
                        status.last_state_reason, status.consecutive_401, status.cooldown_until,
                        status.name,
                    ),
                )
                if from_state is not None and from_state != status.state:
                    conn.execute(
                        "INSERT INTO state_log(ts, name, from_state, to_state, reason) VALUES(?,?,?,?,?)",
                        (time.time(), status.name, from_state.value, status.state.value, status.last_state_reason),
                    )
        except sqlite3.Error:
            # 不阻塞请求路径
            pass
