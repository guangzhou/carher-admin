#!/usr/bin/env python3
"""cx_multi_question.py -- 多问题会话不能串题(用户实测报告的回归)

真实症状(~/.codex/archived_sessions/rollout-2026-07-28T09-12-52-*.jsonl):
用户先问"快速排序",再问"本地磁盘大小";bridge 正确跑了 df -h,
却又讲了一遍快速排序。真因是 _original_goal() 取会话里**第一个**用户问题,
nudge 把过期目标塞给模型 —— 我们覆盖了用户意图。

这个 case 走真实 codex 路径,离线单测见 test_original_goal.py。
跑: python3 testkit/cx_multi_question.py
"""
import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location("cx", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cx_e2e.py"))
cx=importlib.util.module_from_spec(spec); spec.loader.exec_module(cx)

inp=[cx.TOOLS_ITEM, cx.ENV_CTX, {"role":"user","content":"快速排序"}]
d=cx.post(inp); calls,t1=cx.extract(d)
print("  Q1 快速排序 -> 答案含'快速排序':", "快速排序" in t1 or "Quick" in t1)
inp.append({"role":"assistant","content":t1})
# 第二个问题
inp.append({"role":"user","content":"本地磁盘大小"})
for turn in range(5):
    d=cx.post(inp); calls,t2=cx.extract(d)
    if not calls: break
    for o,cmd in calls:
        out=cx.sh(cmd)
        print("     CMD:",cmd[:60],"->",out[:50].replace("\n"," "))
        typ=o.get("type"); cid=o.get("call_id") or o.get("id") or "c%d"%turn
        inp.append({"type":typ,"call_id":cid,"name":o.get("name") or "exec",
          **({"input":o.get("input")} if typ=="custom_tool_call" else {"arguments":o.get("arguments")})})
        inp.append({"type":"custom_tool_call_output" if typ=="custom_tool_call" else "function_call_output",
          "call_id":cid,"output":[{"type":"input_text","text":out}]})
print()
print("  Q2 本地磁盘大小 的答案:")
print("   ",(t2 or "")[:200].replace("\n"," "))
qs = ("快速排序" in (t2 or "")) or ("Quick" in (t2 or "")) or ("pivot" in (t2 or ""))
disk = any(k in (t2 or "") for k in ("Gi","GB","磁盘","disk","/dev/","Ti","容量"))
print()
print("  含快排内容(应为 False):",qs)
print("  含磁盘内容(应为 True): ",disk)
print("  == %s ==" % ("PASS 已修复" if (disk and not qs) else "FAIL 仍串题"))
sys.exit(0 if (disk and not qs) else 1)
