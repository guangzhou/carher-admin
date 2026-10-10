"""responses_tool_args_done_backfill 回归 —— 必须在装了 litellm 1.103.2 的 pod 里跑（真迭代器，不 mock）。

    python tests/test_responses_tool_args_done_backfill.py          # 自带 runner，无需 pytest

阳性对照：每个「补发」场景先用**未打补丁**的原 chunk_parser 跑一遍，断言它确实丢参数，
否则这把尺子量不出补丁有没有用。逐字节不变场景：补丁前后 chunk 序列 JSON 全等。
"""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, "/app")

from litellm.completion_extras.litellm_responses_transformation import transformation as T  # noqa: E402

It = T.OpenAiResponsesToChatCompletionStreamIterator
ORIG = It.__dict__["chunk_parser"]
mod = importlib.import_module("responses_tool_args_done_backfill")
PATCHED = It.__dict__["chunk_parser"]
assert getattr(PATCHED, "_carher_args_backfill", False), "patch did not install"


def fc(i, name, args, typ="function_call"):
    it = {"type": typ, "id": "fc_%d" % i, "call_id": "call_%d" % i, "name": name, "status": "completed"}
    it["input" if typ == "custom_tool_call" else "arguments"] = args
    return it


def stream(calls, delta_for=(), with_args_done=True, with_item_done=True, reasoning_first=False, text=None):
    """calls: [(name, args, typ)]；delta_for: 哪些调用序号走 delta。"""
    ev = [{"type": "response.created", "response": {"id": "resp_t"}}]
    base = 0
    if reasoning_first:
        ev.append({"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_0", "summary": []}})
        ev.append({"type": "response.output_item.done", "output_index": 0, "item": {"type": "reasoning", "id": "rs_0", "summary": []}})
        base = 1
    out = []
    for j, (name, args, typ) in enumerate(calls):
        oi = base + j
        custom = typ == "custom_tool_call"
        ev.append({"type": "response.output_item.added", "output_index": oi, "item": fc(j, name, "", typ)})
        if j in delta_for:
            for k in range(0, len(args), 4):
                ev.append({"type": "response.custom_tool_call_input.delta" if custom else "response.function_call_arguments.delta",
                           "output_index": oi, "item_id": "fc_%d" % j, "delta": args[k:k + 4]})
        if with_args_done:
            d = {"type": "response.custom_tool_call_input.done" if custom else "response.function_call_arguments.done",
                 "output_index": oi, "item_id": "fc_%d" % j}
            d["input" if custom else "arguments"] = args
            ev.append(d)
        if with_item_done:
            ev.append({"type": "response.output_item.done", "output_index": oi, "item": fc(j, name, args, typ)})
        out.append(fc(j, name, args, typ))
    if text:
        oi = base + len(calls)
        ev.append({"type": "response.output_item.added", "output_index": oi, "item": {"type": "message", "id": "msg", "role": "assistant", "content": []}})
        ev.append({"type": "response.output_text.delta", "output_index": oi, "delta": text})
        ev.append({"type": "response.output_item.done", "output_index": oi, "item": {"type": "message", "id": "msg", "role": "assistant", "content": []}})
    ev.append({"type": "response.completed", "response": {"id": "resp_t", "output": out}})
    return ev


