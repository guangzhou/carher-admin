"""60s 后台 wham/usage probe tick worker。

[[chatgpt-acct-probe-without-pod-via-188-tmp]]: /codex/usage 端点不严校 token freshness。
[[chatgpt-usage-main-vs-addl-rate-limits]]: 看 rate_limit.{primary,secondary}_window.used_percent。

绝不在请求路径里探 wham/usage; 全部走这里。
状态机:
  used_percent < 100 & allowed=True   → HEALTHY (若是 COOLING/OFFLINE 自动恢复)
  primary >= 100  & allowed=True      → COOLING (5h 短窗)
  secondary >= 100 & allowed=False    → OFFLINE (7d 长窗 / 真停服)
  401 连续 N 次 (CONSECUTIVE_401_THRESHOLD) → TOKEN_INVALIDATED
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .config import (
    CONSECUTIVE_401_THRESHOLD,
    PRIMARY_WINDOW_BLOCK_PCT,
    SECONDARY_WINDOW_BLOCK_PCT,
    UPSTREAM_BASE,
    UPSTREAM_WHAM_USAGE_PATH,
    WHAM_PROBE_INTERVAL_S,
    WHAM_PROBE_TIMEOUT_S,
)
from .metrics import PROBE, record_acct_gauge
from .refresh import load_bundle
from .registry import Registry
from .state import AccountState, transition

log = logging.getLogger("gateway.probe")

# 4 个 header 缺一就静默裁字段 ([[feedback_chatgpt_banked_reset_headers_verbatim]])
_WHAM_HEADERS_TPL = {
    "Authorization": "Bearer {token}",
    "ChatGPT-Account-ID": "{account_id}",
    "OpenAI-Beta": "codex-1",
    "originator": "codex_cli_rs",
}


async def probe_once(
    reg: Registry,
    name: str,
    session_factory,
    *,
    base: str = UPSTREAM_BASE,
) -> dict[str, Any] | None:
    status = reg.get(name)
    if status is None:
        return None
    try:
        bundle = load_bundle(reg, name)
    except Exception as e:
        log.warning("probe %s load_bundle failed: %s", name, e)
        PROBE.labels(result="fail").inc()
        return None

    headers = {
        "Authorization": f"Bearer {bundle.access_token}",
        "ChatGPT-Account-ID": bundle.account_id or "",
        "OpenAI-Beta": "codex-1",
        "originator": "codex_cli_rs",
    }
    if not bundle.account_id:
        log.warning("probe %s missing account_id, skip", name)
        PROBE.labels(result="fail").inc()
        return None

    from_state = status.state
    try:
        async with session_factory() as session:
            resp = await session.get(
                base.rstrip("/") + UPSTREAM_WHAM_USAGE_PATH,
                headers=headers,
                timeout=WHAM_PROBE_TIMEOUT_S,
            )
    except Exception as e:
        PROBE.labels(result="fail").inc()
        log.warning("probe %s network: %s", name, e)
        return None

    status.last_probe_at = time.time()
    http = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    if http == 401:
        status.consecutive_401 += 1
        if status.consecutive_401 >= CONSECUTIVE_401_THRESHOLD:
            transition(status, AccountState.TOKEN_INVALIDATED,
                       f"401x{status.consecutive_401}")
            reg.persist(status, from_state=from_state)
        PROBE.labels(result="fail").inc()
        record_acct_gauge(name, status.state.value, status.primary_used_pct, status.secondary_used_pct)
        return None

    if http != 200:
        PROBE.labels(result="fail").inc()
        record_acct_gauge(name, status.state.value, status.primary_used_pct, status.secondary_used_pct)
        return None

    status.consecutive_401 = 0
    try:
        data = resp.json() if not callable(resp.json) else resp.json()
    except Exception:
        try:
            body = resp.text if hasattr(resp, "text") and not callable(resp.text) else await resp.text()
            data = json.loads(body)
        except Exception:
            PROBE.labels(result="fail").inc()
            return None

    rl = (data or {}).get("rate_limit") or {}
    primary = (rl.get("primary_window") or {}).get("used_percent", 0.0)
    secondary = (rl.get("secondary_window") or {}).get("used_percent", 0.0)
    allowed = bool((data or {}).get("allowed", True))
    primary_reset = (rl.get("primary_window") or {}).get("reset_at", 0.0)
    secondary_reset = (rl.get("secondary_window") or {}).get("reset_at", 0.0)

    status.primary_used_pct = float(primary)
    status.secondary_used_pct = float(secondary)
    status.primary_reset_at = float(primary_reset or 0.0)
    status.secondary_reset_at = float(secondary_reset or 0.0)
    PROBE.labels(result="ok").inc()

    # 状态机
    if not allowed:
        transition(status, AccountState.OFFLINE, "allowed=False")
    elif primary >= PRIMARY_WINDOW_BLOCK_PCT:
        transition(status, AccountState.COOLING, "primary>=100")
    elif secondary >= SECONDARY_WINDOW_BLOCK_PCT:
        transition(status, AccountState.OFFLINE, "secondary>=100")
    else:
        # 健康 -> 回到 HEALTHY (仅 COOLING/OFFLINE 才转, TOKEN_INVALIDATED/DISABLED 不动)
        if status.state in (AccountState.COOLING, AccountState.OFFLINE):
            transition(status, AccountState.HEALTHY, "probe_ok")

    reg.persist(status, from_state=from_state)
    record_acct_gauge(name, status.state.value, status.primary_used_pct, status.secondary_used_pct)
    return data


async def probe_loop(reg: Registry, session_factory, *, interval_s: int = WHAM_PROBE_INTERVAL_S) -> None:
    """启动后无限循环, 每 interval_s 扫一遍所有 acct。"""
    while True:
        names = [s.name for s in reg.all()]
        for name in names:
            try:
                await probe_once(reg, name, session_factory)
            except Exception as e:
                log.exception("probe %s unexpected: %s", name, e)
        await asyncio.sleep(interval_s)
