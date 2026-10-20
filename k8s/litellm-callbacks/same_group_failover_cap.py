"""
同组换号上限（198 pro，litellm 1.105.1+）。

为什么要有
----------
1.105.1 下线了 `midstream_fallback_loop`（上游自带多跳中流兜底），那个补丁里的
`_MAX_SAME_GROUP_RETRIES = 3` 也一起没了。上游的 `_maybe_run_weighted_failover`
只要同组还有没冷却的腿就继续换号，而每换一次都走 `run_async_fallback`，
`fallback_depth + 1`，和跨组兜底**共用** `max_fallbacks`（默认 5）。
16 腿的 chatgpt 组一次过载就把 5 次全花在同组换号上，跨组那一跳在
`run_async_fallback` 入口 `fallback_depth >= max_fallbacks` 直接放弃。
2026-10-20 实测 60min：同组 hop 的排除表长度 1..5 分布 192/122/94/76/54、从无 >5，
最终放弃 191 次，跨组到 grok 仅 9 次。

做法
----
包一层 `Router._maybe_run_weighted_failover`：本请求已排除（=已失败换掉）的腿
达到 `_MAX_SAME_GROUP` 个时返回 None —— 这正是上游「不适用」的返回值，
`async_function_with_fallbacks_common_utils` 随即走 fallbacks 链。
排除表是上游自己维护的 `meta["_failover_excluded_ids"]`，本补丁只读不写。
其余情况原样调原函数，逐字节不变。

配合 env `ROUTER_MAX_FALLBACKS=10`：3 次同组 + 最多 7 跳链（DB 最长链 7）。

开关：env `SAME_GROUP_FAILOVER_CAP`（默认 3），`SAME_GROUP_FAILOVER_CAP_DISABLED=1` 不装。
"""

import os

from litellm.integrations.custom_logger import CustomLogger

try:
    from litellm._logging import verbose_router_logger
except Exception:  # pragma: no cover
    import logging

    verbose_router_logger = logging.getLogger("litellm.router")


def _read_cap() -> int:
    try:
        return max(1, int(os.environ.get("SAME_GROUP_FAILOVER_CAP", "3")))
    except ValueError:
        return 3


_MAX_SAME_GROUP = _read_cap()


def _install_same_group_failover_cap() -> None:
    if os.environ.get("SAME_GROUP_FAILOVER_CAP_DISABLED") == "1":
        verbose_router_logger.warning("same_group_failover_cap: disabled by env, skip")
        return

    from litellm.router import Router

    if getattr(Router, "_carher_same_group_cap_patched", False):
        return

    # 硬拒装：方法不在 = 上游换了机制，包它没有意义。
    _orig = getattr(Router, "_maybe_run_weighted_failover", None)
    if _orig is None:
        verbose_router_logger.error(
            "same_group_failover_cap: install failed — Router._maybe_run_weighted_failover missing"
        )
        return

    # 软守卫：读不到源码 / 文本对不上只告警（量具坏 ≠ 上游变，见
    # feedback_guard_must_warn_not_refuse_when_its_ruler_breaks）。
    try:
        import inspect

        _src = inspect.getsource(_orig)
        if '"_failover_excluded_ids"' not in _src:
            verbose_router_logger.warning(
                "same_group_failover_cap: upstream no longer shows _failover_excluded_ids — "
                "cap may never trip; re-diff router.py"
            )
    except Exception as exc:
        verbose_router_logger.warning(
            "same_group_failover_cap: cannot read source (%r), installing unverified", exc
        )

    async def _capped(self, exception, original_model_group, all_deployments, args, kwargs, input_kwargs):
        try:
            meta = kwargs.get(self._get_metadata_variable_name_from_kwargs(kwargs))
            prev = meta.get("_failover_excluded_ids") if isinstance(meta, dict) else None
            n_prev = len(prev) if isinstance(prev, (list, tuple, set)) else 0
        except Exception:
            n_prev = 0  # 判不出来就不拦，保持上游行为
        if n_prev >= _MAX_SAME_GROUP:
            verbose_router_logger.info(
                "same_group_failover_cap: %d same-group failovers used for model_group=%s, "
                "handing over to fallbacks chain (depth=%s/%s) trace=%s",
                n_prev,
                original_model_group,
                kwargs.get("fallback_depth"),
                input_kwargs.get("max_fallbacks"),
                kwargs.get("litellm_trace_id") or "-",
            )
            return None
        return await _orig(self, exception, original_model_group, all_deployments, args, kwargs, input_kwargs)

    Router._maybe_run_weighted_failover = _capped
    Router._carher_same_group_cap_patched = True
    verbose_router_logger.info(
        "same_group_failover_cap: installed (cap=%d same-group failovers per request)", _MAX_SAME_GROUP
    )


try:
    _install_same_group_failover_cap()
except Exception as _exc:  # pragma: no cover
    # 装不上保持上游行为，绝不能拖垮 proxy 启动。
    verbose_router_logger.error("same_group_failover_cap: install failed, skipped: %r", _exc)


class SameGroupFailoverCap(CustomLogger):
    """空 callback 壳：litellm_settings.callbacks 挂载入口，逻辑全在 import 副作用。"""


same_group_failover_cap = SameGroupFailoverCap()
