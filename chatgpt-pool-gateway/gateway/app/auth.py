"""refresh_token rotation + auth.json 原子写。

强制保证：
- per-account asyncio.Lock 串行化 refresh（openai/codex#19803: refresh_token 一次性，并发 refresh
  后输的那个让赢的也失效，引发 invalid_grant 死循环）
- 文件落盘 tmpfile + os.replace 原子替换（hermes-agent#11364: N 进程读同一 auth.json 让所有 entry
  坍缩到同 token）
- 进程内 token 缓存 >= TOKEN_REFRESH_MIN_INTERVAL_S 才考虑下一次 refresh
- writer-only-on-active：调用方传 is_leader=True 才允许真正 refresh（K8s Lease 保证 active/backup
  不抢同一 refresh_token）
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import TOKEN_REFRESH_MIN_INTERVAL_S


@dataclass
class AuthBundle:
    access_token: str
    refresh_token: str
    expires_at: float = 0.0          # epoch seconds; 0 = 未知
    last_refresh_at: float = 0.0
    account_id: str | None = None    # OpenAI 内部 account id（chatgpt-auth.json 里有）

    @classmethod
    def from_file(cls, path: str | Path) -> "AuthBundle":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        tokens = d.get("tokens") or d
        return cls(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expires_at=tokens.get("expires_at", 0.0) or 0.0,
            last_refresh_at=tokens.get("last_refresh", 0.0) or 0.0,
            account_id=d.get("account_id") or tokens.get("account_id"),
        )

    def to_dict(self) -> dict:
        return {
            "tokens": {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "last_refresh": self.last_refresh_at,
            },
            "account_id": self.account_id,
        }


def atomic_write_auth(path: str | Path, bundle: AuthBundle) -> None:
    """tmpfile + os.replace 原子写。失败抛 OSError，让调用方处理（不静默吞）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bundle.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def should_refresh(bundle: AuthBundle, now: float | None = None) -> bool:
    """rate-limit refresh 频率，避免短时间内重复换 token。"""
    now = now if now is not None else time.time()
    if bundle.last_refresh_at and now - bundle.last_refresh_at < TOKEN_REFRESH_MIN_INTERVAL_S:
        return False
    # 已知过期 / 还有 < 60s 寿命 -> 该 refresh
    if bundle.expires_at and now >= bundle.expires_at - 60:
        return True
    # 未知过期：不主动 refresh，等 401 触发
    return False


def merge_refreshed(old: AuthBundle, refreshed: dict, *, now: float | None = None) -> AuthBundle:
    """把上游 /oauth/token 返回的 dict 合并进新的 AuthBundle。"""
    now = now if now is not None else time.time()
    expires_in = refreshed.get("expires_in")
    expires_at = float(now + expires_in) if expires_in else 0.0
    return AuthBundle(
        access_token=refreshed["access_token"],
        # rotation：响应里若没新 refresh_token 就保留旧的（少数 IdP 不轮换）
        refresh_token=refreshed.get("refresh_token") or old.refresh_token,
        expires_at=expires_at,
        last_refresh_at=now,
        account_id=old.account_id,
    )
