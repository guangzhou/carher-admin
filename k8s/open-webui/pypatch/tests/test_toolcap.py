#!/usr/bin/env python3
"""leg2 截断逻辑的回归测试（纯函数，不碰 OWUI、不碰网络）。

第 0 步是阳性对照：先证明这套断言「能」抓到改之前那两个 bug，
否则全绿说明不了任何事。
"""
import importlib.util
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "sitecustomize.py"

spec = importlib.util.spec_from_file_location("owui_patch_undertest", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

cap = mod._cap_tool_messages
clen = mod._content_len

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def tool(text):
    return {"role": "tool", "tool_call_id": "c1", "content": text}


def tool_parts(*texts):
    return {
        "role": "tool",
        "tool_call_id": "c1",
        "content": [{"type": "input_text", "text": t} for t in texts],
    }


PER = 60000
TOTAL = 240000
FLOOR = 2000

print("=== Bug 1：多模态 part 列表必须共享一条消息的额度 ===")
msgs = cap([tool_parts("A" * 50000, "B" * 50000, "C" * 50000)])
got = clen(msgs[0]["content"])
check("3 个 part 合计不超过 per",
      got <= PER + 400,  # 容忍截断标记本身的字符
      f"实测 {got}，上限 {PER}")
check("part 结构没被删",
      isinstance(msgs[0]["content"], list) and len(msgs[0]["content"]) == 3,
      f"part 数 {len(msgs[0]['content'])}")

print()
print("=== Bug 2：总预算必须收敛，最新一条也要参与 ===")
# 4 条各 100k，单条截到 60k 后合计 240k，正好卡在预算上；再加一条就必须压
many = [tool("X" * 100000) for _ in range(6)]
msgs = cap(many)
total = sum(clen(m["content"]) for m in msgs)
check("6 条 100k 压完不超过总预算",
      total <= TOTAL + 2000,
      f"实测合计 {total}，预算 {TOTAL}")
check("最新一条保留得比旧的多",
      clen(msgs[-1]["content"]) > clen(msgs[0]["content"]),
      f"最新 {clen(msgs[-1]['content'])} vs 最旧 {clen(msgs[0]['content'])}")

# N 小的情况：2 条巨大的，旧的压到地板也不够，最新那条必须也被压
two = [tool("Y" * 400000), tool("Z" * 400000)]
msgs = cap(two)
total2 = sum(clen(m["content"]) for m in msgs)
check("只有 2 条巨型输出时也收敛",
      total2 <= TOTAL + 2000,
      f"实测合计 {total2}")

print()
print("=== 不变量：绝不删消息、绝不动非 tool 消息 ===")
mixed = [
    {"role": "system", "content": "S" * 100000},
    {"role": "user", "content": "U" * 100000},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
    tool("T" * 300000),
]
out = cap(list(mixed))
check("消息条数不变", len(out) == 4, f"{len(out)} 条")
check("system 未被截断", clen(out[0]["content"]) == 100000)
check("user 未被截断", clen(out[1]["content"]) == 100000)
check("tool_call_id 保留", out[3].get("tool_call_id") == "c1")
check("tool 被截断", clen(out[3]["content"]) <= PER + 400,
      f"{clen(out[3]['content'])}")

print()
print("=== 小输出不该被动 ===")
small = [tool("hello")]
out = cap(small)
check("短内容原样通过", out[0]["content"] == "hello")

print()
print("=== 没有 tool 消息时原样返回 ===")
none_tool = [{"role": "user", "content": "hi"}]
out = cap(list(none_tool))
check("无 tool 消息不改动", out == none_tool)

print()
print("=== 截断痕迹本身也要算在预算里（09-19 fuzz 抓到的）===")
# 原来 _middle_truncate 是 head+tail 吃满 limit 再额外拼 marker，每截一次就
# 超一个 marker；多 part 消息里每个耗尽的壳同样是纯超支，实测到 limit+72。
mt = mod._middle_truncate
over = [
    (lim, n, len(mt("x" * n, lim)))
    for lim in (1, 10, 39, 40, 100, 2000, PER)
    for n in (0, 1, lim, lim + 1, lim * 3, 500000)
    if len(mt("x" * n, lim)) > lim
]
check("_middle_truncate 任何 limit 下都不超", not over, f"{over[:3]}")

parts5 = cap([tool_parts(*["y" * 100000] * 5)])
check("5 part 消息总长不超 per", clen(parts5[0]["content"]) <= PER,
      f"{clen(parts5[0]['content'])}")

many = cap([tool("z" * 400000) for _ in range(12)] )
tot = sum(clen(m["content"]) for m in many if m["role"] == "tool")
check("12 条 400k 总长不超 total", tot <= TOTAL, f"{tot}")

print()
if fails:
    print(f"FAILED: {len(fails)} 项 — {fails}")
    sys.exit(1)
print("全部通过")
sys.exit(0)