def run(events, parser, via_sse=True):
    """真路径：SSE 字符串 → __next__ → _handle_string_chunk → chunk_parser。返回 (chunks_json, calls, finish, text)。"""
    It.chunk_parser = parser
    try:
        if via_sse:
            lines = ["data: " + json.dumps(e) for e in events]
        else:
            # 另一条真路径：上游已解析成 BaseModel（_handle_string_chunk 走 model_dump()）
            from pydantic import BaseModel, ConfigDict

            class _Ev(BaseModel):
                model_config = ConfigDict(extra="allow")

            lines = [_Ev(**e) for e in events]
        it = It(streaming_response=iter(lines), sync_stream=True)
        chunks, calls, finish, text = [], {}, None, ""
        for ch in it:
            d = ch.model_dump() if hasattr(ch, "model_dump") else dict(ch)
            d.pop("created", None)
            chunks.append(json.dumps(d, sort_keys=True, default=str))
            for c in getattr(ch, "choices", None) or []:
                finish = c.finish_reason or finish
                text += getattr(c.delta, "content", None) or ""
                for tc in getattr(c.delta, "tool_calls", None) or []:
                    fn = tc.function
                    slot = calls.setdefault(tc.index, {"id": None, "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    slot["name"] += fn.name or ""
                    slot["args"] += fn.arguments or ""
        return chunks, calls, finish, text
    finally:
        It.chunk_parser = PATCHED


FAIL = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        FAIL.append(name)


SIX = [("get_weather", '{"city": "Tokyo"}', "function_call"), ("get_time", '{"city": "Tokyo"}', "function_call"),
       ("get_weather", '{"city": "Paris"}', "function_call"), ("get_time", '{"city": "Paris"}', "function_call"),
       ("get_weather", '{"city": "Berlin"}', "function_call"), ("get_time", '{"city": "Berlin"}', "function_call")]


def want_args(calls, spec):
    return [calls[i]["args"] for i in sorted(calls)] == [a for _, a, _ in spec]


# 1. 生产实测形状：6 路并行、零 delta、args.done + item.done
ev = stream(SIX)
_, c0, f0, _ = run(ev, ORIG)
check("1a 对照: 原函数确实丢参数", all(v["args"] == "" for v in c0.values()) and len(c0) == 6, c0)
_, c1, f1, _ = run(ev, PATCHED)
check("1b 补丁: 6 个参数完整且不重复", want_args(c1, SIX), c1)
check("1c 补丁: id/name 不变", [(v["id"], v["name"]) for v in c1.values()] == [(v["id"], v["name"]) for v in c0.values()])
check("1d finish_reason=tool_calls 保持", f1 == "tool_calls" == f0, (f0, f1))
check("1e 每个参数都是合法 JSON", all(json.loads(v["args"]) for v in c1.values()))

# 2. 全部走 delta ⇒ 补丁前后逐字节相同
ev = stream(SIX, delta_for=range(6))
a, _, _, _ = run(ev, ORIG)
b, cb, _, _ = run(ev, PATCHED)
check("2a 全 delta: 补丁前后 chunk 序列全等", a == b, (len(a), len(b)))
check("2b 全 delta: 参数完整", want_args(cb, SIX), cb)

# 3. 单调用 delta（最常见路径）逐字节相同
one = SIX[:1]
ev = stream(one, delta_for=[0])
a, _, _, _ = run(ev, ORIG)
b, _, _, _ = run(ev, PATCHED)
check("3 单调用 delta: 补丁前后全等", a == b)

# 4. 混合：0/2/4 有 delta，1/3/5 没有
ev = stream(SIX, delta_for=[0, 2, 4])
_, c0, _, _ = run(ev, ORIG)
check("4a 对照: 原函数丢掉无 delta 的 3 个", [i for i in sorted(c0) if not c0[i]["args"]] == [1, 3, 5], c0)
_, c4, _, _ = run(ev, PATCHED)
check("4b 混合: 6 个全对、有 delta 的不重复", want_args(c4, SIX), c4)

# 5. 只有 item.done（无 args.done）
ev = stream(SIX, with_args_done=False)
_, c0, _, _ = run(ev, ORIG)
check("5a 对照: 原函数丢参数", all(v["args"] == "" for v in c0.values()))
_, c5, _, _ = run(ev, PATCHED)
check("5b 只有 item.done: 补得回来", want_args(c5, SIX), c5)

# 6. 只有 args.done（无 item.done）
ev = stream(SIX, with_item_done=False)
_, c6, _, _ = run(ev, PATCHED)
check("6 只有 args.done: 补得回来", want_args(c6, SIX), c6)

# 7. reasoning 在 output_index 0 ⇒ tool_call index 仍从 0 连续
ev = stream(SIX, reasoning_first=True)
_, c7, _, _ = run(ev, PATCHED)
check("7a reasoning 前置: index 连续 0..5", sorted(c7) == list(range(6)), sorted(c7))
check("7b reasoning 前置: 参数完整", want_args(c7, SIX), c7)

# 8. custom_tool_call（apply_patch 类）只发 input.done
spec = [("apply_patch", "*** Begin Patch\n*** End Patch", "custom_tool_call"), ("exec", '{"cmd": "ls"}', "function_call")]
ev = stream(spec)
_, c0, _, _ = run(ev, ORIG)
_, c8, _, _ = run(ev, PATCHED)
check("8a 对照: 原函数 custom/function 都丢", all(v["args"] == "" for v in c0.values()), c0)
check("8b custom_tool_call input.done 补回", want_args(c8, spec), c8)

# 9. 纯文本回复：补丁前后全等
ev = stream([], text="hello world")
a, _, _, ta = run(ev, ORIG)
b, _, fb, tb = run(ev, PATCHED)
check("9 纯文本: 补丁前后全等", a == b and ta == tb == "hello world" and fb == "stop", (ta, tb, fb))

# 10. 工具调用 + 文本同回合
ev = stream(SIX[:2], text="ok")
_, c10, f10, t10 = run(ev, PATCHED)
check("10 工具+文本: 参数完整 & 文本在", want_args(c10, SIX[:2]) and t10 == "ok" and f10 == "tool_calls", (c10, t10, f10))

# 11. 空参数终态（零参工具 args="{}" 已由上游给出；真空串不应凭空造参数）
spec = [("get_goal", "{}", "function_call"), ("noop", "", "function_call")]
ev = stream(spec)
_, c11, _, _ = run(ev, PATCHED)
check("11 零参工具得 {}、真空串保持空（不造数据）", [c11[i]["args"] for i in sorted(c11)] == ["{}", ""], c11)

# 12. dict 直喂（非 SSE 字符串，BaseModel/dict 路径）
ev = stream(SIX)
_, c12, _, _ = run(ev, PATCHED, via_sse=False)
check("12 BaseModel 输入路径同样补回", want_args(c12, SIX), c12)

# 13. 两条流互不串状态（每个迭代器独立）
_, ca, _, _ = run(stream(SIX[:2]), PATCHED)
_, cb, _, _ = run(stream(SIX[2:4]), PATCHED)
check("13 流间状态隔离", want_args(ca, SIX[:2]) and want_args(cb, SIX[2:4]), (ca, cb))

# 14. 无状态调用方（guardrail handler 直调 translate）不受影响
d = T.OpenAiResponsesToChatCompletionStreamIterator.translate_responses_chunk_to_openai_stream(
    {"type": "response.output_item.done", "output_index": 0, "item": fc(0, "get_weather", '{"city": "Tokyo"}')})
got = d.choices[0].delta.tool_calls[0]
got_args = got["function"]["arguments"] if isinstance(got, dict) else got.function.arguments
check("14 无状态 translate 原样返回完整调用", got_args == '{"city": "Tokyo"}', got_args)

# 16. output_item.added 自带完整参数（部分上游/grok 形状）+ 无 delta ⇒ 原函数已发过，补丁不得重复
spec = SIX[:2]
ev = [{"type": "response.created", "response": {"id": "r"}}]
for j, (n, a, _) in enumerate(spec):
    ev.append({"type": "response.output_item.added", "output_index": j, "item": fc(j, n, a)})
    ev.append({"type": "response.function_call_arguments.done", "output_index": j, "item_id": "fc_%d" % j, "arguments": a})
    ev.append({"type": "response.output_item.done", "output_index": j, "item": fc(j, n, a)})
ev.append({"type": "response.completed", "response": {"id": "r", "output": [fc(j, n, a) for j, (n, a, _) in enumerate(spec)]}})
a, c0, _, _ = run(ev, ORIG)
b, c16, _, _ = run(ev, PATCHED)
check("16 added 自带参数: 不重复、补丁前后全等", a == b and want_args(c16, spec), (c0, c16))

# 17. delta 全发完但 done 里参数与拼接结果不同（上游纠正/截断）⇒ 补丁不改写，保持原行为
ev = stream(SIX[:1], delta_for=[0])
for e in ev:
    if e["type"] == "response.function_call_arguments.done":
        e["arguments"] = '{"city": "DIFFERENT"}'
a, _, _, _ = run(ev, ORIG)
b, _, _, _ = run(ev, PATCHED)
check("17 已有 delta 时 done 不同: 补丁不介入", a == b)

# 18. 上游重复发 args.done 和 item.done（两次终态）⇒ 只补一次
ev = stream(SIX[:3])
_, c18, _, _ = run(ev, PATCHED)
check("18 args.done+item.done 双终态只补一次", want_args(c18, SIX[:3]), c18)

# 19. 中途断流：只有 added + args.done，没有 completed ⇒ 补丁不抛异常、已补的参数在
ev = stream(SIX[:2])[:-1]
try:
    _, c19, _, _ = run(ev, PATCHED)
    check("19 无 completed 截断流: 不抛异常且参数在", want_args(c19, SIX[:2]), c19)
except Exception as exc:  # noqa: BLE001
    check("19 无 completed 截断流: 不抛异常且参数在", False, repr(exc))

# 20a. 缺 output_index ⇒ 补丁按 0 处理并补回
ev = [{"type": "response.created", "response": {"id": "r"}},
      {"type": "response.output_item.added", "item": fc(0, "get_weather", "")},
      {"type": "response.function_call_arguments.done", "item_id": "fc_0", "arguments": '{"city": "Tokyo"}'},
      {"type": "response.completed", "response": {"id": "r", "output": [fc(0, "get_weather", '{"city": "Tokyo"}')]}}]
try:
    _, c20, _, _ = run(ev, PATCHED)
    check("20a 缺 output_index: 不崩且补回", [c20[i]["args"] for i in sorted(c20)] == ['{"city": "Tokyo"}'], c20)
except Exception as exc:  # noqa: BLE001
    check("20a 缺 output_index: 不崩且补回", False, repr(exc))


# 20b. 畸形事件（item=None）：补丁行为必须与原函数一致（原函数自己就抛 AttributeError），不新增也不吞掉异常
def outcome(parser, events):
    try:
        return ("ok", run(events, parser)[0])
    except Exception as exc:  # noqa: BLE001
        return ("raise", type(exc).__name__)


ev = [{"type": "response.output_item.done", "item": None}]
check("20b item=None: 补丁与原函数同结果", outcome(ORIG, ev) == outcome(PATCHED, ev), (outcome(ORIG, ev), outcome(PATCHED, ev)))

# 15. 重复 import 不叠装
importlib.reload(mod)
check("15 reload 不叠装", getattr(It.__dict__["chunk_parser"], "__wrapped__", None) is ORIG)

print()
print("SELFTEST FAILED: %s" % FAIL if FAIL else "SELFTEST PASSED")
sys.exit(1 if FAIL else 0)
