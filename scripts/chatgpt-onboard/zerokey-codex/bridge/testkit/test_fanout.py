# test_fanout.py -- BRIDGE_TOOL_FANOUT 语义测试
#
# 原实现 max(fanout, env) 是单向棘轮:operator 能把 fanout 调高,
# 但无法调低到 caller 传入值以下 -> BRIDGE_TOOL_FANOUT=1 无法撤销 caller 的 fanout=3。
# 对配额守卫(G4)来说方向正好是错的:这个 env 存在的目的是"限制成本",
# 所以必须既能升也能降。改为 max(1, env)。
#
# 跑: python3 testkit/test_fanout.py
# fanout 语义测试:BRIDGE_TOOL_FANOUT 必须能"降"不只能"升"
import os
def old(fanout, env): return max(fanout, int(env))
def new(fanout, env): return max(1, int(env))
cases=[
 ("caller=3 env=1 应降到1", 3, "1", 1),
 ("caller=1 env=3 应升到3", 1, "3", 3),
 ("caller=1 env=1 保持1",   1, "1", 1),
 ("caller=5 env=2 应降到2", 5, "2", 2),
 ("env=0 不能变成0",        1, "0", 1),
]
bad=0
for n,f,e,want in cases:
    g=new(f,e); o=old(f,e); ok=(g==want)
    if not ok: bad+=1
    print(("PASS  " if ok else "FAIL  ")+n+"  new=%d want=%d  (旧实现=%d%s)"%(g,want,o,"  <-- 旧的错" if o!=want else ""))
print(("\n%d 失败"%bad) if bad else "\n5/5 通过")
raise SystemExit(1 if bad else 0)
