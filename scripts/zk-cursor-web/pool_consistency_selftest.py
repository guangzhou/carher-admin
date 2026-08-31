#!/usr/bin/env python3
"""pool_consistency_selftest.py — 证明 pool_consistency.py 的门**真的会红**。

绿灯不算验证:一个从没红过的门,和 `return True` 无法区分(08-31 的教训,
见 memory feedback_syntax_check_is_not_a_test_gate)。这里对真实集群数据做三次注入,
每次只污染**读到的那一份副本**,不碰集群、不碰 CM、不碰 pod:

  ① 正常     → 必须 PASS(退出码 0)
  ② CM 内容改一个字节 → A 段必须 FAIL(证明它比的是字节,不是"我记得重启过")
  ③ DB 里插一条指向 lane 99 的别名 → B 段必须报 dangling FAIL(证明池覆盖真在算差集)

用法: python3 scripts/zk-cursor-web/pool_consistency_selftest.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('pc', os.path.join(HERE, 'pool_consistency.py'))
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

ORIG_KJSON, ORIG_SH = pc.kjson, pc.sh
results = []


def record(name, got, want):
    ok = got == want
    results.append((name, ok, got, want))
    print('  %s %-38s got=%-5s want=%s' % ('✅' if ok else '❌', name, got, want))


print('① 基线(不注入)')
ok_a, lanes = pc.check_code()
record('baseline-code-pass', ok_a, True)
record('baseline-pool-pass', pc.check_pool(lanes), True)

print('\n② 注入:CM 里的 responses.js 末尾多一个字节')


def kjson_corrupt(args, timeout=120):
    out = ORIG_KJSON(args, timeout)
    if args.startswith('get cm'):
        out['data']['responses.js'] += '\n'  # 只污染内存里这份副本
    return out


pc.kjson = kjson_corrupt
ok_a2, _ = pc.check_code()
pc.kjson = ORIG_KJSON
record('corrupt-cm-must-fail', ok_a2, False)

print('\n③ 注入:DB 多一条 cursor-g-5.6-sol → lane 99(集群里没有这条 lane)')


def sh_dangling(cmd, timeout=180):
    out = ORIG_SH(cmd, timeout)
    if 'psql' in cmd:
        out = out.rstrip('\n') + '\ncursor-g-5.6-sol|zerokey-cursor-g-99-sol\n'
    return out


pc.sh = sh_dangling
ok_b3 = pc.check_pool(lanes)
pc.sh = ORIG_SH
record('dangling-lane-must-fail', ok_b3, False)

print('\n' + '=' * 72)
bad = [r for r in results if not r[1]]
print('SELFTEST: %d/%d OK' % (len(results) - len(bad), len(results)))
if bad:
    print('门没有按预期咬住:', [r[0] for r in bad])
print('=' * 72)
sys.exit(1 if bad else 0)
