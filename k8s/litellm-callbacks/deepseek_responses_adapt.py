"""deepseek_responses_adapt.py — 让 deepseek 能接住「按 gpt 元数据构造」的请求。

背景
----
``deepseek-v4-flash`` 官方原生支持 Responses API 并适配 Codex，但那条路径要求
客户端加载 DeepSeek 的模型目录（``~/.codex/models.json``，``apply_patch_tool_type:
"freeform"``）。**兜底场景下不成立**：请求是客户端按 gpt-5.x 元数据构造的，
落到 deepseek 时形状对不上。2026-08-03 实测两处硬不兼容：

1. custom tool **只支持小写 ``apply_patch``**，其它名字一律
   ``400 Unsupported custom tool: 'X'. Only 'apply_patch' is supported.``
   （实测 ``ApplyPatch`` / ``applyPatch`` / ``apply-patch`` / ``shell`` /
   ``exec_command`` 全拒；与长度、前缀无关。）
2. deepseek 是 thinking 模型，历史里出现工具调用轮次时要求回传配套推理，
   且**格式必须是** ``reasoning.content:[{"type":"reasoning_text","text":...}]``。
   chatgpt 的 ``encrypted_content`` 形状不算，缺了就
   ``400 The `reasoning_text` in the thinking mode must be passed back to the API.``

本模块做出站适配（deepseek 部署专属）：
  * ``ApplyPatch`` 及变体 -> ``apply_patch``（工具定义 + 历史项同步改名）
  * 其它 custom tool -> 标准 function（单 string 参数 ``input``），
    与 ``codex_custom_tool_bridge`` 对 chat 目标的处理同构
  * **每一轮**工具调用的**第一条**调用项前若无合规 reasoning，补一个占位
    reasoning item；已有的 chatgpt encrypted reasoning 补上 ``content[]``
    并剥掉读不了的密文

离线验证（真打 api.deepseek.com，5 个真实 gpt 形状载荷）：转换前 2/5，转换后 5/5。

2026-08-04：并行工具调用被补 reasoning 打断（生产故障）
------------------------------------------------------
DeepSeek 用「**相邻**的 tool call 属于同一轮」来给 call / output 配对。同一轮的
两个 ``function_call`` 之间只要插进任何非 tool-call 的项（哪怕是它自己要求的
reasoning），后面的 ``function_call_output`` 就配不上前面那条 call，整单 400::

    No tool output found for tool call call_00_0WAoGDLwNnmouIVTqXki0916.

原实现的补 reasoning 判据是「前一项不是带 reasoning_text 的 reasoning 就补」，
对同一轮的第 2、3 条并行调用同样成立 —— 于是自己把 block 劈开了。

api.deepseek.com 单变量实测（``call_id`` 全部配对齐全，只动中间那一项）：

===================================================== ==========================
input 形状                                             结果
===================================================== ==========================
``[msg, R, fc1, fc2, fco1, fco2]``                    200 completed
``[msg, R, fc1, R, fc2, fco1, fco2]``                 **400 No tool output found**
``[msg, R, fc1, fco1, fc2, fco2]``                    400 reasoning_text 必须回传
``[msg, R, fc1, fco1, R, fc2, fco2]``                 200 completed
===================================================== ==========================

即：reasoning 要补在**每一轮的开头**，一轮内部一条都不能插。DeepSeek 一轮确实会
返回多条 ``function_call``（实测 ``['reasoning','function_call','function_call']``），
所以这条历史形状是常态而非边角料。

2026-08-05：``tool_choice`` 被当成 tagged enum 解析（生产故障）
--------------------------------------------------------------
生产 24h 内 ``deepseek-v4-flash-responses`` 只有一种错误，27 次，全是::

    400 Failed to deserialize the JSON body into the target type: tool_choice:
        unknown variant `auto`, expected one of `function`, `web_search`,
        `web_search_2025_08_26`, `custom`

DeepSeek 的 ``tool_choice`` 是 **serde 的双形态**：``auto`` / ``none`` 只接受
**裸字符串**；一旦写成对象 ``{"type": ...}``，就只走 tagged-enum 分支，只认
``function`` / ``web_search`` / ``web_search_2025_08_26`` / ``custom``。
错误里那句 "expected one of" 列的是**对象分支的 serde variant 名**，**不是
"能用的值"** —— 照字面去构造 ``{"type":"function"}`` / ``{"type":"custom"}``
反而撞另一堵墙（见下表 F/G/W1/W2）。这一点 2026-08-05 第一版补丁栽过：
把 ``custom`` 当"内建 tag"放行，Codex 真载荷（``tool_choice``
``{"type":"custom","name":"shell"}``）当场 400。

api.deepseek.com 单变量实测（2026-08-05，其余字段全同）：

===================================================== ==========================
``tool_choice``                                        结果
===================================================== ==========================
（不带）                                               200，出 function_call
``"auto"``                                            200，出 function_call
``"none"``                                            200，只出 reasoning
``"required"``                                        400 Thinking mode 不支持
``{"type": "auto"}``                                  **400 unknown variant**（生产真凶）
``{"type": "none"}``                                  400 unknown variant
``{"type": "tool"}`` / ``{"type": "required"}``       400 unknown variant
``{"type":"function","name":"X"}``                    400 Thinking mode 不支持
``{"type":"function","function":{"name":"X"}}``       400 missing field ``name``
``{"type":"allowed_tools", ...}``                     400 unknown variant
W1 ``{"type":"custom","name":"shell"}``               400 Unsupported custom tool
W2 ``{"type":"custom","name":"apply_patch"}``         400 Thinking mode 不支持
W3 ``{"type":"web_search"}``，tools 里**没有** ws     400 no web_search tool specified
W4 ``{"type":"web_search"}``，tools 里**有** ws       **200**
W5 ``{"type":"web_search_2025_08_26"}`` + ws tool     **200**
===================================================== ==========================

穿过 198 proxy 打同一组载荷，字符串 200 / 对象 400，错误文本与生产日志逐字一致
—— 即**网关原样透传，是客户端发的对象形状**（对照
``deepseek_responses_adapt ... counts={} (no-op)`` 紧跟 400，网关没动过手）。

结论：thinking 模式下**唯一活着的强制形态是 ``web_search``，且 ``tools`` 里必须
真有这个工具**；对 function / custom 的强制一概不支持。所以本模块把
``tool_choice`` 归一为：裸 ``auto`` / 裸 ``none`` / 有依托的 web_search 强制 /
不带，其余一律降级为不带（= 默认 auto）—— 宁可少一个约束，也不把 400 甩给
用户，该 model group 没有 fallback，400 直吐客户端。

作用域
------
``_is_deepseek()`` 只认 model 名里含 ``deepseek`` 的部署。其它任何模型（chatgpt
池 / wangsu / anthropic / zerokey）**结构上不进入任何转换**，第一行就 return。

未覆盖（诚实标注）
------------------
* 占位 reasoning 文本是常量，不是真实推理内容 —— 换模型兜底时上一轮的思考
  本来就拿不到（chatgpt 的是加密的）。对模型是轻微上下文损失，换请求能活。
* 入站不做反向还原：custom tool 被转成 function 后，模型回的是 ``function_call``。
  客户端（Codex）声明的是 custom，可能不认这个形状。**兜底一轮能出结果，
  但带 apply_patch 的多轮编辑链路未用真客户端闭环验证。**
"""
from __future__ import annotations

