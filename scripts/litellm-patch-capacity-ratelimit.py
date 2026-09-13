#!/usr/bin/env python3
"""
LiteLLM exception_mapping_utils.py — "at capacity" 升格补丁。

问题：上游 chatgpt/codex 偶发吐 400 + body "Selected model is at capacity"
（或 "Selected model isn't available"）。vanilla LiteLLM 把它落成 BadRequestError，
router 既不 cooldown 也不 fallback、也不 retry，整个请求直接穿透到用户。

修复：在 `invalid_request_error` 那条 catch-all elif 之前插一条分支，命中这两句
就升格成 RateLimitError，让 router 走 cooldown + fallbacks + retry_policy。

为什么是就地插入而不是整文件 COPY：
  旧的 Dockerfile.prod 用的是 `COPY exception_mapping_utils.py`，那份文件冻结在
  v1.89.2。把它盖到新 base 上会把 base 自己在这个文件里的全部改动一起回退掉 ——
  这个区间里上游真的改过它（v1.98.0 #36705 把裸 429 用 status_code 门住、
  v1.100.0 #38318 给没有 exception_type 分支的 provider 加按 status 的映射）。
  整文件 COPY 会静默抹掉这两条。所以这里只做加法。

上游状态（2026-09-13 查证）：没修，也不打算按我们这个形状修。
  - 全仓库搜 "Selected model is at capacity"：0 条 issue/PR
  - v1.100.1 的 ExceptionCheckers.is_error_str_rate_limit 只认三种形状
    （被 status_code 门住的裸 429 / rate[\\s_-]*limit / Mistral 整句
     "service tier capacity exceeded"），这句一条都不匹配
  - 想扩短语的 PR #38706 仍 open
  ⚠️ 不要改成"靠 body 里的裸数字判限流"——那正是 #36705 修掉的坑。

用法：
    python3 litellm-patch-capacity-ratelimit.py [目标文件]
默认目标：
    /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py

幂等：文件里已有 MARKER 就跳过并返回 1（未改动），成功插入返回 0。
"""

import os
import sys

TARGET = (
    "/app/.venv/lib/python3.13/site-packages/litellm/"
    "litellm_core_utils/exception_mapping_utils.py"
)

MARKER = "selected model is at capacity"

# v1.100.1 把整条 OpenAI 分支链抽成了模块级 helper，缩进从 16 空格变成 4 空格，
# 而且 elif 条件被压回一行。旧锚点（16 空格、三行条件）在新 base 上匹配 0 次 —— 这是
# 门禁按设计红的，不是脚本坏了。同理，`exception_mapping_worked` 那个局部变量在新
# helper 里不存在，补丁块里必须去掉它，否则 NameError。
ANCHOR = (
    '    elif "invalid_request_error" in error_str '
    'and "Incorrect API key provided" not in error_str:\n'
)

PATCH = '''    elif (
        "selected model is at capacity" in error_str.lower()
        or "selected model isn't available" in error_str.lower()
    ):
        # 198-prod patch: upstream chatgpt/codex 偶发吐 400+"Selected model is at capacity",
        # vanilla 走 BadRequestError 不重试不 fallback. 升格成 RateLimitError 让 router
        # 触发 cooldown + fallbacks + retry_policy.
        # ref: Wei-Shaw/sub2api#2481, openai/codex#22390
        raise RateLimitError(
            message=f"RateLimitError: {exception_provider} (transient capacity) - {message}",
            model=model,
            llm_provider=custom_llm_provider,
            response=getattr(original_exception, "response", None),
            litellm_debug_info=extra_information,
        )
'''


def apply_patch(filepath: str) -> bool:
    with open(filepath, "r", encoding="utf-8") as handle:
        content = handle.read()

    if MARKER in content:
        print(f"SKIP: capacity branch already present in {filepath}")
        return False

    count = content.count(ANCHOR)
    if count != 1:
        # 锚点漂了就停，不猜。base 换代时这里会先红，比静默漏打补丁好。
        print(
            f"ERROR: anchor matched {count} times (want exactly 1) in {filepath}; "
            "upstream 可能改了 invalid_request_error 那条分支，需要人工重定锚点"
        )
        return False

    content = content.replace(ANCHOR, PATCH + ANCHOR, 1)

    with open(filepath, "w", encoding="utf-8") as handle:
        handle.write(content)

    pyc_dir = os.path.join(os.path.dirname(filepath), "__pycache__")
    if os.path.isdir(pyc_dir):
        import glob

        for pyc in glob.glob(os.path.join(pyc_dir, "exception_mapping_utils*.pyc")):
            os.remove(pyc)
            print(f"  deleted: {pyc}")

    os.utime(filepath, None)
    print(f"OK: capacity branch injected into {filepath}")
    return True


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else TARGET
    if not os.path.exists(target):
        print(f"ERROR: {target} not found")
        sys.exit(2)
    sys.exit(0 if apply_patch(target) else 1)
