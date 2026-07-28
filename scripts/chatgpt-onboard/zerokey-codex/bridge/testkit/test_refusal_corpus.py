# test_refusal_corpus.py -- 用 corpus 自带的 score() 跑拒绝检测器
#
# 为什么要用 corpus.score() 而不是自己写打分:
# STALL 的期望是**被捕获**(True),因为"跑了 --help 然后承认任务没做完"应当触发
# 重试。我第一次手写打分时把 STALL 当成 False 来判,得出"4 条失败"的假退化,
# 差点去改一个没坏的东西。口径以 corpus.score() 为准。
#
# 同时钉住 typographic 撇号:模型输出的是 U+2019('), 而检测器里所有 pattern
# 都写直撇("can't"),曾导致真实拒绝被漏判 —— 两条文本除撇号外完全相同,
# 一条判 refusal 一条不判。归一化放在 _looks_like_refusal 入口。
#
# 跑: python3 testkit/test_refusal_corpus.py   (从 bridge/ 或任意目录)

import os
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("ZK_KEY", "x")

import corpus  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "br", os.path.join(HERE, "..", "zerokey-codex-responses-bridge.py"))
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

f = m._looks_like_refusal
bad = 0


def chk(name, cond):
    global bad
    if not cond:
        bad += 1
    print(("PASS  " if cond else "FAIL  ") + name)


# --- 权威口径:corpus 自己的 score() ---
tp, fp, st = corpus.score(f)
chk("REFUSE 全捕获 (%d/%d)" % (tp, len(corpus.REFUSE)), tp == len(corpus.REFUSE))
chk("KEEP 无误判 (%d)" % fp, fp == 0)
chk("STALL 全捕获 (%d/%d)" % (st, len(corpus.STALL)), st == len(corpus.STALL))

# --- typographic 撇号:同一句话两种撇号必须同判 ---
PAIRS = [
    ("I don{0}t have access to a shell execution tool in this chat, "
     "so I can{0}t actually run the command.", True),
    ("The command output shows it{0}s already appended -- line3 is present.", False),
    ("I don{0}t think you need sudo here; the directory is already writable.", False),
]
for tmpl, want in PAIRS:
    straight = f(tmpl.format("'"))
    curly = f(tmpl.format("’"))
    chk("撇号无关性: %s..." % tmpl[:34], straight == curly == want)

# 生产真实样本(弯撇),曾被漏判
REAL = ("I don’t have access to a shell execution tool in this chat, so I "
        "can’t actually run the command or truthfully show the resulting "
        "`cat` output.")
chk("生产漏判样本now捕获", f(REAL) is True)

# 空/None 不炸
chk("空字符串返回 False", f("") is False)
chk("None 返回 False", f(None) is False)

print(("\n%d 条失败" % bad) if bad else "\n%d/%d 通过" % (9, 9))
sys.exit(1 if bad else 0)
