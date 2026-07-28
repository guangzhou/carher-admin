# test_original_goal.py -- _original_goal() 必须取【最新】的用户问题
#
# 为什么需要:原实现正向扫描,取的是会话里**第一个**用户问题。
# 多问题会话中这会让 bridge 回答一个**过期的问题** —— 用户实测报告的现象。
#
# 真实会话证据(~/.codex/archived_sessions/rollout-2026-07-28T09-12-52-*.jsonl):
#     行  7  USER  "快速排序"
#     行 10  ASST  <快排讲解>            # 正确
#     行 26  USER  "本地磁盘大小"         # 新问题
#     行 28  CALL  df -h                 # 命令是对的!
#     行 33  ASST  <又讲了一遍快排>       # 错
# 工具调用是对的,说明模型理解了新问题;但 nudge 告诉它
# "ORIGINAL REQUEST: 快速排序",于是它在"判断是否已回答"那一轮回答了
# 我们塞给它的目标,而不是用户真正问的。**是我们覆盖了用户意图。**
#
# 跑: python3 testkit/test_original_goal.py

import os
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("ZK_KEY", "x")
spec = importlib.util.spec_from_file_location(
    "br", os.path.join(HERE, "..", "zerokey-codex-responses-bridge.py"))
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

g = m._original_goal
bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    if not ok:
        bad += 1
    print(("PASS  " if ok else "FAIL  ") + name
          + ("" if ok else "  got=%r want=%r" % (got, want)))


ENV = {"role": "user", "content": "<environment_context>\n <cwd>/x</cwd>\n</environment_context>"}
AGENTS = {"role": "user",
          "content": "# AGENTS.md instructions for /x\n<INSTRUCTIONS>\n环境工具提示"}
CALL = {"type": "function_call", "name": "exec", "call_id": "c1",
        "arguments": '{"command":["bash","-lc","df -h"]}'}
OUT = {"type": "function_call_output", "call_id": "c1", "output": "/dev/disk1 500G"}

# ⭐ 用户真实场景:两个问题,必须取第二个
chk("多问题取最新(真实回归场景)",
    g([AGENTS, ENV,
       {"role": "user", "content": "快速排序"},
       {"role": "assistant", "content": "快速排序是..."},
       {"role": "user", "content": "本地磁盘大小"}, CALL, OUT]),
    "本地磁盘大小")

# 三个问题
chk("三问题取最新",
    g([ENV, {"role": "user", "content": "Q1"},
       {"role": "assistant", "content": "A1"},
       {"role": "user", "content": "Q2"},
       {"role": "assistant", "content": "A2"},
       {"role": "user", "content": "Q3"}, CALL, OUT]),
    "Q3")

# 单问题不能退化
chk("单问题仍正确", g([ENV, {"role": "user", "content": "快速排序"}, CALL, OUT]),
    "快速排序")

# 我们自己注入的轮绝不能被当成用户目标
for inj in ("[ERROR] You did not use a tool in your previous response. Continue.",
            "[ERROR] You issued the same command twice and it did not produce usable output.",
            "The command output above is the ONLY new information you have."):
    chk("跳过我们注入的轮: %s..." % inj[:26],
        g([ENV, {"role": "user", "content": "真问题"}, CALL, OUT,
           {"role": "user", "content": inj}]),
        "真问题")

# 合成的历史块
chk("跳过 [result of ...]",
    g([ENV, {"role": "user", "content": "真问题"},
       {"role": "user", "content": "[result of df -h] ..."}]),
    "真问题")

# Codex 注入的 AGENTS.md 不是请求
chk("跳过 AGENTS.md 前言", g([AGENTS, ENV, {"role": "user", "content": "真问题"}]), "真问题")
chk("跳过 environment_context", g([ENV, {"role": "user", "content": "真问题"}]), "真问题")

# 边界
chk("字符串输入", g("直接一句话"), "直接一句话")
chk("空列表", g([]), "")
chk("None", g(None), "")
chk("全被过滤时返回空", g([AGENTS, ENV]), "")
chk("非 dict 元素不炸", g([None, 42, "x", {"role": "user", "content": "真问题"}]), "真问题")

print(("\n%d 条失败" % bad) if bad else "\n14/14 通过")
sys.exit(1 if bad else 0)
