"""
LiteLLM callback — 只给发往 kiro-rs-slots（kiro 槽位实例）的那一跳带上调用方 key 身份。

为什么要它
----------
kiro-rs-slots 按「人」把用户固定到一个槽（1 号 + 1 出口 IP），需要知道这次请求是哪把 key。
LiteLLM 自带的 `add_user_information_to_llm_headers` + `model_group_settings` 判断时用的是
**客户端发来的模型名**（per-key alias 改写之前），对 `claude-opus-5-5 → kiro-slots-claude-opus-5-5`
这种别名永远不命中；全局开又会把身份头带给所有 provider（包括兜底到的第三方）。

做法
----
`async_pre_call_deployment_hook`：路由器已经选定具体 deployment、请求马上要发出时才执行。
只有当这次要打的 `api_base` 就是 kiro-rs-slots 时，才往本次请求的 `headers` 里加两个头：

    x-litellm-user_api_key_hash   调用方 key 的 hash（LiteLLM 存库的那个）
    x-litellm-user_api_key_alias  调用方 key 的别名

- 其它任何 provider / deployment：一个字节都不改（不加、不删、不覆盖）。
- 同一请求兜底到别家：兜底是新的一跳、api_base 不同，本 hook 不加头。
- kiro-rs-slots 只在本地用它选槽；发往 Kiro 官方的请求头是写死的一套，不带这两个头
  （kiro.rs 侧有测试 `upstream_request_carries_no_user_identity_headers` 守着）。

开关
----
环境变量 `KIRO_SLOTS_IDENTITY_HOSTS`（逗号分隔，默认 `kiro-rs-slots.kiro-rs.svc.cluster.local`
+ 它的 ClusterIP `10.43.252.245`），只有 api_base 的 host 完全等于其中一个才生效。设为空串 = 整体关闭。

为什么要认 IP（2026-10-27）：`aws-*` 组的 api_base 写 ClusterIP 而不是服务名 ——
`/v2/model/info` 对普通 key 也吐 litellm_params.api_base，写服务名等于把后端线路名给每个人看。
ClusterIP 只在删重建 svc 时才变；变了这里要跟着改（`kubectl -n kiro-rs get svc kiro-rs-slots`）。

注册（litellm_settings.callbacks）::

    - kiro_slots_identity.kiro_slots_identity
"""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import urlparse

from litellm.integrations.custom_logger import CustomLogger

_DEFAULT_HOSTS = "kiro-rs-slots.kiro-rs.svc.cluster.local,10.43.252.245"
HDR_HASH = "x-litellm-user_api_key_hash"
HDR_ALIAS = "x-litellm-user_api_key_alias"


def _hosts() -> frozenset[str]:
    raw = os.environ.get("KIRO_SLOTS_IDENTITY_HOSTS", _DEFAULT_HOSTS)
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _api_base(kwargs: dict[str, Any]) -> Optional[str]:
    """本次要打的 api_base：deployment 的 litellm_params 优先，退回顶层 kwargs。"""
    for src in (kwargs.get("litellm_params"), kwargs):
        if isinstance(src, dict):
            v = src.get("api_base")
            if isinstance(v, str) and v:
                return v
    return None


def _identity(kwargs: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """从本次请求的 metadata 取调用方 key 的 hash / alias（proxy 鉴权后写入）。"""
    for key in ("litellm_metadata", "metadata"):
        md = kwargs.get(key)
        if isinstance(md, dict) and md.get("user_api_key_hash"):
            return md.get("user_api_key_hash"), md.get("user_api_key_alias")
    lp = kwargs.get("litellm_params")
    if isinstance(lp, dict):
        for key in ("litellm_metadata", "metadata"):
            md = lp.get(key)
            if isinstance(md, dict) and md.get("user_api_key_hash"):
                return md.get("user_api_key_hash"), md.get("user_api_key_alias")
    return None, None


def is_slots_target(api_base: Optional[str], hosts: Optional[frozenset[str]] = None) -> bool:
    if not api_base:
        return False
    hosts = _hosts() if hosts is None else hosts
    if not hosts:
        return False
    try:
        host = (urlparse(api_base).hostname or "").lower()
    except Exception:
        return False
    return host in hosts


class KiroSlotsIdentity(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs: dict[str, Any], call_type: Any) -> Optional[dict]:
        try:
            if not is_slots_target(_api_base(kwargs)):
                return None  # 别的 provider：原样不动
            h, a = _identity(kwargs)
            if not h:
                return None
            headers = dict(kwargs.get("headers") or {})
            headers[HDR_HASH] = str(h)
            if a:
                headers[HDR_ALIAS] = str(a)
            out = dict(kwargs)
            out["headers"] = headers
            return out
        except Exception:
            return None  # 任何异常都不影响请求本身


kiro_slots_identity = KiroSlotsIdentity()
