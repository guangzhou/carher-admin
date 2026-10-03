"""local_gpu_max_tokens_clamp — 钳制打到自建 local-gpu 盒子的 max_tokens。

Why: her 侧 catalog 按厂商规格给 deepseek-v4-flash 写 maxTokens=384000，而自建
sglang 盒子 max_model_len=393216 且「输入+max_tokens」一起计入 —— 任何真实会话
必 400，全部回落 official fallback，盒子接不到流量（her-170 事故同源）。

做法: async_pre_call_deployment_hook（兜底路径也会过）里按 deployment 的
api_base 含盒子 IP 门控，把 max_tokens 系列字段钳到 65536
（393216 - 65536 = 327680 留给输入；超长输入仍靠 context_window_fallbacks 落 official）。
"""
import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("local_gpu_max_tokens_clamp")

_BOX_MARKER = "36.151.241.10"
_MAX_OUT = 65536
_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def _api_base(kwargs: dict) -> str:
    for src in (kwargs, kwargs.get("litellm_params") or {}):
        if isinstance(src, dict):
            ab = src.get("api_base")
            if ab:
                return str(ab)
    return ""


class LocalGpuMaxTokensClamp(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any) -> Any:
        try:
            if not isinstance(kwargs, dict):
                return kwargs
            if _BOX_MARKER not in _api_base(kwargs):
                return kwargs
            for f in _FIELDS:
                v = kwargs.get(f)
                if isinstance(v, int) and v > _MAX_OUT:
                    kwargs[f] = _MAX_OUT
                    _log.warning("local_gpu_clamp: %s %d -> %d (call_type=%s)", f, v, _MAX_OUT, call_type)
        except Exception as exc:  # 防御：钳不动也绝不能拦请求
            _log.warning("local_gpu_clamp error: %r", exc)
        return kwargs


local_gpu_max_tokens_clamp_instance = LocalGpuMaxTokensClamp()
local_gpu_max_tokens_clamp = local_gpu_max_tokens_clamp_instance
