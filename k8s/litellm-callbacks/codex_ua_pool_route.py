"""codex_ua_pool_route.py — 按 User-Agent 在**池子之间**选路。

为什么不用 LiteLLM 原生 tag 路由（这是本模块存在的唯一理由）
--------------------------------------------------------
原生 `get_deployments_for_tag` 是在**一个 model group 内部**筛 deployment，
没法在两个 group 之间选。2026-08-07 第一版为了用上它，把 acct 池和 zerokey 池的
deployment **各复制一份**塞进同一个 `ua-split-*` 组 —— 等于给 acct 池拍了张快照。

那个快照当天就烂了：acct 池的号被轮换（155~159 → 160~164），旧 deployment 被删，
复制出来的 `uasplit-chatgpt-acct-155~159` 跟着消失，于是 Desktop 的 UA 匹配不到
任何 acct 机器 → 落到 `tags:["default"]` 的 zerokey 上。**全程零报错**，
Desktop 流量悄悄全跑去了 zerokey，是用户自己撞上来才发现的。

本模块换成"改写 model 名"：真池子还是原来那个池子，号怎么轮换都不用管，
不需要复制、不需要快照、不需要自检。

判据来源
--------
UA 由 LiteLLM 自己填在 ``metadata["user_agent"]``
（``litellm_pre_call_utils.py:1773``）。Codex 的 UA 开头就是 originator
（``codex-rs/login/src/auth/default_client.rs:159``），线上实测取值：
``Codex Desktop`` / ``codex-tui`` / ``codex_vscode`` / ``codex_exec`` /
``OpenAI`` / ``claude-cli`` …

⚠️ UA 是客户端可伪造的（官方文档明说）：**只能用于分流，不能当权限边界。**

作用范围
--------
只改写**映射表里列出的 model 名**，且只在 UA 命中 Desktop 时改写；其它一律原样
放行。改写发生在 ``async_pre_call_hook``（per-key alias 已在
``add_litellm_data_to_request`` 里应用完，所以这里看到的是 alias 之后的名字，
我们的改写在它之后、路由之前，赢）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("codex_ua_pool_route")

_RESPONSE_CALL_TYPES = {"responses", "aresponses", "completion", "acompletion"}

# 命中即走 acct 池。两种 Desktop 标识都算（用户 2026-08-07 确认）。
_DESKTOP_RE = re.compile(
    os.environ.get("UA_ROUTE_DESKTOP_RE", r"^(Codex Desktop|codex_work_desktop)/"))

# **必须按 key 白名单门控。**
# 2026-08-07 第一版没有门控，后果立刻被验收测出来：master key 明确点名
# `chatgpt-gpt-5.6-terra`（acct 池）被改写去了 zerokey。因为表里也收录了池子名，
# 而其它 546 把 cursor key 的 gpt 请求经全局 model_group_alias 之后正好都变成
# `chatgpt-gpt-*` —— 等于把全员的 gpt 流量都扳去了 zerokey。
# 所以：只对白名单里的 key 生效，其余一律原样放行。
# 两种写法，命中任一即生效：
#   UA_ROUTE_KEY_ALIASES  = 精确名单，逗号分隔（灰度单把 key 用）
#   UA_ROUTE_KEY_PREFIXES = 前缀名单，逗号分隔（如 "cursor-" 覆盖全部 cursor key，
#                           **包括以后新建的**，不用每次改名单）
# 两个都为空 = 本模块完全不生效（这就是一键回退的落点）。
# 默认两个都空 = **不配置就完全不生效**（fail-safe）。
# 第一版把默认值写成某把 key 的名字，导致"删掉环境变量"反而回到单 key 生效，
# 一键回退不干净。范围一律由部署上的环境变量显式声明。
_ALLOWED_KEY_ALIASES = {
    a.strip() for a in os.environ.get("UA_ROUTE_KEY_ALIASES", "").split(",") if a.strip()
}
_ALLOWED_KEY_PREFIXES = tuple(
    p.strip() for p in os.environ.get("UA_ROUTE_KEY_PREFIXES", "").split(",") if p.strip()
)


def key_in_scope(alias: str) -> bool:
    if not alias:
        return False
    if alias in _ALLOWED_KEY_ALIASES:
        return True
    return bool(_ALLOWED_KEY_PREFIXES) and alias.startswith(_ALLOWED_KEY_PREFIXES)

# alias 之后可能出现的名字 -> 两个真池子。
# key 写全两种形态（`gpt-5.6-x` 和 `chatgpt-gpt-5.6-x`），因为 per-key alias 可能
# 把任意一种指过来；value 里的组名是**真实存在的池子**，不是复制品。
_DEFAULT_TABLE = {
    "gpt-5.6-terra": ("chatgpt-gpt-5.6-terra", "zerokey-pool-gpt-5.6-terra"),
    "gpt-5.6-sol": ("chatgpt-gpt-5.6-sol", "zerokey-pool-gpt-5.6-sol"),
    "gpt-5.6-luna": ("chatgpt-gpt-5.6-luna", "zerokey-pool-gpt-5.6-luna"),
    "gpt-5.5": ("chatgpt-gpt-5.5", "zerokey-pool-gpt-5.5"),
    "gpt-5.4": ("chatgpt-gpt-5.4", "zerokey-pool-gpt-5.4"),
    "gpt-5.3-codex": ("chatgpt-gpt-5.3-codex", "zerokey-pool-gpt-5.3"),
}
# 两个池子的名字本身也要能被改写（用户可能直接点名 zerokey-pool-*）
for _base, (_acct, _zk) in list(_DEFAULT_TABLE.items()):
    _DEFAULT_TABLE.setdefault(_acct, (_acct, _zk))
    _DEFAULT_TABLE.setdefault(_zk, (_acct, _zk))


def _load_table() -> dict[str, tuple[str, str]]:
    raw = os.environ.get("UA_ROUTE_TABLE")
    if not raw:
        return dict(_DEFAULT_TABLE)
    try:
        data = json.loads(raw)
        return {k: (v[0], v[1]) for k, v in data.items()}
    except Exception as exc:  # pragma: no cover - 配错就退回默认表
        _log.warning("codex_ua_pool_route: bad UA_ROUTE_TABLE (%r), using default", exc)
        return dict(_DEFAULT_TABLE)


TABLE = _load_table()


def _key_alias(data: dict[str, Any]) -> str:
    for key in ("metadata", "litellm_metadata"):
        md = data.get(key)
        if isinstance(md, dict):
            v = md.get("user_api_key_alias")
            if isinstance(v, str) and v:
                return v
    return ""


def _user_agent(data: dict[str, Any]) -> str:
    for key in ("metadata", "litellm_metadata"):
        md = data.get(key)
        if isinstance(md, dict):
            ua = md.get("user_agent")
            if isinstance(ua, str) and ua:
                return ua
    return ""


def is_desktop(ua: str) -> bool:
    return bool(ua) and bool(_DESKTOP_RE.search(ua))


def pick_pool(model: Any, ua: str) -> str | None:
    """返回要改写成的组名；不需要改写返回 None。"""
    if not isinstance(model, str):
        return None
    entry = TABLE.get(model)
    if not entry:
        return None
    acct, zk = entry
    target = acct if is_desktop(ua) else zk
    return target if target != model else None


def route(data: dict[str, Any], source: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return data
    if not key_in_scope(_key_alias(data)):
        return data
    ua = _user_agent(data)
    target = pick_pool(data.get("model"), ua)
    if target:
        _log.warning("codex_ua_pool_route: %s %r -> %s (ua=%r)",
                     source, data.get("model"), target, ua[:40])
        data["model"] = target
    return data


class CodexUaPoolRoute(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any,
                                 data: dict, call_type: str) -> Any:
        try:
            if str(call_type) in _RESPONSE_CALL_TYPES:
                return route(data, "pre_call:%s" % call_type)
        except Exception as exc:
            _log.warning("codex_ua_pool_route: pre_call error: %r", exc)
        return None


codex_ua_pool_route = CodexUaPoolRoute()
_log.warning("codex_ua_pool_route: loaded, aliases=%s prefixes=%s (both empty = inert)",
             sorted(_ALLOWED_KEY_ALIASES), list(_ALLOWED_KEY_PREFIXES))
