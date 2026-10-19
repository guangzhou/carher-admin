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


# 2026-10-19:js 的菜单改成按 Key 现拉(fetchKeyModels/orderMenu),不再有写死的 DEFAULT_MODELS。
# py 孪生不随包发(见 skill cursor-client-bundle-patch),它那份旧菜单不再参与比对。
if re.search(r"const DEFAULT_MODELS = \[", js):
    sys.exit("FAIL: js 里又出现了写死的 DEFAULT_MODELS —— 菜单应按 Key 动态拉取")
if "async function fetchKeyModels(" not in js or "function orderMenu(" not in js:
    sys.exit("FAIL: js 里找不到 fetchKeyModels / orderMenu —— 动态菜单被改没了")
js_models = []
js_def = scalar(js, r'const DEFAULT_MODEL = "([^"]+)"')
py_def = scalar(py, r'^DEFAULT_MODEL = "([^"]+)"')
js_pfx = names(block(js, r"const MODEL_PREFIXES = \[", "]"))
py_pfx = names(block(py, r"(?m)^MODEL_PREFIXES = \[", "]"))
js_ret = names(block(js, r"const RETIRED_MODELS = \[", "]"))
py_ret = names(block(py, r"(?m)^RETIRED_MODELS = \[", "]"))

fail = 0
# 🔴 会数东西的门必须先断言自己数得到。2026-09-22 实测:注释里写了一个 `aliases[裸名]`,
#    block() 截到那个 `]` 就停 ⇒ 两边都抠出**空菜单**,而 `[] == []` 让全门变绿,
#    连"菜单每个名字命中前缀"都成了"0 个名字全部命中"。空集是假绿的经典形状,直接拒绝出数。
#    (顺带的硬约束:DEFAULT_MODELS / MODEL_PREFIXES 的注释里不许出现 `]`。)
if len(js_pfx) < 3 or len(js_ret) < 1:
    sys.exit("FAIL: MODEL_PREFIXES=%d / RETIRED_MODELS=%d —— 没量到东西"
             % (len(js_pfx), len(js_ret)))
print("✅ js 菜单是动态的(无写死清单,有 fetchKeyModels/orderMenu)")

for label, a, b in (("DEFAULT_MODEL", js_def, py_def), ("MODEL_PREFIXES", js_pfx, py_pfx),
                    ("RETIRED_MODELS", js_ret, py_ret)):
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

# 退役名同理:mergeConfig 的删除路径靠名字精确匹配,但 uninstall 走 isOurs,
# 不命中前缀的退役名会在"卸载"那条路上被漏掉。
ret_orphan = [m for m in js_ret if not any(m.startswith(p) for p in js_pfx)]
if ret_orphan:
    fail = 1
    print("❌ 退役名不命中 MODEL_PREFIX:", ret_orphan)
# 退役名不许同时还在菜单里 —— 那样 dedup 会先加回来,删除路径等于没有。
both = [m for m in js_ret if m in js_models]
if both:
    fail = 1
    print("❌ 这些名字同时在 DEFAULT_MODELS 和 RETIRED_MODELS 里(一边加一边删):", both)
elif js_ret:
    print("✅ 退役名 %r 不在菜单里,且命中 MODEL_PREFIX" % (js_ret,))

print("VERDICT:", "FAIL" if fail else "PASS")
sys.exit(fail)
