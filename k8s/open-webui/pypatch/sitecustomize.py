"""OWUI 本地补丁（PVC 上，靠 deployment 的 PYTHONPATH=/app/backend/data/pypatch 加载）。

两件事，互相独立：

1. 给 feishu provider 补一个显示名。
   上游 config.py 的 OAUTH_PROVIDERS['feishu'] 没有 'name' 键（只有 oidc 有），
   main.py 的 /api/config 用 provider.get('name', name) 取标签，
   所以登录按钮只能退回显示 provider 的 key 'feishu'。
   等 open_webui.config 导入完成（它在模块层就调了 load_oauth_providers()），
   往已经建好的 dict 里补一个键。/api/config 是请求时才读，所以补上就生效。

2. 给「同一轮工具循环内」的工具输出加长度上限。
   背景：context compaction 只在用户每轮入口跑一次
   （middleware.py:2471，process_chat_payload 里），而工具循环
   （middleware.py:5551-6041，最多 CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS=256 轮）
   每轮都把 [*form_data['messages'], *tool_messages] 重新发一次，循环体内
   没有任何再压缩。2026-09-18 wanglihua 就是这样在一轮内从 178k 涨到 547k
   token，撞上 grok-4.6 的 500000 上限，被 litellm 在选 deployment 之前
   就抛 ContextWindowExceededError（router.py:10762，_pre_call_checks）。
   单个元凶示例：grep_knowledge_files 一次吐 259033 字符 —— 它只 cap 命中条数
   （KNOWLEDGE_GREP_MAX_MATCHES=50），不 cap 每行长度，而 xlsx 转出来的
   一"行"是整个表格行。

   为什么拦在 convert_output_to_messages：它是四个 LLM payload 构造点
   （misc.py:325 本体；middleware.py:2218 / 5938 / 5943 / 6193）的唯一收口，
   而且这四处全是"发给模型"的路径 —— 存库和发前端走的是 full_output()
   （middleware.py:5909-5915），不经过这里。所以截断只影响模型看到的内容，
   聊天记录和用户界面上的原文都不动。

   只截断、绝不删消息：assistant 的 tool_calls 和 role='tool' 的
   tool_call_id 必须成对，删一条会让请求直接变成非法。

   旋钮（都是 deployment 的环境变量，改完需要重启才生效）：
     OWUI_TOOL_OUTPUT_MAX_CHARS=60000        单条工具输出上限
     OWUI_TOOL_OUTPUT_TOTAL_MAX_CHARS=240000 一次请求内工具输出总预算
     OWUI_TOOL_OUTPUT_FLOOR_CHARS=2000       超总预算时，从最旧的开始压到这个地板

   预算数字的来源（2026-09-19 实测 webui.db 未截断原文，4000 个 chat）：
     function_call_output 2496 条，p50=1458 p95=13941 p99=52058 max=397099
     超 30000 的 52 条（2.08%），超 100000 的 15 条（0.60%）
   per 取 60000 是为了盖住 p99=52058（原来的 30000 会把 p99 那档砍掉一半多），
   同时仍然拦住 397k 那种。⛔ 别用 SpendLogs 量这个分布：litellm 的
   MAX_STRING_LENGTH_PROMPT_IN_DB 默认 2048，写库前逐字符串截断，量出来的
   "最大 4826 字符"全是量具产物。
     OWUI_TOOL_OUTPUT_CAP_DISABLED=1         整个第 2 项的 kill switch

不想要这个文件了：删掉 deployment 的 PYTHONPATH 环境变量即可。
"""

import builtins
import logging
import os
import sys

log = logging.getLogger("owui.pypatch")

_real_import = builtins.__import__

_done = {"feishu": False, "toolcap": False}


# ---------------------------------------------------------------- 工具输出截断

def _int_env(name, default):
    try:
        value = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


_EXHAUSTED_MARKER = "...[本地补丁截断：本条消息额度已用尽]..."


def _middle_truncate(text, limit):
    """保留头部和尾部，砍中间。grep/表格类输出尾部往往和头部一样有信息量。"""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    dropped = len(text) - limit
    marker = f"\n...[本地补丁截断：省略 {dropped} 字符，原长 {len(text)}]...\n"
    return text[:head] + marker + (text[-tail:] if tail > 0 else "")


def _cap_text_in_content(content, limit):
    """content 可能是 str，也可能是多模态的 part 列表。返回 (新content, 新长度)。

    limit 是**整条消息**的上限，不是每个 part 各一份。原来的写法给列表里每个
    input_text 都发一份完整 limit，3 个 part 的消息就能合法占到 3×limit，而
    _cap_tool_messages 拿这个返回值当"这条消息的长度"去凑总预算 —— 预算算术
    直接失真。misc.py 确实会产出列表形状的 tool 消息
    （'content': [{'type': 'input_text', 'text': ...}]），所以这条路真会走到。
    """
    if isinstance(content, str):
        capped = _middle_truncate(content, limit)
        return capped, len(capped)
    if isinstance(content, list):
        out = []
        total = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "input_text":
                text = part.get("text", "")
                if isinstance(text, str):
                    # 按顺序分配剩余额度：前面的 part 用掉多少，后面就少多少。
                    remaining = limit - total
                    if remaining <= 0:
                        # 额度已耗尽。保留 part 结构（不删 part，避免破坏
                        # 多模态消息的形状），但正文清空并留下痕迹。
                        out.append({**part, "text": _EXHAUSTED_MARKER})
                        total += len(_EXHAUSTED_MARKER)
                        continue
                    capped = _middle_truncate(text, remaining)
                    total += len(capped)
                    out.append({**part, "text": capped})
                    continue
            out.append(part)
        return out, total
    return content, 0


