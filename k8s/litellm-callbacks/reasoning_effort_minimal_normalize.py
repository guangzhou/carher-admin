"""
LiteLLM deployment-level hook — 把 `reasoning_effort: "minimal"` 归一成 `"low"`。

Bug（2026-09-28 实测）
---------------------
gpt-5.6 家族（sol / luna / terra / mini）的上游不认 `minimal` 这一档：

    Unsupported value: 'minimal' is not supported with the 'gpt-5.6-terra' model.
    Supported values are: 'none', 'low', 'medium', 'high', 'xhigh', and 'max'.
    param: reasoning.effort   code: unsupported_value   status: 400

gpt-6 家族（astra / sol / luna）认 `minimal`，客户端按 gpt-6 的档位表发，
一旦请求落到 5.6 家族就被硬拒。1 小时窗口实测 121 次（terra 77 / sol 22 / luna 22）。

这条 400 不写 SpendLogs，所以从 SpendLogs 看这些落点是「零行」而非「失败」。
实测一条请求的完整轨迹（mlc5p 12:29:30~12:29:35）：

    chatgpt-gpt-5.6-terra  400 minimal
    → 同组换号 ×5          全部 400 minimal
    → chatgpt-gpt-5.6-luna 400 minimal
    → chatgpt-gpt-5.6-sol  400 minimal

**整条 fallback 链被同一个参数一路拒到底** —— 换号和跨组兜底都救不了，
因为被拒的原因跟账号无关，是请求体本身。`chatgpt-gpt-6-astra` 的链第 2、3 跳
（chatgpt-gpt-6-luna → chatgpt-gpt-5.6-sol）正是这样成为死胡同。

为什么挂 async_pre_call_deployment_hook 而不是 async_pre_call_hook
-----------------------------------------------------------------
`async_pre_call_hook` 跑在**路由之前**，那时 `data["model"]` 还是客户端发的名字
（如 `gpt-6-astra`），看不到落点是哪个家族；fallback 跳到 5.6 时它早已执行完毕。
`async_pre_call_deployment_hook` 在**选定 deployment 之后、发出请求之前**触发，
兜底路径的每一跳都会过一次 —— 这是唯一能按落点名判断的挂点
（同 local_gpu_max_tokens_clamp.py 的用法）。

Fix
---
仅当目标模型属 5.6 家族且 effort == "minimal" 时改写为 "low"（该家族支持的
最低非 none 档）。其余一切逐字节原样放过。

不降到 `none`：`minimal` 的语义是「尽量少推理但仍推理」，`none` 是「完全不推理」，
降到 none 会改变输出质量；`low` 是最接近的档。

不用硬编码模型名表：新增一个 5.6 衍生型号就会成为永久盲区
（见 topic_ruler_failure_shapes）。按 "5.6" 子串判断，落点名
（`openai/chatgpt-gpt-5.6-sol`）和裸名（`gpt-5.6-terra`）都能命中。

注册：litellm_settings.callbacks 加
    - reasoning_effort_minimal_normalize.reasoning_effort_minimal_normalize
"""

import logging
from typing import Any, Dict

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("reasoning_effort_minimal_normalize")

# 不认 minimal 的家族标记。用子串而非全名表，避免新增型号成为盲区。
_MARKER = "5.6"
_FROM = "minimal"
_TO = "low"


def _target_model(kwargs: Dict[str, Any]) -> str:
    """取落点模型名。优先 litellm_params.model（真实上游名），退回顶层 model。"""
    lp = kwargs.get("litellm_params")
    if isinstance(lp, dict):
        m = lp.get("model")
        if m:
            return str(m)
    return str(kwargs.get("model") or "")


def _rejects_minimal(model: str) -> bool:
    return _MARKER in model


class ReasoningEffortMinimalNormalize(CustomLogger):
    async def async_pre_call_deployment_hook(
        self, kwargs: dict, call_type: Any
    ) -> Any:
        try:
            if not isinstance(kwargs, dict):
                return kwargs

            model = _target_model(kwargs)
            if not _rejects_minimal(model):
                return kwargs

            changed = []

            # 形状一：顶层 reasoning_effort（chat/completions 风格）
            if str(kwargs.get("reasoning_effort", "")).lower() == _FROM:
                kwargs["reasoning_effort"] = _TO
                changed.append("reasoning_effort")

            # 形状二：responses API 的 reasoning.effort 嵌套体
            # （实测报错 param 就是 reasoning.effort）
            reasoning = kwargs.get("reasoning")
            if (
                isinstance(reasoning, dict)
                and str(reasoning.get("effort", "")).lower() == _FROM
            ):
                # 拷贝再写：别就地改客户端传进来的同一个 dict 引用，
                # 那个引用可能被 fallback 的下一跳复用。
                new_reasoning = dict(reasoning)
                new_reasoning["effort"] = _TO
                kwargs["reasoning"] = new_reasoning
                changed.append("reasoning.effort")

            if changed:
                _log.warning(
                    "reasoning_effort_minimal_normalize: %s %s -> %s "
                    "for model=%s (call_type=%s)",
                    ",".join(changed),
                    _FROM,
                    _TO,
                    model,
                    call_type,
                )
        except Exception as exc:  # 防御：归一不动也绝不能拦请求
            _log.warning("reasoning_effort_minimal_normalize error: %r", exc)
        return kwargs


reasoning_effort_minimal_normalize = ReasoningEffortMinimalNormalize()
