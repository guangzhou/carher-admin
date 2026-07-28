#!/usr/bin/env python3
"""cx_suite.py -- codex 侧全功能回归(8 项),每项独立校验磁盘/随机 token

判定只看客观证据,不看模型自述:写文件查磁盘内容、读文件用模型猜不到的随机
token、改配置逐字段校验、多轮链必须先读 step1 才知道第二个文件名。

用法: python3 testkit/cx_suite.py
      依赖 cx_e2e.py 与 /tmp/cxkey.txt
"""
import json, os, secrets, shutil, subprocess, sys
sys.path.insert(0, '/tmp')
sys.argv = ['x']
import importlib.util
spec = importlib.util.spec_from_file_location("cx", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cx_e2e.py"))
cx = importlib.util.module_from_spec(spec); spec.loader.exec_module(cx)

D = "/tmp/cxt"
results = []


def case(name, setup, task, verify):
    d = os.path.join(D, name)
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    ctx = setup(d) or {}
    r = cx.loop(task.format(d=d, **ctx), max_turns=8)
    ok, why = verify(d, r, ctx)
    results.append((name, ok, why, len(r.get("cmds") or [])))
    print("  %-22s %s  cmds=%d  %s" % (name, "PASS" if ok else "FAIL",
                                       len(r.get("cmds") or []), why))
    return ok


# 1. 写文件
def s1(d): return {}
def v1(d, r, c):
    p = os.path.join(d, "hello.txt")
    if not os.path.exists(p): return False, "文件不存在"
    return (open(p).read().strip() == "CX_WRITE_OK", open(p).read().strip()[:30])
case("write", s1, "Create {d}/hello.txt containing exactly: CX_WRITE_OK", v1)

# 2. 读文件(随机 token,模型猜不到)
def s2(d):
    t = "CXR-" + secrets.token_hex(6).upper()
    open(os.path.join(d, "in.txt"), "w").write("a\nline2: %s\nb\n" % t)
    return {"tok": t}
def v2(d, r, c): return (c["tok"] in (r.get("text") or ""), c["tok"])
case("read", s2, "Read {d}/in.txt and report ONLY the token on line 2. Read the actual file.", v2)

# 3. 原地改配置
def s3(d):
    open(os.path.join(d, "c.ini"), "w").write("name=old\nport=8080\ndebug=false\n")
    return {}
def v3(d, r, c):
    s = open(os.path.join(d, "c.ini")).read()
    ok = "port=9090" in s and "debug=true" in s and "name=old" in s
    return ok, s.replace("\n", "|")[:40]
case("edit-inplace", s3,
     "Edit {d}/c.ini in place: set port to 9090 and debug to true. Leave name unchanged. Then cat it.", v3)

# 4. 追加写(不能覆盖)
def s4(d):
    open(os.path.join(d, "log.txt"), "w").write("line1\nline2\n")
    return {}
def v4(d, r, c):
    L = open(os.path.join(d, "log.txt")).read().splitlines()
    return (L == ["line1", "line2", "line3"], "|".join(L)[:40])
case("append", s4, "Append 'line3' to {d}/log.txt keeping existing lines, then cat it.", v4)

# 5. 读->算->写
def s5(d):
    open(os.path.join(d, "n.txt"), "w").write("alpha 3\nbeta 7\ngamma 5\n")
    return {}
def v5(d, r, c):
    p = os.path.join(d, "sum.txt")
    if not os.path.exists(p): return False, "sum.txt 不存在"
    return (open(p).read().strip() == "15", open(p).read().strip())
case("read-compute-write", s5,
     "Read {d}/n.txt, sum the second column, write ONLY that sum into {d}/sum.txt, then cat it.", v5)

# 6. 真多轮(必须先读 step1 才知道第二个文件名)
def s6(d):
    t = secrets.token_hex(4).upper(); a = "CXC-" + secrets.token_hex(6).upper()
    open(os.path.join(d, "step1.txt"), "w").write("next_file: step2_%s.txt\n" % t)
    open(os.path.join(d, "step2_%s.txt" % t), "w").write("final_answer: %s\n" % a)
    return {"ans": a}
def v6(d, r, c): return (c["ans"] in (r.get("text") or ""), c["ans"])
case("multiturn-chain", s6,
     "In {d}: read step1.txt to find the next file, open it, report its final_answer. "
     "You cannot know the second filename in advance.", v6)

# 7. 目录树探索
def s7(d):
    for sub in ("a", "b", "a/deep"): os.makedirs(os.path.join(d, sub), exist_ok=True)
    t = "CXN-" + secrets.token_hex(6).upper()
    for p in ("a/x.log", "b/y.log"): open(os.path.join(d, p), "w").write("filler\n")
    open(os.path.join(d, "a/deep/t.conf"), "w").write("cfg\nsecret_value = %s\nend\n" % t)
    return {"tok": t}
def v7(d, r, c): return (c["tok"] in (r.get("text") or ""), c["tok"])
case("explore-tree", s7,
     "Somewhere under {d} a .conf file has a line starting with 'secret_value ='. "
     "Find it and report only that value.", v7)

# 8. 多命令序列(创建目录+多文件+统计)
def s8(d): return {}
def v8(d, r, c):
    fs = sorted(os.listdir(os.path.join(d, "out"))) if os.path.isdir(os.path.join(d, "out")) else []
    return (fs == ["1.txt", "2.txt", "3.txt"], ",".join(fs)[:40])
case("multi-step-build", s8,
     "In {d}: create a subdirectory 'out', then create three files 1.txt 2.txt 3.txt inside it, "
     "each containing its own number. Then list them.", v8)

print()
p = sum(1 for _, ok, _, _ in results if ok)
print("== codex 端到端全功能: %d/%d ==" % (p, len(results)))
json.dump([{"name": n, "ok": ok, "why": w, "cmds": c} for n, ok, w, c in results],
          open("/tmp/cx_results.json", "w"), ensure_ascii=False)
sys.exit(0 if p == len(results) else 1)
