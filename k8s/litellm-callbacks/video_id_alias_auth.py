"""视频查进度/下载按对外名验权，内部组名不用再进 key 的允许列表（2026-10-30）。

问题
----
提交视频 ``POST /v1/videos`` 带 ``model: gemini-video``，鉴权看的是请求名，在允许列表里就过，
然后 key 的 ``aliases`` 把它改写成内部组 ``web-gemini-video`` 去路由。

但之后的 ``GET /v1/videos/{id}`` 和 ``GET /v1/videos/{id}/content`` 请求里**没有模型名**。
LiteLLM 从 video_id 里解出当初的 deployment id，再反查成组名（=内部名 ``web-gemini-video``），
拿这个名字去对 key 的允许列表（``auth_utils._extract_models_from_managed_resource_id``）。
允许列表里只有对外名 ⇒ 403 ``key_model_access_denied``。10-30 实测：cursor-liuguoxian-std
去掉 ``web-gemini-video`` 后，提交 200、查进度 403。

所以以前每把开视频的 key 都得把内部名也塞进允许列表，用户菜单里就多出一个看不懂的名字。

做法（两处包装，用一个 contextvar 串起来）
------------------------------------------
1. 包 ``auth_utils._extract_models_from_managed_resource_id``：当它是从 video_id / character_id
   解出组名时，把这些组名记进 contextvar。
2. 包 ``can_key_call_model``（它手上有 ``valid_token``）：入参 model 里凡是第 1 步记下的、并且
   这把 key 的 ``aliases`` 里有某个对外名指向它的，**换成那个对外名**再交给原函数；用完立刻清掉标记。

两步在 ``user_api_key_auth._enforce_key_model_access`` 里是紧挨着的：先
``_get_model_from_request_context``（内部走到第 1 步），下一句就 ``can_key_call_model``（第 2 步）。

🔴 为什么是**替换**不是追加：``_can_object_call_model`` 收到 list 时对每一项都单独校验
（``for m in model: _can_object_call_model(...)``），**每一项都必须在允许列表里**。追加的话内部名那一项照样 403。

不放宽权限：只在「这把 key 的 aliases 里确实有对外名 → 这个内部名」时才替换，换完仍要过原函数的
允许列表检查 ⇒ 能通过的前提是「对外名在这把 key 的允许列表里」，跟提交视频时的判据完全一样。
没有别名指向它的（例如 ``muse-video`` 这种同名组直接放在允许列表里）原样不动。
标记只来自资源 id 反解，普通请求体里的 ``model`` 不会被替换。

kill switch：``VIDEO_ID_ALIAS_AUTH_DISABLED=1``。
"""
from __future__ import annotations

import contextvars
import logging
import os
from typing import Any

_log = logging.getLogger("litellm.video_id_alias_auth")

# 只在「这次的候选模型名是从 video_id / character_id 反解出来的」时才做替换。
_FROM_RESOURCE_ID: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "video_id_alias_auth_from_resource_id", default=frozenset())


def _disabled() -> bool:
    return os.environ.get("VIDEO_ID_ALIAS_AUTH_DISABLED") == "1"


def _wrap_extract(orig):
    if getattr(orig, "_video_id_alias_patched", False):
        return orig

    def patched(resource_id, *args, **kwargs):
        out = orig(resource_id, *args, **kwargs)
        try:
            field = kwargs.get("resource_id_field")
            if field in ("video_id", "character_id") and out:
                names = out if isinstance(out, (list, tuple)) else [out]
                _FROM_RESOURCE_ID.set(_FROM_RESOURCE_ID.get() | frozenset(n for n in names if isinstance(n, str)))
        except Exception:
            pass
        return out

    patched._video_id_alias_patched = True
    return patched


def _swap_to_public(model: Any, aliases: dict, from_rid: frozenset) -> Any:
    """把「从资源 id 解出来的内部组名」换成这把 key 里指向它的对外名。没有对应别名就原样。"""
    reverse: dict[str, list[str]] = {}
    for public, target in (aliases or {}).items():
        if isinstance(public, str) and isinstance(target, str) and public != target:
            reverse.setdefault(target, []).append(public)

    def one(m):
        if isinstance(m, str) and m in from_rid and reverse.get(m):
            return sorted(reverse[m])[0]
        return m

    if isinstance(model, list):
        seen, out = set(), []
        for m in (one(x) for x in model):
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out
    return one(model)


def _wrap_can_key_call_model(orig):
    if getattr(orig, "_video_id_alias_patched", False):
        return orig

    async def patched(model, *args, **kwargs):
        from_rid = _FROM_RESOURCE_ID.get()
        if from_rid:
            # 只用一次：同一请求后面的检查（兜底目标、预算等）不能继承这个标记
            _FROM_RESOURCE_ID.set(frozenset())
        try:
            if from_rid and not _disabled():
                valid_token = kwargs.get("valid_token")
                if valid_token is None and len(args) >= 2:
                    valid_token = args[1]
                aliases = getattr(valid_token, "aliases", None) or {}
                new = _swap_to_public(model, aliases, from_rid)
                if new != model:
                    _log.warning("video_id_alias_auth: resource-id model %r -> public %r", model, new)
                    model = new
        except Exception as exc:
            _log.warning("video_id_alias_auth: swap failed, using original: %r", exc)
        return await orig(model, *args, **kwargs)

    patched._video_id_alias_patched = True
    return patched


def _install() -> bool:
    if _disabled():
        _log.warning("video_id_alias_auth: disabled by env")
        return False
    import importlib
    ok = True
    try:
        au = importlib.import_module("litellm.proxy.auth.auth_utils")
        au._extract_models_from_managed_resource_id = _wrap_extract(au._extract_models_from_managed_resource_id)
    except Exception as exc:
        _log.warning("video_id_alias_auth: cannot patch auth_utils: %r", exc)
        ok = False
    # can_key_call_model 被 user_api_key_auth 按名字 import ⇒ 两个命名空间都要换（同 budget_notice）
    for modpath in ("litellm.proxy.auth.user_api_key_auth", "litellm.proxy.auth.auth_checks"):
        try:
            mod = importlib.import_module(modpath)
            orig = getattr(mod, "can_key_call_model", None)
            if orig is None:
                continue
            setattr(mod, "can_key_call_model", _wrap_can_key_call_model(orig))
        except Exception as exc:
            _log.warning("video_id_alias_auth: cannot patch %s: %r", modpath, exc)
            ok = False
    _log.warning("video_id_alias_auth: loaded ok=%s", ok)
    return ok


_INSTALLED = _install()

# config.yaml 的 litellm_settings.callbacks 只认 CustomLogger 实例。补丁本身在 import 时已装好，
# 这个实例不做事，只是让模块按 callback 方式被加载（同 same_group_failover_cap 等猴补丁模块）。
try:
    from litellm.integrations.custom_logger import CustomLogger as _CL

    class _VideoIdAliasAuth(_CL):
        pass

    video_id_alias_auth = _VideoIdAliasAuth()
except Exception:  # pragma: no cover - 只在没有 litellm 的环境里
    video_id_alias_auth = None
