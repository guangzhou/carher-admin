"""OAuth refresh worker。

调用方式:
  await refresh_if_needed(reg, "acct-1", session_factory)
  await force_refresh(reg, "acct-1", session_factory)

per-account Lock (来自 registry) 串行化, 进 lock 后再 check rate-limit,
避免并发多个 401 同时拿同一 RT 引发 invalid_grant 死锁。

成功路径:
  POST https://auth.openai.com/oauth/token form:
    grant_type=refresh_token, refresh_token=<old>, client_id=<env>
  → 200 {access_token, refresh_token?, expires_in}
  → atomic_write_auth + 更新 in-memory bundle cache

失败路径:
  400 invalid_grant → 标 TOKEN_INVALIDATED, 不再 retry
  429/5xx → REFRESH counter inc fail, 调用方决定 cooldown
  network error → 同上
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .auth import AuthBundle, atomic_write_auth, merge_refreshed, should_refresh
from .config import UPSTREAM_TOKEN_PATH
from .metrics import REFRESH
from .registry import Registry
from .state import AccountState, transition

log = logging.getLogger("gateway.refresh")

# 进程内 bundle 缓存: avoid 每请求都 read auth.json (磁盘 I/O + 不一致风险)
_BUNDLE_CACHE: dict[str, AuthBundle] = {}


def load_bundle(reg: Registry, name: str) -> AuthBundle:
    """读 in-memory bundle, miss 则从 auth.json 加载。"""
    b = _BUNDLE_CACHE.get(name)
    if b is None:
        b = AuthBundle.from_file(reg.auth_path(name))
        _BUNDLE_CACHE[name] = b
    return b


def cache_bundle(name: str, bundle: AuthBundle) -> None:
    _BUNDLE_CACHE[name] = bundle


def invalidate_bundle(name: str) -> None:
    _BUNDLE_CACHE.pop(name, None)


# session_factory: () -> AsyncContextManager yielding object with async .post(url, data=dict)
SessionFactory = Callable[[], Any]


async def _do_refresh(
    bundle: AuthBundle,
    session_factory: SessionFactory,
    *,
    client_id: str,
) -> tuple[bool, AuthBundle | None, str]:
    """实跑 /oauth/token, 返回 (ok, new_bundle, reason)。"""
    async with session_factory() as session:
        try:
            resp = await session.post(
                UPSTREAM_TOKEN_PATH,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": bundle.refresh_token,
                    "client_id": client_id,
                },
                timeout=30,
            )
        except Exception as e:
            return False, None, f"network:{e!r}"
    status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    body_text = ""
    try:
        body_text = resp.text if hasattr(resp, "text") and not callable(resp.text) else await resp.text()
    except Exception:
        pass
    if status != 200:
        # invalid_grant -> token invalidated
        if status == 400 and "invalid_grant" in (body_text or ""):
            return False, None, "invalid_grant"
        return False, None, f"http_{status}"
    try:
        data = resp.json() if not callable(resp.json) else resp.json()
    except Exception as e:
        return False, None, f"json:{e!r}"
    if isinstance(data, dict) and "access_token" in data:
        new_bundle = merge_refreshed(bundle, data)
        return True, new_bundle, "ok"
    return False, None, f"missing_access_token:{data!r}"


async def refresh_if_needed(
    reg: Registry,
    name: str,
    session_factory: SessionFactory,
    *,
    client_id: str,
    force: bool = False,
) -> bool:
    """串行 refresh; 进 lock 后再二次判断 (double-check)。返回是否真 refresh 了。"""
    bundle = load_bundle(reg, name)
    if not force and not should_refresh(bundle):
        return False
    async with reg.lock(name):
        bundle = load_bundle(reg, name)
        if not force and not should_refresh(bundle):
            return False
        ok, new_bundle, reason = await _do_refresh(bundle, session_factory, client_id=client_id)
        if ok and new_bundle is not None:
            try:
                atomic_write_auth(reg.auth_path(name), new_bundle)
            except OSError as e:
                log.error("atomic_write_auth failed for %s: %s", name, e)
                REFRESH.labels(result="fail").inc()
                return False
            cache_bundle(name, new_bundle)
            REFRESH.labels(result="ok").inc()
            status = reg.get(name)
            if status is not None:
                status.consecutive_401 = 0
            return True
        REFRESH.labels(result="fail").inc()
        if reason == "invalid_grant":
            status = reg.get(name)
            if status is not None:
                transition(status, AccountState.TOKEN_INVALIDATED, f"refresh:{reason}")
                reg.persist(status, from_state=AccountState.HEALTHY)
                invalidate_bundle(name)
        return False
