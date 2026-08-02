"""test_degrade_strip_no_kwarg_leak.py — degrade_strip 幂等标记不得污染 payload。

背景（2026-08-02 生产故障）
--------------------------
``encrypted_content_degrade_strip.py`` 为修 fallback 重入（2026-06-25 那次
503），在第一次 strip 成功后打了个幂等标记::

    request_kwargs["_degrade_stripped"] = True

但 ``request_kwargs`` **就是发往上游的 payload**。anthropic 通路会把不认识的
kwarg 丢掉，所以这个漏洞潜伏了一个多月没暴露；直到把 gpt 组兜底首选换成
``wangsu-cheliantianxia1-qwen3.7-plus``（``custom_openai``，走 OpenAI SDK），
它立刻炸::

    InternalServerError: Custom_openaiException -
    AsyncCompletions.create() got an unexpected keyword argument '_degrade_stripped'

也就是说这个 callback 隐性限定了"哪些 provider 能当兜底目标"——加兜底目标的人
根本不会想到来审它。

修法：标记搬到 ContextVar，payload 一个字节都不碰；顺手把 legacy key pop 掉，
让滚动期间残留的 payload 自愈。

本文件钉住两件事：①标记绝不出现在 payload 里；②fallback 重入仍然被识别（不能
为了修泄漏把 2026-06-25 那个 503 放回来）。

用法::

    python3 test_degrade_strip_no_kwarg_leak.py [被测文件路径]
"""
import asyncio
import importlib.util
import sys
import types

DEFAULT_PATH = "../encrypted_content_degrade_strip.py"


def _stub_litellm():
    """最小 litellm 替身，使回归可脱离集群运行。"""
    if "litellm.integrations.custom_logger" in sys.modules:
        return
    try:
        import litellm.integrations.custom_logger  # noqa: F401
        return
    except Exception:
        pass
    litellm = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    logging_mod = types.ModuleType("litellm._logging")
    exceptions = types.ModuleType("litellm.exceptions")

    class CustomLogger:
        pass

    class _Err(Exception):
        pass

    custom_logger.CustomLogger = CustomLogger
    import logging as _logging
    logging_mod.verbose_router_logger = _logging.getLogger("litellm.router")
    for name in ("BadRequestError", "RateLimitError", "ServiceUnavailableError"):
        setattr(exceptions, name, type(name, (_Err,), {}))
    litellm.integrations = integrations
    integrations.custom_logger = custom_logger
    litellm._logging = logging_mod
    litellm.exceptions = exceptions
    for name in ("BadRequestError", "RateLimitError", "ServiceUnavailableError"):
        setattr(litellm, name, getattr(exceptions, name))
    sys.modules.update({
        "litellm": litellm,
        "litellm.integrations": integrations,
        "litellm.integrations.custom_logger": custom_logger,
        "litellm._logging": logging_mod,
        "litellm.exceptions": exceptions,
    })


_stub_litellm()
PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
spec = importlib.util.spec_from_file_location("edgs", PATH)
MOD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MOD)

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


# --- 1. 源码级护栏：不许再往 request_kwargs 写标记 --------------------------
# 用 AST 而不是字符串匹配 —— 文档字符串里记录"旧做法"是合理的，护栏只该看代码。
import ast

src = open(PATH).read()
_bad_writes = []
for node in ast.walk(ast.parse(src)):
    if not isinstance(node, (ast.Assign, ast.AugAssign)):
        continue
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for t in targets:
        if (isinstance(t, ast.Subscript)
                and isinstance(t.value, ast.Name)
                and t.value.id == "request_kwargs"):
            key = getattr(t.slice, "value", None)
            # 重写 input / 删 previous_response_id 是本模块的正事；
            # 真正的不变式是：内部记账（下划线开头的键）不许进 payload。
            if isinstance(key, str) and key.startswith("_"):
                _bad_writes.append((getattr(node, "lineno", "?"), key))
check("代码里没有把内部标记(_ 开头)写进 request_kwargs",
      not _bad_writes, _bad_writes)
check("改用 ContextVar 承载标记", "_degrade_stripped_var" in src and "contextvars" in src)
check("legacy key 仍被 pop 掉（滚动期自愈）",
      'request_kwargs.pop("_degrade_stripped"' in src)

# --- 2. strip 本身不引入新键 ------------------------------------------------
payload = {
    "model": "chatgpt-gpt-5.6-terra",
    "previous_response_id": "resp_x",
    "input": [
        {"type": "reasoning", "id": "encitem_a", "encrypted_content": "litellm_enc:zz",
         "summary": [{"type": "summary_text", "text": "t"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    ],
}
before_keys = set(payload)
did = MOD._strip_encrypted_reasoning(payload)
check("strip 生效", did is True)
check("strip 之后 payload 不多出任何键", set(payload) - before_keys == set(),
      set(payload) - before_keys)
check("_degrade_stripped 绝不在 payload 里", "_degrade_stripped" not in payload,
      list(payload))
check("previous_response_id 被剥掉", "previous_response_id" not in payload)

# --- 3. ContextVar 语义：同请求内可见，跨请求隔离 ---------------------------
var = MOD._degrade_stripped_var
check("默认值为 False", var.get() is False)


async def _same_task_visible():
    var.set(True)
    return var.get()


async def _fresh_task_isolated():
    async def child():
        return var.get()
    # 新 task 会拷贝当前 context: 父设过就该看得见
    var.set(True)
    seen_by_child = await asyncio.create_task(child())
    return seen_by_child


async def _independent_requests():
    async def req(mark):
        if mark:
            var.set(True)
        await asyncio.sleep(0)
        return var.get()
    a, b = await asyncio.gather(asyncio.create_task(req(True)),
                                asyncio.create_task(req(False)))
    return a, b


check("同一请求内 set 后可见", asyncio.run(_same_task_visible()) is True)
check("fallback 重入(同 context 子任务)看得见标记", asyncio.run(_fresh_task_isolated()) is True)
a, b = asyncio.run(_independent_requests())
check("并发的另一个请求不受污染", a is True and b is False, (a, b))

# --- 4. 汇总 ----------------------------------------------------------------
passed = sum(1 for _, ok, _ in _results if ok)
for name, ok, detail in _results:
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "   -> %r" % (detail,)))
print("\n%d/%d passed" % (passed, len(_results)))
if passed != len(_results):
    sys.exit(1)
