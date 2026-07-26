#!/usr/bin/env python3
"""Run N independent agent loops CONCURRENTLY and report a pass rate.

Serial trials cost ~60-90s each, which is too slow to measure a rate with any
confidence. These loops are independent, so run them in parallel and classify
each outcome:

  ANSWERED  -- ended in text that actually contains information from the task
  BRAKED    -- ended in one of the bridge's own brake messages (a real defect:
               work was done and then discarded)
  REFUSED   -- ended in the model declining / handing the job back
  NOCMD     -- never issued a single command
"""
import json, os, re, subprocess, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentloop

BRAKE_RE = re.compile(r"I stopped after repeating|already ran successfully earlier")
REFUSE_RE = re.compile(
    r"没有可用|没有可调用|不能直接|无法直接|贴给我|你可以在|请在|不存在|"
    r"No such file or directory|未能|没有拿到|无法进入")


def classify(trace, expect):
    ncmd = sum(1 for k, _ in trace if k == "CMD")
    last_kind, last_val = trace[-1] if trace else ("none", "")
    if last_kind == "ERROR":
        return "ERROR", ncmd
    if last_kind != "TEXT":
        return "NOEND", ncmd
    if BRAKE_RE.search(last_val):
        return "BRAKED", ncmd
    # A real answer must contain at least one expected token from the task's
    # actual data -- otherwise "I looked at it" would count as success.
    if expect and any(tok in last_val for tok in expect):
        return "ANSWERED", ncmd
    if REFUSE_RE.search(last_val):
        return "REFUSED", ncmd
    if ncmd == 0:
        return "NOCMD", ncmd
    return "VAGUE", ncmd


def trial(args):
    task, expect, mt = args
    try:
        tr = agentloop.loop(task, max_turns=mt, verbose=False)
    except Exception as e:
        return ("ERROR", 0, str(e)[:60])
    kind, ncmd = classify(tr, expect)
    tail = tr[-1][1][:70].replace("\n", " ") if tr else ""
    return (kind, ncmd, tail)


def run(task, expect, n=6, mt=10, label=""):
    with ThreadPoolExecutor(max_workers=n) as ex:
        res = list(ex.map(trial, [(task, expect, mt)] * n))
    c = Counter(k for k, _, _ in res)
    good = c["ANSWERED"]
    print("  %-26s ANSWERED=%d/%d  BRAKED=%d REFUSED=%d NOCMD=%d VAGUE=%d ERROR=%d"
          % (label or "task", good, n, c["BRAKED"], c["REFUSED"], c["NOCMD"],
             c["VAGUE"], c["ERROR"]))
    for k, ncmd, tail in res:
        print("      %-9s cmds=%d %s" % (k, ncmd, tail))
    return c


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    DOC = ("帮我看看这个飞书文档 "
           "https://t83dfrspj4.feishu.cn/docx/Cj7OdSqgjoV5Ldxr9dKcKK6BnOa 用 lark-cli 搞定")
    run(DOC, ["95.1", "1380", "203", "67"], n=n, label="feishu doc (lark-cli)")

# --- additional workloads, to check the fixes generalize beyond the one task
# they were tuned against ---
def suite(n=4):
    print("\n### generalization suite ###")
    cases = [
        ("local disk", "查看我本地磁盘大小", ["926", "567", "Gi", "G"]),
        ("git status", "这个仓库当前在哪个分支？用 git 查", ["codex/litellm", "branch", "分支"]),
        ("chat only", "你好，简单介绍下你自己", ["助手", "帮", "hi", "Hi", "你好"]),
        ("multi-step", "统计 /Users/Liuguoxian/codes/carher-admin/backend 下有多少个 .py 文件", 
         ["个", "files", "共", ".py"]),
    ]
    for label, task, expect in cases:
        run(task, expect, n=n, label=label)
