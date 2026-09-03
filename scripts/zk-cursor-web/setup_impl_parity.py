#!/usr/bin/env python3
# 判据(不是语法检查):两份安装器实现的三个常量必须逐字相等。
# 不执行任一文件 —— 纯文本抠常量块,避免 import 副作用。
import io, re, sys

import os

BASE = "/Users/Liuguoxian/codes/carher-admin/scripts/zk-cursor-web/"
# 阳性对照用:指向换代前的旧文件跑一遍,这把尺子必须变红,否则它不是门。
JS_PATH = os.environ.get("PARITY_JS", BASE + "cursor_team_setup.js")
PY_PATH = os.environ.get("PARITY_PY", BASE + "cursor_team_setup.py")
js = io.open(JS_PATH, encoding="utf-8").read()
py = io.open(PY_PATH, encoding="utf-8").read()


def block(src, start_pat, end_ch):
    m = re.search(start_pat, src)
    if not m:
        sys.exit("FAIL: 找不到 %r" % start_pat)
    tail = src[m.end():]
    i = tail.index(end_ch)
    return tail[:i]


def names(chunk):
    # 只取引号里的串,顺带丢掉注释行
    lines = [l.split("//")[0].split("#")[0] for l in chunk.splitlines()]
    return re.findall(r'"([^"]+)"', "\n".join(lines))


def scalar(src, pat):
    # 必须开 re.M —— `^` 锚的是行首不是文件首。少这个标志会让门红在
    # "找不到常量" 而不是 "两边不相等",是假红(2026-09-02 阳性对照当场抓到)。
    m = re.search(pat, src, re.M)
    if not m:
        sys.exit("FAIL: 找不到 %r" % pat)
    return m.group(1)


js_models = names(block(js, r"const DEFAULT_MODELS = \[", "]"))
py_models = names(block(py, r"DEFAULT_MODELS = \[", "]"))
js_def = scalar(js, r'const DEFAULT_MODEL = "([^"]+)"')
py_def = scalar(py, r'^DEFAULT_MODEL = "([^"]+)"')
js_pfx = names(block(js, r"const MODEL_PREFIXES = \[", "]"))
py_pfx = names(block(py, r"(?m)^MODEL_PREFIXES = \[", "]"))

fail = 0
print("js DEFAULT_MODELS: %d 个" % len(js_models))
print("py DEFAULT_MODELS: %d 个" % len(py_models))
if js_models != py_models:
    fail = 1
    print("❌ 菜单不等")
    print("  只在 js:", [x for x in js_models if x not in py_models])
    print("  只在 py:", [x for x in py_models if x not in js_models])
    if sorted(js_models) == sorted(py_models):
        print("  (元素相同、顺序不同 —— 菜单顺序影响同事看到的排序,也算不等)")
else:
    print("✅ 菜单逐字相等(含顺序):", ", ".join(js_models))

for label, a, b in (("DEFAULT_MODEL", js_def, py_def), ("MODEL_PREFIXES", js_pfx, py_pfx)):
    if a != b:
        fail = 1
        print("❌ %s 不等: js=%r py=%r" % (label, a, b))
    else:
        print("✅ %s 相等: %r" % (label, a))

# 前缀判断的语义门:老代名字必须**不**被认成"已经是我们的名字",否则升级换不掉
for old in ("cursor-g-5.6-sol", "cursor-web-fc-terra"):
    if any(old.startswith(p) for p in js_pfx):
        fail = 1
        print("❌ 旧名 %r 命中 MODEL_PREFIX —— 老用户升级不会被换到新名" % old)
if not fail:
    print("✅ 旧代名字不命中 MODEL_PREFIX(升级会正确换代)")
    if not any(js_def.startswith(p) for p in js_pfx):
        fail = 1
        print("❌ DEFAULT_MODEL 自己不命中 MODEL_PREFIX —— 幂等重跑会反复覆盖")
    else:
        print("✅ DEFAULT_MODEL 命中 MODEL_PREFIX(重跑幂等)")

print("VERDICT:", "FAIL" if fail else "PASS")
sys.exit(fail)
