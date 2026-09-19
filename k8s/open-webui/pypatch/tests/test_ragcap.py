#!/usr/bin/env python3
"""leg3（RAG 引文截断）的回归测试（纯函数，不碰 OWUI、不碰网络）。

用法：test_ragcap.py <被测 sitecustomize.py 路径>

第 0 步是阳性对照：把它指向 leg3 之前的文件，必须直接报「没有 _cap_rag_sources」，
而不是全绿 —— 否则这套断言说明不了任何事。
"""
import importlib.util
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "sitecustomize.py"

spec = importlib.util.spec_from_file_location("owui_patch_undertest", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if not hasattr(mod, "_cap_rag_sources"):
    print(f"RED(阳性对照): {SRC} 里没有 _cap_rag_sources —— leg3 未安装")
    sys.exit(1)

cap = mod._cap_rag_sources

PER = 40000
TOTAL = 160000

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def src(*docs, name="kb"):
    """构造一个 source：document 与 metadata 必须等长平行。"""
    return {
        "document": list(docs),
        "metadata": [{"source": f"{name}-{i}"} for i in range(len(docs))],
        "source": {"id": name, "name": name, "type": "file"},
    }


def doclen(sources):
    return sum(
        len(d)
        for s in sources
        for d in s.get("document", [])
        if isinstance(d, str)
    )


print("== 单条上限 ==")
s = [src("x" * 100000)]
out = cap(s)
check("100k 单条压到 per 以内", len(out[0]["document"][0]) <= PER, f"{len(out[0]['document'][0])}")
check("留了截断痕迹", "本地补丁截断" in out[0]["document"][0])

print("== 总预算 ==")
s = [src(*["y" * 100000] * 8)]
out = cap(s)
check("8×100k 收敛到 total 以内", doclen(out) <= TOTAL, f"{doclen(out)}")
s = [src("z" * 90000) for _ in range(10)]
out = cap(s)
check("10 个 source 各 90k 收敛", doclen(out) <= TOTAL, f"{doclen(out)}")

print("== 形状不变（引文编号靠它对齐）==")
s = [src(*["a" * 80000] * 5), src("b" * 80000)]
out = cap(s)
check("source 个数不变", len(out) == len(s))
check("每个 source 的 document 条数不变",
      all(len(o["document"]) == len(i["document"]) for o, i in zip(out, s)))
check("metadata 原样", all(o["metadata"] == i["metadata"] for o, i in zip(out, s)))
check("source 描述原样", all(o["source"] == i["source"] for o, i in zip(out, s)))
check("没有空正文（空的 <source id=N> 比截断更糟)",
      all(isinstance(d, str) and d.strip() for o in out for d in o["document"]))

print("== 不原地改（前端引文展示共用这个 list）==")
orig_doc = "q" * 100000
s = [src(orig_doc)]
snapshot = s[0]["document"][0]
out = cap(s)
check("入参 sources 未被改动", s[0]["document"][0] == snapshot and len(snapshot) == 100000)
check("返回的是新对象", out is not s and out[0] is not s[0])

print("== 小内容不动 ==")
s = [src("hello", "world")]
out = cap(s)
check("小正文原样返回", out == s or (out[0]["document"] == ["hello", "world"]))

print("== 退化输入不炸 ==")
check("空 list", cap([]) == [])
check("None", cap(None) is None)
check("非 dict 元素", cap([None, 42]) == [None, 42])
s = [{"document": None}, {"no_document": 1}, {"document": [None, 5, "t" * 100000]}]
out = cap(s)
check("document 非 list / 缺键 / 混类型 不炸", len(out) == 3)
check("混类型里的非字符串原样", out[2]["document"][0] is None and out[2]["document"][1] == 5)

print("== 截断痕迹本身也要算在预算里（09-19 fuzz 抓到的）==")
# 每条被压到 0 额度的正文都会留一个壳，壳的额度必须先预留；
# 否则前几条吃满 total_cap 之后，后面每个壳都是纯超支（实测 160096）。
s = [src(*["y" * 100000] * 4), src(*["y" * 100000] * 4)]
out = cap(s)
check("4+4 条 100k 严格不超 total", doclen(out) <= TOTAL, f"{doclen(out)}")
s = [src("y" * 400000) for _ in range(20)]
out = cap(s)
check("20 条 400k 严格不超 total", doclen(out) <= TOTAL, f"{doclen(out)}")
check("20 条场景里每条都非空", all(d.strip() for o in out for d in o["document"]))

print("== kill switch ==")
import os
os.environ["OWUI_RAG_SOURCE_CAP_DISABLED"] = "1"
s = [src("w" * 200000)]
check("关掉后原样", cap(s) is s)
del os.environ["OWUI_RAG_SOURCE_CAP_DISABLED"]

print()
if fails:
    print(f"RED: {len(fails)} 项失败 -> {fails}")
    sys.exit(1)
print("GREEN: 全部通过")
