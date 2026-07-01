"""账号状态机。

5 个状态，转移由 picker / refresh tick / 请求路径 trigger。
所有状态转移都有理由 (reason 字段) 进 Prometheus picker_selection_total。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class AccountState(str, Enum):
    HEALTHY = "healthy"
    COOLING = "cooling"                # 短期不可用（429 / 5h window 撞顶）
    OFFLINE = "offline"                # 长期不可用（7d window 撞顶 / 反复 5xx）
    TOKEN_INVALIDATED = "token_invalidated"  # 需 reauth
    DISABLED = "disabled"              # 人工 disable


VALID_TRANSITIONS = {
    AccountState.HEALTHY: {
        AccountState.COOLING,
        AccountState.OFFLINE,
        AccountState.TOKEN_INVALIDATED,
        AccountState.DISABLED,
    },
    AccountState.COOLING: {
        AccountState.HEALTHY,           # 仅通过 wham probe 自动恢复
        AccountState.OFFLINE,
        AccountState.TOKEN_INVALIDATED,
        AccountState.DISABLED,
    },
    AccountState.OFFLINE: {
        AccountState.HEALTHY,           # 仅通过 wham probe 自动恢复
        AccountState.COOLING,
        AccountState.TOKEN_INVALIDATED,
        AccountState.DISABLED,
    },
    AccountState.TOKEN_INVALIDATED: {
        # 不允许直接 -> HEALTHY；reauth 后必须再过 wham probe
        AccountState.COOLING,           # reauth 成功，等下一轮 probe
        AccountState.DISABLED,
    },
    AccountState.DISABLED: {
        AccountState.COOLING,           # 人工 enable 后等 probe
    },
}


@dataclass
class AccountStatus:
    """内存 dict + SQLite mirror，picker / health / metrics 直接读这里。"""
    name: str
    state: AccountState = AccountState.HEALTHY
    primary_used_pct: float = 0.0          # wham primary_window.used_percent
    secondary_used_pct: float = 0.0        # wham secondary_window.used_percent
    primary_reset_at: float = 0.0
    secondary_reset_at: float = 0.0
    priority: int = 100                    # picker 用：越小越优先
    last_probe_at: float = 0.0
    last_used_at: float = 0.0              # picker LRU tiebreaker，picker 选中后赋 now
    last_state_change_at: float = field(default_factory=time.time)
    last_state_reason: str = "init"
    consecutive_401: int = 0
    cooldown_until: float = 0.0


def transition(status: AccountStatus, target: AccountState, reason: str, now: float | None = None) -> bool:
    """触发状态转移。返回是否真正改变。illegal transition 拒绝并返 False。"""
    if status.state == target:
        return False
    allowed = VALID_TRANSITIONS.get(status.state, set())
    if target not in allowed:
        return False
    status.state = target
    status.last_state_change_at = now if now is not None else time.time()
    status.last_state_reason = reason
    return True


def is_routable(status: AccountStatus, now: float | None = None) -> bool:
    """picker 是否会考虑这个账号。HEALTHY + cooldown 已过 = True。"""
    if status.state is not AccountState.HEALTHY:
        return False
    now = now if now is not None else time.time()
    if status.cooldown_until and now < status.cooldown_until:
        return False
    if status.primary_used_pct >= 100 or status.secondary_used_pct >= 100:
        return False
    return True