import json
import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("deepseek_responses_adapt")

# 「一条工具调用」的全部类型 —— 并行块判定全靠它，漏一种就判瞎。
# 2026-08-05：原来只认前两种，对照 sub2api（Wei-Shaw/sub2api，
# backend/internal/service/openai_codex_transform.go 的
# isCodexToolCallInputType）补齐到 6 种。Codex/Cursor 真实会发
# local_shell_call / mcp_tool_call —— 这些落在并行块里时，旧判据认不出是
# tool call，于是把后面的调用当成新一轮、在中间补 reasoning，劈开并行块。
_TOOL_CALL_TYPES = (
    "function_call",
    "custom_tool_call",
    "tool_call",
    "local_shell_call",
    "tool_search_call",
    "mcp_tool_call",
)

# 对应的 output 类型，配对时同样不能漏。
_TOOL_OUTPUT_TYPES = (
    "function_call_output",
    "custom_tool_call_output",
    "mcp_tool_call_output",
    "tool_search_output",
    "local_shell_call_output",
)

_APPLY_PATCH_CANON = "apply_patch"
_PLACEHOLDER = "(上一轮推理在跨模型兜底时不可用)"
_RESPONSE_CALL_TYPES = {"responses", "aresponses", "_aresponses_websocket"}

# 对象形态里唯一能活的 tag：强制 web_search。**但有前提** —— ``tools`` 里必须真的
# 有 web_search 工具，否则 400 "no web_search tool was specified"。
# ``custom`` 虽然出现在 DeepSeek 的错误文本 "expected one of" 里，却是**死的**
# （见模块头 W1/W2 两格）—— 那串列表是 serde 的 variant 名，不是「能用的值」。
_WEB_SEARCH_TAGS = {"web_search", "web_search_2025_08_26"}
_SENTINEL = object()