def _content_len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return 0


def _cap_tool_messages(messages):
    if os.getenv("OWUI_TOOL_OUTPUT_CAP_DISABLED") == "1":
        return messages
    if not isinstance(messages, list):
        return messages

    per = _int_env("OWUI_TOOL_OUTPUT_MAX_CHARS", 60000)
    total_cap = _int_env("OWUI_TOOL_OUTPUT_TOTAL_MAX_CHARS", 240000)
    floor = _int_env("OWUI_TOOL_OUTPUT_FLOOR_CHARS", 2000)

    idxs = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    if not idxs:
        return messages

    before = sum(_content_len(messages[i].get("content")) for i in idxs)

    # 第一轮：单条上限
    running = 0
    for i in idxs:
        content, length = _cap_text_in_content(messages[i].get("content"), per)
        messages[i] = {**messages[i], "content": content}
        running += length

    # 第二轮：还超总预算，就从最旧的开始压到地板（最新的工具输出对模型最有用）。
    #
    # 最新那条也要参与，只是给它更高的地板。原来写 idxs[:-1] 把它完全豁免，
    # 于是可压上限只有 (N-1)*floor + per —— N 小的时候根本降不到 total_cap，
    # 静默不收敛。现在最坏情况也能收到 (N-1)*floor + max(floor, per)。
    if running > total_cap:
        newest = idxs[-1]
        for i in idxs:
            if running <= total_cap:
                break
            # 最新一条保留到 per（单条上限），其余压到地板。
            target = max(floor, per) if i is newest else floor
            was = _content_len(messages[i].get("content"))
            if was <= target:
                continue
            content, length = _cap_text_in_content(messages[i].get("content"), target)
            messages[i] = {**messages[i], "content": content}
            running -= was - length

    if running > total_cap:
        # 收敛失败。用 WARNING 而不是 INFO —— 补丁跑在 OWUI 装 logging handler
        # 之前，此时 root.handlers 为空，stdlib 的 lastResort 只收 WARNING 以上，
        # INFO 会被静默丢掉（09-19 实测过）。
        log.warning(
            "pypatch tool-output cap 未收敛: %d 条工具消息压到 %d 字符，"
            "仍超总预算 %d（per=%d floor=%d）",
            len(idxs),
            running,
            total_cap,
            per,
            floor,
        )

    if running < before:
        log.info(
            "pypatch tool-output cap: %d tool msgs, %d -> %d chars "
            "(per=%d total=%d floor=%d)",
            len(idxs),
            before,
            running,
            per,
            total_cap,
            floor,
        )
    return messages


def _install_tool_cap():
    """包住 convert_output_to_messages。

    middleware.py:107 是 `from ... import convert_output_to_messages`，拿的是
    函数对象本身，所以光改 misc 模块属性对 middleware 无效 —— 两个模块的
    全局名都要换。
    """
    misc = sys.modules.get("open_webui.utils.misc")
    if misc is None:
        return False
    original = getattr(misc, "convert_output_to_messages", None)
    if original is None:
        return False

    if getattr(original, "_owui_tool_cap", False):
        # misc 已经包好了。middleware 若是之后才导入，它的
        # `from ... import convert_output_to_messages` 自然拿到包过的版本，
        # 不用再补；此时就算收工，钩子可以撤了。
        return True

    def wrapped(*args, **kwargs):
        messages = original(*args, **kwargs)
        try:
            return _cap_tool_messages(messages)
        except Exception:
            # 截断失败绝不能连带把请求搞挂：原样返回。
            log.exception("pypatch tool-output cap failed, passing through")
            return messages

    wrapped._owui_tool_cap = True
    wrapped.__name__ = getattr(original, "__name__", "convert_output_to_messages")
    wrapped.__doc__ = getattr(original, "__doc__", None)

    misc.convert_output_to_messages = wrapped
    patched = ["open_webui.utils.misc"]

    mw = sys.modules.get("open_webui.utils.middleware")
    if mw is not None and getattr(mw, "convert_output_to_messages", None) is original:
        mw.convert_output_to_messages = wrapped
        patched.append("open_webui.utils.middleware")

    log.info("pypatch tool-output cap installed on: %s", ", ".join(patched))
    # middleware 还没导入就先别收工，等它进来再补上它的那份引用。
    return mw is not None


# ---------------------------------------------------------------- 导入钩子

def _hook(name, *args, **kwargs):
    module = _real_import(name, *args, **kwargs)

    if not _done["feishu"]:
        cfg = sys.modules.get("open_webui.config")
        if cfg is not None:
            providers = getattr(cfg, "OAUTH_PROVIDERS", None)
            feishu = providers.get("feishu") if isinstance(providers, dict) else None
            if isinstance(feishu, dict):
                feishu.setdefault("name", os.getenv("OAUTH_PROVIDER_NAME") or "飞书")
                _done["feishu"] = True

    if not _done["toolcap"]:
        try:
            if _install_tool_cap():
                _done["toolcap"] = True
        except Exception:
            log.exception("pypatch tool-output cap install failed; giving up on it")
            _done["toolcap"] = True

    # 两件事都办完才撤钩子（原来的 feishu 补丁是补完就撤，现在要等齐）。
    if _done["feishu"] and _done["toolcap"]:
        builtins.__import__ = _real_import

    return module


builtins.__import__ = _hook
