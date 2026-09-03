#!/usr/bin/env python3
"""pool_consistency_selftest.py — 证明 pool_consistency.py 的门**真的会红**。

绿灯不算验证:一个从没红过的门,和 `return True` 无法区分(08-31 的教训,
见 memory feedback_syntax_check_is_not_a_test_gate)。这里对真实集群数据做三次注入,
每次只污染**读到的那一份副本**,不碰集群、不碰 CM、不碰 pod:

  ① 正常     → 必须 PASS(退出码 0)
  ② CM 内容改一个字节 → A 段必须 FAIL(证明它比的是字节,不是"我记得重启过")
  ③ DB 里插一条指向 lane 99 的别名 → B 段必须报 dangling FAIL(证明池覆盖真在算差集)
  ④ 基线 env(带允许清单)→ 必须 PASS
  ⑤ 给某条 lane 塞一个别人没有的 env → C 段必须 FAIL(证明它真在逐项比众数)
  ⑥ 把允许清单清空 → C 段必须 FAIL(证明 101 那 15 条分歧是被**允许清单**放行的,
     不是 C 段压根没看见 —— 否则允许清单等于装饰)
  ⑦ 把某条 lane 偷偷换成没登记的 fork CM → 必须 FAIL(2026-09-01 补:82 换 fork CM 后
     它一度整条退出 A 段检查范围,那种"少一条 lane 也照样绿"是最危险的形状)

用法: python3 scripts/zk-cursor-web/pool_consistency_selftest.py
"""
import copy
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

print('\n④ 基线 env(带 ACCEPTED_ENV_DRIFT 允许清单)')
deploys = ORIG_KJSON('get deploy')['items']
record('baseline-env-pass', pc.check_env(copy.deepcopy(deploys)), True)

print('\n⑤ 注入:某条 lane 多一个别人没有的 env(未决策)')
d5 = copy.deepcopy(deploys)
victim = None
for d in d5:
    vols = d['spec']['template']['spec'].get('volumes') or []
    if any((v.get('configMap') or {}).get('name') == pc.CM for v in vols):
        victim = d['metadata']['name']
        d['spec']['template']['spec']['containers'][0].setdefault('env', []).append(
            {'name': 'ZK_SELFTEST_DRIFT', 'value': '1'})
        break
print('  (被注入的 lane = %s)' % victim)
record('undecided-env-drift-must-fail', pc.check_env(d5), False)

print('\n⑥ 注入:清空允许清单 → 真实存在的 101 分歧必须变红')
saved = dict(pc.ACCEPTED_ENV_DRIFT)
pc.ACCEPTED_ENV_DRIFT = {}
ok_c6 = pc.check_env(copy.deepcopy(deploys))
pc.ACCEPTED_ENV_DRIFT = saved
record('empty-allowlist-must-fail', ok_c6, False)

print('\n⑦ 注入:某条 lane 偷偷换成**没登记**的 fork CM → 必须红'
      '(fork 不登记就等于退出检查范围,这正是要防的)')

# 注入目标**不能写死 lane 名**。这条 fixture 已经被陈旧咬过两次:
#   · 2026-09-02 上午:写死成"挂 pc.CM 的那条 lane",而池化后 83 挂的是 `-pool`
#     (已登记的 fork),if 一个字都匹配不上 —— 注入变成空操作,门当然不红。
#   · 2026-09-02 傍晚:83 因为背后是 free 号被整条删掉,靶子直接不存在了,这条检测
#     从此每次都 N/A —— **不吐假红,但也永远测不到东西**,等于静默失测。
# 现在改成**运行时从活着的 deploy 里挑**:优先挑池腿(挂已登记 fork 的),挑不到再退而
# 求其次挑挂共用 CM 的。这样删腿/加腿都不会让它失效。
# 仍然保留"没注入到就报 N/A 不报 FAIL"那一层:喂错对象的套件说 N/A,
# 别让人去改产品迎合断言。
def pick_stealth_lane():
    """挑一条此刻真挂着受检 CM 的 lane 当靶子;挑不到返回 None。"""
    try:
        items = ORIG_KJSON('get deploy')['items']
    except Exception:
        return None
    forked, plain = [], []
    for d in items:
        base = pc.lane_cm(d)
        if not base:
            continue
        (forked if base != pc.CM else plain).append(d['metadata']['name'])
    # 优先池腿:它们是"fork 已登记"那条路径,正是本用例要防的那种偷换
    return sorted(forked)[0] if forked else (sorted(plain)[0] if plain else None)


STEALTH_LANE = pick_stealth_lane()
_stealth_hits = []


def kjson_stealth_fork(args, timeout=120):
    out = ORIG_KJSON(args, timeout)
    if args.startswith('get deploy') and STEALTH_LANE:
        for d in out['items']:
            if d['metadata']['name'] != STEALTH_LANE:
                continue
            base = pc.lane_cm(d)          # 它此刻的基准 CM(共用的或已登记的 fork)
            if not base:
                continue
            for v in d['spec']['template']['spec'].get('volumes') or []:
                if (v.get('configMap') or {}).get('name') == base:
                    v['configMap']['name'] = base + '-stealth'
                    _stealth_hits.append(base)
    return out


pc.kjson = kjson_stealth_fork
ok_a7, lanes7 = pc.check_code()
ok_b7 = pc.check_pool(lanes7)
pc.kjson = ORIG_KJSON
if not _stealth_hits:
    print('  ⏭  N/A stealth-fork:%s —— 注入无对象(集群里没有挂受检 CM 的 lane?),'
          '这不是门的红,是套件喂错了对象' % (STEALTH_LANE or '没挑到靶子'))
else:
    print('  (注入生效:%s 的 %s → %s-stealth)'
          % (STEALTH_LANE, _stealth_hits[0], _stealth_hits[0]))
    record('stealth-fork-must-fail', ok_a7 and ok_b7, False)

print('\n' + '=' * 72)
bad = [r for r in results if not r[1]]
print('SELFTEST: %d/%d OK' % (len(results) - len(bad), len(results)))
if bad:
    print('门没有按预期咬住:', [r[0] for r in bad])
print('=' * 72)
sys.exit(1 if bad else 0)
