"""picker：账号选择算法。

抄 thibautrey/multicodex-proxy README + Soju06/codex-lb 选址逻辑。
所有"why-selected"输出必须可解释，进 Prometheus picker_selection_total。

输入：iter[AccountStatus]
输出：(选中 AccountStatus or None, reason str)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from .state import AccountStatus, is_routable


@dataclass
class PickResult:
    account: AccountStatus | None
    reason: str  # 给 metric + accountctl why-selected --json 用
    # 比较时落选账号原因（debug 用）
    rejected: dict[str, str] | None = None


def pick(accounts: Iterable[AccountStatus], now: float | None = None) -> PickResult:
    """
    选择优先级（低→高）：
      1. routable（HEALTHY + cooldown 已过 + 双 window < 100）
      2. priority 数值越小越优先
      3. primary_used_pct 越低越优先（5h window 双窗口更"凉"）
      4. secondary_used_pct 越低越优先（7d window）
      5. last_used_at 越早越优先（LRU round-robin, 同 tier 平铺流量）
      6. last_probe_at 越新越优先（数据更可信）
    """
    now = now if now is not None else time.time()
    candidates: list[AccountStatus] = []
    rejected: dict[str, str] = {}
    for acct in accounts:
        if not is_routable(acct, now):
            if acct.state.value != "healthy":
                rejected[acct.name] = f"state={acct.state.value}"
            elif acct.primary_used_pct >= 100:
                rejected[acct.name] = "primary_window>=100"
            elif acct.secondary_used_pct >= 100:
                rejected[acct.name] = "secondary_window>=100"
            elif acct.cooldown_until and now < acct.cooldown_until:
                rejected[acct.name] = f"cooldown_until={acct.cooldown_until:.0f}"
            else:
                rejected[acct.name] = "not_routable"
            continue
        candidates.append(acct)

    if not candidates:
        return PickResult(account=None, reason="no_routable_account", rejected=rejected)

    # 用 bucket 离散化 used_pct, 避免浮点小数把同 tier 切开
    def bucket(p: float) -> int:
        return int(p // 5)  # 5% 一档

    candidates.sort(key=lambda a: (
        a.priority,
        bucket(a.primary_used_pct),
        bucket(a.secondary_used_pct),
        a.last_used_at,            # LRU: 没用过的(0)最优先, 然后越早越优先
        -a.last_probe_at,
        a.name,                    # 最后一道 deterministic tiebreaker
    ))
    chosen = candidates[0]
    chosen.last_used_at = now      # 落到尾部, 下次让别人来
    reason = (
        f"priority={chosen.priority},"
        f"primary={chosen.primary_used_pct:.1f}%,"
        f"secondary={chosen.secondary_used_pct:.1f}%"
    )
    return PickResult(account=chosen, reason=reason, rejected=rejected)
