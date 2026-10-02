# pricing_overlay.py — 198 Pro 计价覆盖（2026-08-17，挂 litellm_settings.callbacks 加载）
#
# 只动"记账价"，不影响上游真实行为/缓存命中：
#   1. chatgpt 订阅池全系：cached tokens 按全价 input 记账。
#      缓存修复后 codex 命中 82-93%，订阅池真实成本包月固定，
#      政策决定缓存收益不让利给 key budget。
#   2. gpt-5.6-sol：output = $50/M（至高版本政策价；input $5/M 不动）。
#   3. 全局计费 ×1.3：抵消未计费 token（图片/音频/工具描述等漏计口径）。
#      打在 litellm.response_cost_calculator / litellm.completion_cost 两个
#      模块属性上（logging 层与 passthrough 层各自的入口）。
#      response_cost_calculator 内部调用的是 cost_calculator 模块本地的
#      completion_cost 引用（非 litellm 属性），所以两处包装不会叠乘。
#
# 机制：import 时改 litellm.model_cost + 包装成本函数。
# 回滚：config callbacks 列表删掉本条 + rollout（CM key 留着无害）。

import sys

import litellm
from litellm.integrations.custom_logger import CustomLogger

CHATGPT_BASES = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
)

SOL_OUTPUT_PER_TOKEN = 50.0 / 1_000_000  # $50/M 政策价
GLOBAL_COST_MULTIPLIER = 1.3  # 未计费 token 抵消系数，2026-08-17 政策


def _apply():
    mc = litellm.model_cost
    patched = []
    for base in CHATGPT_BASES:
        for key in (base, f"openai/{base}", f"chatgpt/{base}"):
            entry = mc.get(key)
            if not isinstance(entry, dict):
                continue
            inp = entry.get("input_cost_per_token")
            if inp:
                entry["cache_read_input_token_cost"] = inp
                # 分层/变体价同步对齐（存在才覆盖，不新造字段）
                for suffix in (
                    "_above_200k_tokens",
                    "_above_272k_tokens",
                    "_above_512k_tokens",
                    "_flex",
                    "_priority",
                ):
                    tier_in = entry.get("input_cost_per_token" + suffix)
                    tier_key = "cache_read_input_token_cost" + suffix
                    if tier_in and entry.get(tier_key) is not None:
                        entry[tier_key] = tier_in
            if base == "gpt-5.6-sol":
                entry["output_cost_per_token"] = SOL_OUTPUT_PER_TOKEN
            patched.append(key)
    print(f"[pricing_overlay] patched {len(patched)} entries: {patched}", file=sys.stderr)


def _install_multiplier():
    if getattr(litellm, "_pricing_overlay_multiplier_installed", False):
        return

    def _wrap(fn):
        def wrapper(*args, **kwargs):
            cost = fn(*args, **kwargs)
            try:
                if isinstance(cost, (int, float)) and cost > 0:
                    return cost * GLOBAL_COST_MULTIPLIER
            except Exception:
                pass
            return cost

        wrapper.__name__ = getattr(fn, "__name__", "wrapped_cost_fn")
        wrapper.__wrapped__ = fn
        return wrapper

    litellm.response_cost_calculator = _wrap(litellm.response_cost_calculator)
    litellm.completion_cost = _wrap(litellm.completion_cost)
    litellm._pricing_overlay_multiplier_installed = True
    print(
        f"[pricing_overlay] global cost multiplier x{GLOBAL_COST_MULTIPLIER} installed",
        file=sys.stderr,
    )


try:
    _apply()
    _install_multiplier()
except Exception as e:  # fail-open：计价覆盖失败不能挡启动
    print(f"[pricing_overlay] FAILED: {e}", file=sys.stderr)


class _PricingOverlayNoop(CustomLogger):
    """空 callback 载体：本模块的工作在 import 时已完成。"""


pricing_overlay = _PricingOverlayNoop()