def _is_responses_call(call_type: Any) -> bool:
    """必须取 ``.value``，不能用 ``str(call_type)``。

    ``CallTypes`` 是 ``(str, Enum)``，Python 3.13 下
    ``str(CallTypes.aresponses) == 'CallTypes.aresponses'``，
    所以 ``str(call_type) in {"responses","aresponses"}`` **永远是 False**。
    2026-08-03 实测：照抄 ``chatgpt_responses_normalize`` 的
    ``str(call_type) in ...`` 写法后，pre_call / pre_deployment 两个钩子
    零触发（连 no-op 日志都打不出来），因为门控在日志之前。
    该文件同样的写法也是废的，它实际靠 pre_routing 的 model 门控工作。
    """
    v = getattr(call_type, "value", call_type)
    return str(v) in _RESPONSE_CALL_TYPES


def _norm(s: Any) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _is_apply_patch(name: Any) -> bool:
    return "applypatch" in _norm(name)


def _is_deepseek(model: Any) -> bool:
    return isinstance(model, str) and "deepseek" in model.lower()


def _has_reasoning_text(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return False
    content = item.get("content")
    return isinstance(content, list) and any(
        isinstance(p, dict) and p.get("type") == "reasoning_text" for p in content
    )


def _placeholder_reasoning() -> dict[str, Any]:
    return {"type": "reasoning", "summary": [],
            "content": [{"type": "reasoning_text", "text": _PLACEHOLDER}]}


def _item_type(item: Any) -> Any:
    return item.get("type") if isinstance(item, dict) else None


def _is_intra_turn_filler(item: Any) -> bool:
    """轮内「不打断并行块」的项：assistant message / reasoning。

    DeepSeek 按相邻关系配对，但 Codex 一轮的真实输出是
    ``['reasoning','message','function_call','function_call']`` —— message 和
    并行调用混在同一轮里。判断某条 call 是否「开启新一轮」时必须跳过这些项，
    否则会把同一轮的第 2 条调用误判成新一轮、在它前面补 reasoning，
    结果劈开并行块 -> 400 No tool output found。
    """
    if not isinstance(item, dict):
        return False
    if item.get("type") == "reasoning":
        return True
    return item.get("type") is None and item.get("role") == "assistant"


def _opens_new_turn(out: list[Any]) -> bool:
    """当前位置要不要补 reasoning：往回跳过轮内填充项再看。

    - 回溯遇到 tool call  -> 同一轮的并行调用，**不能补**
    - 回溯遇到带 reasoning_text 的 reasoning -> 本轮开头已合规，不用补
    - 其它（用户消息 / 空 / tool output）-> 确实是新一轮，要补
    """
    for item in reversed(out):
        t = _item_type(item)
        if t in _TOOL_CALL_TYPES:
            return False
        if t == "reasoning":
            return not _has_reasoning_text(item)
        if _is_intra_turn_filler(item):
            continue
        return True
    return True


def _drop_reasoning_between_calls(items: list[Any], counts: dict[str, int]) -> list[Any]:
    """删掉夹在同一轮并行调用之间的 reasoning / assistant message。

    DeepSeek 靠相邻关系配对，同一轮的两条 ``function_call`` 之间插进**任何**
    非 tool-call 的项，后面的 output 就配不上前面那条 call，整单 400。

    2026-08-05 单变量实测（call_id 全配对，只动并行块中间那一项）::

        [R, fc1, fc2, fo1, fo2]        -> 200
        [R, fc1, MSG, fc2, fo1, fo2]   -> 400 No tool output found   <- 本次真凶
        [R, fc1, R,   fc2, fo1, fo2]   -> 400 No tool output found
        [R, MSG, fc1, fc2, fo1, fo2]   -> 200   （轮开头，合法）
        [MSG, R, fc1, fc2, fo1, fo2]   -> 200

    即 message 与 reasoning 同罪 —— 只要落在并行块**中间**就致命，落在轮
    **开头**无害。原实现只删 reasoning、且判据是「前后紧邻」，认不出
    Codex 真实形状（一轮输出是 ``reasoning, message, fc, fc``，
    message 会夹进并行块）。

    是否真的发生看 counts 里有没有 ``reasoning_between_calls_drop`` /
    ``message_between_calls_drop`` —— 不靠猜。
    """
    def _neighbor_is_call(seq: list[Any]) -> bool:
        for it in seq:
            if _item_type(it) in _TOOL_CALL_TYPES:
                return True
            if _is_intra_turn_filler(it):
                continue
            return False
        return False

    out: list[Any] = []
    for idx, item in enumerate(items):
        if (_is_intra_turn_filler(item)
                and _neighbor_is_call(list(reversed(items[:idx])))
                and _neighbor_is_call(items[idx + 1:])):
            key = ("reasoning_between_calls_drop"
                   if _item_type(item) == "reasoning" else "message_between_calls_drop")
            counts[key] = counts.get(key, 0) + 1
            continue
        out.append(item)
    return out


def _has_web_search_tool(tools: Any) -> bool:
    """``tools`` 里有没有 web_search —— 决定 web_search 强制能不能留。"""
    if not isinstance(tools, list):
        return False
    return any(isinstance(t, dict) and t.get("type") in _WEB_SEARCH_TAGS for t in tools)


def _adapt_tool_choice(choice: Any, tools: Any, counts: dict[str, int]) -> Any:
    """归一到 DeepSeek thinking 模式真正接受的形态。

    返回 ``_SENTINEL`` 表示「把这个字段整个删掉」（等价于默认 auto）。
    形状表见模块头 2026-08-05 一节，每一格都是真打 api.deepseek.com 测出来的。
    """
    # 裸字符串：auto / none 直通；required 及其它一律降级为不带
    if isinstance(choice, str):
        if choice in ("auto", "none"):
            return choice
        # "required" 实测 400 Thinking mode does not support this tool_choice
        counts["tool_choice_force_dropped"] = counts.get("tool_choice_force_dropped", 0) + 1
        return _SENTINEL

    if not isinstance(choice, dict):
        return choice

    tag = choice.get("type")

    # {"type":"auto"} / {"type":"none"} —— 生产真凶，拆成裸字符串
    if tag in ("auto", "none"):
        counts["tool_choice_unwrapped"] = counts.get("tool_choice_unwrapped", 0) + 1
        return tag

    # 强制 web_search 是唯一活着的强制形态，但 tools 里必须真有这个工具，
    # 否则 400 "no web_search tool was specified in the 'tools' parameter"。
    if tag in _WEB_SEARCH_TAGS:
        if _has_web_search_tool(tools):
            return choice
        counts["tool_choice_web_search_unbacked_dropped"] = (
            counts.get("tool_choice_web_search_unbacked_dropped", 0) + 1)
        return _SENTINEL

    # 其余全是「强制用某个/某类工具」：{"type":"function",...}、{"type":"custom",...}、
    # {"type":"tool"}、{"type":"required"}、{"type":"allowed_tools",...}。
    # thinking 模式对 function / custom 一概不支持（function 与 custom 是
    # 400 Thinking mode，其它是 400 unknown variant），只能降级为不带 ——
    # 少一个约束，好过整单 400 直吐用户。
    counts["tool_choice_force_dropped"] = counts.get("tool_choice_force_dropped", 0) + 1
    return _SENTINEL


# _adapt 一次调用内，被从 custom 降级成 function 的工具名。
# key 用 counts 的 id —— 同一次 _adapt 内 counts 是同一个对象，天然隔离；
# _adapt 末尾会把它转存进 litellm_metadata 再清掉，不留全局状态。
_DOWNGRADED_NAMES: "dict[int, set]" = {}


def _adapt_tools(tools: Any, counts: dict[str, int]) -> Any:
    if not isinstance(tools, list):
        return tools
    out: list[Any] = []
    seen_apply_patch = False
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "custom":
            out.append(tool)
            continue
        name = tool.get("name")
        if name == _APPLY_PATCH_CANON:
            seen_apply_patch = True
            out.append(tool)
            continue
        if _is_apply_patch(name):
            if seen_apply_patch:
                counts["custom_dup_apply_patch_drop"] = counts.get("custom_dup_apply_patch_drop", 0) + 1
                continue
            renamed = dict(tool)
            renamed["name"] = _APPLY_PATCH_CANON
            seen_apply_patch = True
            counts["apply_patch_rename"] = counts.get("apply_patch_rename", 0) + 1
            out.append(renamed)
            continue
        # deepseek 不接受任意 custom tool -> 转标准 function
        # 记下降级过的名字：出站要按客户端原始声明还原成 custom_tool_call，
        # 否则 Codex 认不出这条调用（表现为「命令执行被中断」）。
        _DOWNGRADED_NAMES.setdefault(id(counts), set()).add(name)
        out.append({
            "type": "function",
            "name": name,
            "description": tool.get("description") or (name if isinstance(name, str) else "tool"),
            "parameters": {"type": "object",
                           "properties": {"input": {"type": "string"}},
                           "required": ["input"]},
        })
        counts["custom_to_function"] = counts.get("custom_to_function", 0) + 1
    return out


def _adapt_input(items: Any, counts: dict[str, int]) -> Any:
    if not isinstance(items, list):
        return items
    items = _drop_reasoning_between_calls(items, counts)
    out: list[Any] = []
    # 保持 custom 形态（= apply_patch）的 call_id，供 output 配对时判断类型。
    kept_custom_call_ids: set[Any] = set()
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        typ = item.get("type")

        if typ == "reasoning":
            fixed = dict(item)
            if not _has_reasoning_text(fixed):
                fixed["content"] = [{"type": "reasoning_text", "text": _PLACEHOLDER}]
                counts["reasoning_content_fill"] = counts.get("reasoning_content_fill", 0) + 1
            if fixed.pop("encrypted_content", None) is not None:
                counts["foreign_encrypted_strip"] = counts.get("foreign_encrypted_strip", 0) + 1
            out.append(fixed)
            continue

        if typ in _TOOL_CALL_TYPES:
            # 只有当这条调用**开启新一轮** assistant 输出时才补 reasoning。
            # 前一项已经是 tool call = 同一轮的并行调用，中间插任何东西都会让
            # DeepSeek 认为该轮结束 -> 后面的 output 配不上 -> 400
            # "No tool output found for tool call <call_id>"（2026-08-04 生产故障，
            # 单变量实测见模块头形状表）。
            if _opens_new_turn(out):
                out.append(_placeholder_reasoning())
                counts["reasoning_insert"] = counts.get("reasoning_insert", 0) + 1
            call = dict(item)
            name = call.get("name")
            if typ == "custom_tool_call" and name != _APPLY_PATCH_CANON:
                if _is_apply_patch(name):
                    call["name"] = _APPLY_PATCH_CANON
                    counts["apply_patch_rename"] = counts.get("apply_patch_rename", 0) + 1
                    kept_custom_call_ids.add(call.get("call_id"))
                else:
                    call = {"type": "function_call", "id": call.get("id"),
                            "call_id": call.get("call_id"), "name": name,
                            "arguments": json.dumps({"input": call.get("input") or ""},
                                                    ensure_ascii=False)}
                    counts["custom_call_to_function"] = counts.get("custom_call_to_function", 0) + 1
            elif typ == "custom_tool_call":
                # 官方形状：name 就是 apply_patch，原样放行
                kept_custom_call_ids.add(call.get("call_id"))
            out.append(call)
            continue

        if typ == "custom_tool_call_output":
            # 配对判据：**这条 output 对应的 call 有没有被转成 function_call**。
            # 以前这里无条件转 function_call_output —— 对官方形状载荷来说，
            # call 是 custom_tool_call（apply_patch 放行了）、output 却成了
            # function_call_output，一对调用被拆成两种类型（2026-08-05 A/B
            # 实测：官方形状穿 198 时 counts={'custom_out_to_function': 1}，
            # 直连对照组则原样保留）。
            if item.get("call_id") in kept_custom_call_ids:
                out.append(item)
                continue
            out.append({"type": "function_call_output", "call_id": item.get("call_id"),
                        "output": item.get("output") or ""})
            counts["custom_out_to_function"] = counts.get("custom_out_to_function", 0) + 1
            continue

        out.append(item)
    return out


def _is_additional_tools_item(item: Any) -> bool:
    """Codex "responses lite" 的工具声明项。

    形状（实测 2026-08-06 生产抓包）::

        {"type": "additional_tools", "role": "developer",
         "tools": [{"type":"custom","name":"exec", ...},
                   {"type":"function","name":"wait", ...}]}
    """
    return isinstance(item, dict) and item.get("type") == "additional_tools"


def _flatten_tool_entries(tools: Any) -> list[Any]:
    """展开工具列表；``namespace`` 类型把嵌套的 tools 摊平。

    上游 PR #33228 的测试里出现 ``{"type":"namespace","name":"collaboration",
    "tools":[...]}`` —— 不摊平就整包丢给 DeepSeek，它不认这个 type。
    """
    out: list[Any] = []
    if not isinstance(tools, list):
        return out
    for t in tools:
        if isinstance(t, dict) and t.get("type") == "namespace":
            out.extend(_flatten_tool_entries(t.get("tools")))
        else:
            out.append(t)
    return out


def _hoist_additional_tools(data: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    """把 ``input`` 里的 ``additional_tools`` 项提升为顶层 ``tools``。

    2026-08-06 生产故障（**本条是真凶**）：用户本地 Codex 打
    ``deepseek-v4-flash-responses``，输出里出现裸文本工具调用::

        <｜｜DSML｜｜tool_calls>
        <｜｜DSML｜｜invoke name="exec">

    抓包判据 —— 同一时段 39 个 deepseek 请求，38 个正常、只有他那一条异常::

        正常:  tools=['function/execute_shell_command', ...]  tool_choice=None
        异常:  tools=[]  tool_choice='auto'  seq_in=['additional_tools', ...]
               DSAT itemkeys=['role','tools','type'] n=3
                    names=['custom/exec','function/wait','function/request_user_input']

    即 **Codex 的 "responses lite" 线路把工具塞在 input 里，顶层 tools 是空的**。
    LiteLLM 1.90.2 全库 grep ``additional_tools`` 为 0 处，原样透传；DeepSeek
    也不认这个 input item，于是**收到零个工具**。

    模型想调工具却无工具可用 -> 把调用**写成文本**。单变量实测（只改
    ``tools`` 是否为空，其余全同，打 api.deepseek.com 3 次）::

        tools=[]        -> 第 2 次直接吐出 <｜｜DSML｜｜invoke name="list_files">
        tools=[真工具]  -> 5/5 干净，全部结构化 function_call

    这同时解释了「模型编造不存在的工具名」（``list_files`` / ``exec_command``
    每次都不一样）—— 没有工具声明可依据，只能瞎编，不是历史污染。

    做法与上游 PR #33228（``bedrock_mantle`` 侧同一问题）一致：摘出 items ->
    工具提到顶层 -> 从 input 删掉这些项。上游只在 bedrock_mantle 做了，
    **openai provider（deepseek 走这条）没有**，所以升级 LiteLLM 也修不了。
    """
    items = data.get("input")
    if not isinstance(items, list):
        return data
    at_items = [i for i in items if _is_additional_tools_item(i)]
    if not at_items:
        return data

    hoisted: list[Any] = []
    for it in at_items:
        hoisted.extend(_flatten_tool_entries(it.get("tools")))
    if not hoisted:
        # 空的 additional_tools 项照样要删 —— DeepSeek 不认这个 item type
        counts["additional_tools_empty_dropped"] = (
            counts.get("additional_tools_empty_dropped", 0) + len(at_items))
        data = dict(data)
        data["input"] = [i for i in items if not _is_additional_tools_item(i)]
        return data

    out = dict(data)
    out["input"] = [i for i in items if not _is_additional_tools_item(i)]
    existing = out.get("tools")
    existing = list(existing) if isinstance(existing, list) else []
    # 顶层已有同名工具时不重复添加（Codex 两种形态混发的兜底）
    seen = {t.get("name") for t in existing if isinstance(t, dict)}
    added = [t for t in hoisted
             if not (isinstance(t, dict) and t.get("name") in seen)]
    out["tools"] = existing + added
    counts["additional_tools_hoisted"] = counts.get("additional_tools_hoisted", 0) + len(added)
    return out


def _adapt(data: dict[str, Any], source: str) -> dict[str, Any]:
    if not _is_deepseek(data.get("model")):
        return data
    counts: dict[str, int] = {}
    # 必须最先做：先把 additional_tools 提上来，后面的 tools 改写才看得到它们
    data = _hoist_additional_tools(data, counts)
    out = dict(data)
    tools = _adapt_tools(out.get("tools"), counts)
    if tools is not out.get("tools"):
        out["tools"] = tools
    items = _adapt_input(out.get("input"), counts)
    if items is not out.get("input"):
        out["input"] = items
    if "tool_choice" in out:
        # 用**转换后**的 tools 判断 web_search 是否有依托：_adapt_tools 会动
        # custom 工具，虽然目前不碰 web_search，但判据必须跟出站载荷一致。
        tc = _adapt_tool_choice(out["tool_choice"], out.get("tools"), counts)
        if tc is _SENTINEL:
            out.pop("tool_choice")
        elif tc is not out["tool_choice"]:
            out["tool_choice"] = tc
    if not counts:
        # 必须留这行:否则无法区分「钩子没被调用」和「调用了但无需转换」。
        # 2026-08-03 就是因为静默 no-op,把「钩子选错、兜底路径 0 触发」误读成
        # 「转换生效了」。deepseek 兜底流量不大,这条日志量可接受。
        _DOWNGRADED_NAMES.pop(id(counts), None)
        _log.warning("deepseek_responses_adapt: source=%s model=%s counts={} (no-op)", source, data.get("model"))
        return data
    # 把降级过的名字传给出站还原侧（deepseek_id_prefix）。
    # litellm_metadata 会跟着请求走到流式迭代器，是这两层之间唯一可靠的通道
    # —— 2026-08-06 实测 optional_params/litellm_params 里 tools 已是转换后的
    # 形态，input 里的 additional_tools 也已被 hoist 掉，下游无从还原。
    downgraded = _DOWNGRADED_NAMES.pop(id(counts), None)
    if downgraded:
        meta = out.get("litellm_metadata")
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta["deepseek_downgraded_custom_tools"] = sorted(downgraded)
        out["litellm_metadata"] = meta
    _log.warning("deepseek_responses_adapt: source=%s model=%s counts=%s", source, data.get("model"), counts)
    return out


class DeepSeekResponsesAdapt(CustomLogger):
    """三个钩子都挂，缺一不可。

    ``async_pre_call_hook`` 只在请求入口跑一次，那时 ``model`` 还是原始 model
    group（兜底场景下是 ``gpt-5.6-sol``），被 ``_is_deepseek`` 门控挡掉 —— 等
    router fallback 换到 deepseek 时它不会再执行。2026-08-03 实测：只挂入口钩子
    时兜底链上转换 0 次触发，``ApplyPatch`` 照旧 400。
    ``async_pre_call_deployment_hook`` 在**选定 deployment 之后、发请求之前**跑，
    fallback 换 deployment 会再跑一次，是兜底路径唯一可靠的挂载点
    （与 ``chatgpt_responses_normalize`` 同构）。
    """

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str) -> Any:
        try:
            if _is_responses_call(call_type) and isinstance(data, dict):
                return _adapt(data, "pre_call:%s" % call_type)
        except Exception as exc:
            _log.warning("deepseek_responses_adapt: pre_call error: %r", exc)
        return data

    async def async_pre_call_deployment_hook(self, kwargs: dict[str, Any], call_type: Any) -> Any:
        """兜底路径的关键钩子：此时 kwargs['model'] 已是实际 deployment。"""
        try:
            if _is_responses_call(call_type) and isinstance(kwargs, dict):
                return _adapt(kwargs, "pre_deployment:%s" % call_type)
        except Exception as exc:
            _log.warning("deepseek_responses_adapt: pre_deployment error: %r", exc)
        return kwargs

    async def async_pre_routing_hook(self, model: str, request_kwargs: dict, messages: Any = None,
                                     input: Any = None, specific_deployment: bool = False) -> Any:
        try:
            if _is_deepseek(model) and isinstance(request_kwargs, dict):
                rk = dict(request_kwargs)
                rk.setdefault("model", model)
                if "input" not in rk and input is not None:
                    rk["input"] = input
                return _adapt(rk, "pre_routing")
        except Exception as exc:
            _log.warning("deepseek_responses_adapt: pre_routing error: %r", exc)
        return request_kwargs


deepseek_responses_adapt_instance = DeepSeekResponsesAdapt()
deepseek_responses_adapt = deepseek_responses_adapt_instance
