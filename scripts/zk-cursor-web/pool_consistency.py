#!/usr/bin/env python3
"""pool_consistency.py — 池一致性硬门(2026-08-31 起,改 CM 后必跑)。

**为什么有这个脚本**:池里每条 lane 的代码都来自同一个 ConfigMap,但 pod 只有重启才会
把新版本 cp 进 /app/routes/。08-31 就踩了:改完 CM 只 rollout 了 82,101 静默跑旧代码 27h。
有流量的那条是对的、没流量的那条是错的 —— 池化让这种偏差**更难被发现**,lane 越多越难。
所以判据不能是"我记得重启过",必须是**容器里那份文件的字节**。

两段检查(任一红 → 退出码 1,可直接当部署门用):
  A. 代码一致性(硬门):凡挂载 CM `zk-cursor-bpi-patch` 的 deployment,其**每个 pod 内**
     /app/routes/responses.js 的 sha256 必须等于 CM 里 responses.js 的 sha256。
     lane 是从「谁挂了这个 CM」反查出来的,新克隆的 lane 自动进检查范围,不用改本文件。
  B. 池覆盖(硬门 + 提示):DB 里 cursor-g-* / cursor-web-fc-pool-* 各别名挂了哪些 lane。
     · 别名指向的 lane 在集群里没有对应 deployment → 红(dangling,会 502/超时)
     · deployment 存在但没进任何池别名 → 黄(孤儿 lane,白养着不分流)
     · 同一别名下各 lane 数量不齐 → 黄(某档少一条腿,fallback 不对称)
     只取 model_name + model_info->>'id' 两列 —— **禁止拉 litellm_params**,那列是加密凭据。
  C. env 一致性(硬门 + 决策记录,2026-08-31 补):**同一份代码 + 不同 env = 不同行为**。
     A 段绿只证明"六条 lane 跑同一个 responses.js",证不了"六条 lane 行为一致" ——
     responses.js 里几乎每个功能都由 `process.env.ZK_*` 门控,env 差一个开关,
     用户被 WA 钉到哪条 lane 就得到哪种行为,同一个菜单项行为不确定。
     08-31 实测:101 只有 11 个 env、82-85 有 25 个,A 段却一路绿 —— 就是这个盲区。
     判据:除身份类变量(ZK_USER)外,每个变量取各 lane 的**众数值**为基准,
     偏离众数即 drift;drift 必须在 ACCEPTED_ENV_DRIFT 里有**带日期和原因的条目**才放行,
     否则红。允许清单是**决策记录不是消音器**:放行的也照样打印出来。

用法:
  python3 scripts/zk-cursor-web/pool_consistency.py            # 全检查
  python3 scripts/zk-cursor-web/pool_consistency.py --code     # 只跑 A(快,不连 DB)
  python3 scripts/zk-cursor-web/pool_consistency.py --env      # 只跑 C(不连 DB)
"""
import hashlib
import json
import os
import re
import subprocess
import sys

NS = 'litellm-product'
CM = 'zk-cursor-bpi-patch'
# CM 里的 key → 容器内落点(entrypoint zerokey-serve-codex.js 负责 cp)
WATCH = {'responses.js': '/app/routes/responses.js'}
PG_PW = os.environ.get('LITELLM_PG_PW', 'pro-pg-pass-20260430-46138a20')
SSH_PW = os.environ.get('ZK_198_PW', 'Hn8#mKLp3QxZ')
SSH = ['sshpass', '-p', SSH_PW, 'ssh', '-o', 'StrictHostKeyChecking=no',
       '-o', 'ConnectTimeout=25', 'cltx@10.68.13.198']
SUDO = "echo '%s' | sudo -S k3s kubectl -n %s " % (SSH_PW, NS)

# lane 号 → deployment 名。101 是历史首条线,没有数字后缀。
def lane_deploy(lane):
    return 'zero-cursor-bpi' if lane == '101' else 'zero-cursor-bpi-%s' % lane


# ── C 段用 ───────────────────────────────────────────────────────────────────
# 身份类变量:每条 lane 本来就该不一样,不参与众数比对。
ENV_IDENTITY = {'ZK_USER'}

# 已知且**已决策**的 env 分歧。键 = (deployment 名, 变量名),值 = 原因(必须带日期)。
# 规矩:往这里加一条,等于签字"我知道这条 lane 行为与众不同,并且这是有意的"。
# 空着不代表没分歧 —— 代表还没人看过,那就该红。
ACCEPTED_ENV_DRIFT = {
    # 2026-08-27:proto2(槽位化契约)在 101 上按设计回滚过一次,101 因此停在
    # 「九补丁级联」路径,82-85 走 proto2 路径。2026-09-01 用真抓包 cap-6 交错 A/B
    # 复测(lane_task_ab.py,--followup):101 vs 82 = turn1 服从 6/6 vs 6/6、
    # 4/4 vs 4/4,turn2 回灌 4/4 vs 4/4 —— **测不出 101 差**,所以不动它。
    # 要收掉这条分歧,判据不是再跑一遍合成探针(合成绿不算依据),而是真 Cursor
    # 走一遍门②(shell ls + 飞书建文档)。见 docs/lane-101-env-divergence-20260901.md。
    ('zero-cursor-bpi', v): '08-27 proto2 在 101 按设计回滚;09-01 A/B 测不出差异,待真 Cursor 验收'
    for v in ('ZK_PROTO_V2', 'ZK_HANDSHAKE', 'ZK_CONTRACT_DIET', 'ZK_DIET_EXEMPT_MINI',
              'ZK_FAIL_TEACH', 'ZK_WRITE_DIALECT', 'ZK_TOOL_DIET', 'ZK_STRIP_EXECENV',
              'ZK_STRIP_UQ', 'ZK_STRIP_GENUI', 'ZK_EMPTY_RETRY', 'ZK_CONV_PERSIST',
              'ZK_URL_PRIOR', 'ZK_URL_DEBUG', 'ZK_MCP_REL_WATCHDOG_MS')
}


def sh(cmd, timeout=180):
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def kjson(args, timeout=120):
    out = sh(SUDO + args + ' -o json 2>/dev/null', timeout=timeout)
    if not out.strip():
        raise SystemExit('kubectl 无输出(namespace/权限?): ' + args)
    return json.loads(out)


def check_code():
    """A. CM ↔ 每个 pod 内文件逐字节一致。"""
    print('=' * 72)
    print('A. 代码一致性(CM %s ↔ 容器内文件)' % CM)
    print('=' * 72)
    cm = kjson('get cm ' + CM)
    want = {}
    for key, path in WATCH.items():
        if key not in cm['data']:
            print('  ❌ CM 里没有 key %s' % key)
            return False, []
        want[path] = hashlib.sha256(cm['data'][key].encode()).hexdigest()
        print('  CM %-16s sha256=%s  chars=%d' % (key, want[path][:16] + '…',
                                                  len(cm['data'][key])))

    deploys = kjson('get deploy')['items']
    lanes = []
    for d in deploys:
        vols = d['spec']['template']['spec'].get('volumes') or []
        if not any((v.get('configMap') or {}).get('name') == CM for v in vols):
            continue
        lanes.append(d['metadata']['name'])
    if not lanes:
        print('  ❌ 没有任何 deployment 挂载 %s —— 检查范围为空,不算通过' % CM)
        return False, []
    print('\n  挂载该 CM 的 deployment: %s\n' % ', '.join(sorted(lanes)))

    ok = True
    for dname in sorted(lanes):
        d = next(x for x in deploys if x['metadata']['name'] == dname)
        sel = ','.join('%s=%s' % kv for kv in d['spec']['selector']['matchLabels'].items())
        pods = kjson("get pods -l '%s'" % sel)['items']
        running = [p for p in pods if p['status'].get('phase') == 'Running']
        if not running:
            print('  ❌ %-24s 没有 Running 的 pod' % dname)
            ok = False
            continue
        for p in running:
            pn = p['metadata']['name']
            cn = p['spec']['containers'][0]['name']
            age = p['status'].get('startTime', '?')
            paths = ' '.join(want)
            out = sh(SUDO + 'exec %s -c %s -- sha256sum %s 2>/dev/null' % (pn, cn, paths))
            got = {}
            for ln in out.strip().splitlines():
                parts = ln.split()
                if len(parts) == 2:
                    got[parts[1]] = parts[0]
            for path, exp in want.items():
                actual = got.get(path)
                if actual == exp:
                    print('  ✅ %-24s %s  started=%s' % (dname, path, age))
                elif actual is None:
                    print('  ❌ %-24s %s 读不到(pod=%s)' % (dname, path, pn))
                    ok = False
                else:
                    print('  ❌ %-24s %s DRIFT' % (dname, path))
                    print('       期望(CM) %s' % exp)
                    print('       实际(pod) %s   pod=%s started=%s' % (actual, pn, age))
                    print('       → 该 lane 在跑旧代码,修法: kubectl -n %s '
                          'rollout restart deploy/%s' % (NS, dname))
                    ok = False
    return ok, sorted(lanes)


def check_pool(lane_deploys):
    """B. DB 里池别名的 lane 覆盖 vs 集群里实际存在的 lane。"""
    print()
    print('=' * 72)
    print('B. 池覆盖(别名 → lane)')
    print('=' * 72)
    sql = ("select model_name, model_info->>'id' from \\\"LiteLLM_ProxyModelTable\\\" "
           "where model_name like 'cursor-g-%' or model_name like 'cursor-web-fc-pool-%' "
           "order by model_name;")
    out = sh(SUDO + "exec litellm-db-0 -- env PGPASSWORD='%s' psql -U litellm -d litellm "
             "-At -F'|' -c \"%s\" 2>/dev/null" % (PG_PW, sql))
    rows = [ln.split('|') for ln in out.strip().splitlines() if '|' in ln]
    if not rows:
        print('  ❌ DB 查不到池别名(连接失败?),不算通过')
        return False
    alias = {}
    for name, dep_id in rows:
        m = re.search(r'-(\d{2,4})-', dep_id or '')
        alias.setdefault(name, set()).add(m.group(1) if m else '?')

    referenced = set()
    for lanes in alias.values():
        referenced |= lanes
    referenced.discard('?')
    counts = sorted({len(v) for v in alias.values()})

    ok = True
    for name in sorted(alias):
        lanes = sorted(alias[name])
        flag = '  ' if len(lanes) == max(len(v) for v in alias.values()) else '⚠️ '
        print('  %s%-28s lanes=%s' % (flag, name, ','.join(lanes)))
    if len(counts) > 1:
        print('\n  ⚠️  各别名的 lane 数不齐(%s) —— 少腿的那档 fallback 不对称' % counts)

    # dangling:别名指向的 lane 没有对应 deployment
    have = {d.split('zero-cursor-bpi')[-1].lstrip('-') or '101' for d in lane_deploys}
    dangling = sorted(referenced - have)
    orphan = sorted(have - referenced)
    print()
    print('  集群里挂 CM 的 lane : %s' % ','.join(sorted(have)))
    print('  池别名引用的 lane   : %s' % ','.join(sorted(referenced)))
    if dangling:
        print('  ❌ dangling(池里有、集群没有): %s → 打到它必超时/502' % ','.join(dangling))
        ok = False
    if orphan:
        print('  ⚠️  孤儿 lane(集群有、没进任何池): %s → 白养着不分流,'
              '入池用 pool_register.py' % ','.join(orphan))
    if not dangling and not orphan:
        print('  ✅ 池覆盖与集群 lane 完全对齐')
    return ok


ABSENT = '<absent>'


def lane_envs(deploys=None):
    """{deployment 名: {变量名: 值}}。valueFrom 型记成 <from:...>,不解引用 Secret。"""
    if deploys is None:
        deploys = kjson('get deploy')['items']
    out = {}
    for d in deploys:
        vols = d['spec']['template']['spec'].get('volumes') or []
        if not any((v.get('configMap') or {}).get('name') == CM for v in vols):
            continue
        env = {}
        for c in d['spec']['template']['spec'].get('containers') or []:
            for e in c.get('env') or []:
                if 'value' in e:
                    env[e['name']] = e['value']
                else:
                    env[e['name']] = '<from:%s>' % ','.join(sorted(e.get('valueFrom') or {}))
        out[d['metadata']['name']] = env
    return out


def check_env(deploys=None):
    """C. 各 lane env 与众数基准比对;未决策的偏离即红。"""
    print()
    print('=' * 72)
    print('C. env 一致性(众数基准 vs 各 lane)')
    print('=' * 72)
    envs = lane_envs(deploys)
    if not envs:
        print('  ❌ 没有任何挂 %s 的 deployment,检查范围为空,不算通过' % CM)
        return False
    names = sorted(envs)
    print('  参与比对的 lane: %s' % ', '.join('%s(%d 个 env)' % (n, len(envs[n])) for n in names))

    allvars = sorted({v for e in envs.values() for v in e} - ENV_IDENTITY)
    ok = True
    drift_new, drift_ok = [], []
    for var in allvars:
        vals = [envs[n].get(var, ABSENT) for n in names]
        # 众数:出现次数最多的值;并列时取字典序最小,保证判据可复现。
        modal = sorted({v: vals.count(v) for v in set(vals)}.items(),
                       key=lambda kv: (-kv[1], kv[0]))[0][0]
        for n, v in zip(names, vals):
            if v == modal:
                continue
            reason = ACCEPTED_ENV_DRIFT.get((n, var))
            row = (n, var, v, modal, reason)
            (drift_ok if reason else drift_new).append(row)
            if not reason:
                ok = False

    if drift_ok:
        print('\n  ⚠️  已决策的分歧(放行,但照样打印 —— 允许清单是决策记录不是消音器):')
        for n, var, v, modal, reason in drift_ok:
            print('     %-24s %-24s 本 lane=%-12s 众数=%-12s  ∵ %s'
                  % (n, var, v, modal, reason))
    if drift_new:
        print('\n  ❌ 未决策的 env 分歧(每条都要么改回众数、要么进 ACCEPTED_ENV_DRIFT 带日期和原因):')
        for n, var, v, modal, _ in drift_new:
            print('     %-24s %-24s 本 lane=%-12s 众数=%-12s' % (n, var, v, modal))
        print('     → 同一份代码 + 不同 env = 不同行为;用户被 WA 钉到哪条 lane 就吃哪种行为。')
    if not drift_ok and not drift_new:
        print('\n  ✅ 除身份变量外,各 lane env 逐项一致')
    # 允许清单里指向不存在的 lane / 已收敛的变量 = 陈旧条目,提醒清掉。
    live = {(n, var) for n, var, _, _, _ in drift_ok}
    stale = sorted(set(ACCEPTED_ENV_DRIFT) - live)
    if stale:
        print('\n  ⚠️  ACCEPTED_ENV_DRIFT 里已无对应分歧的陈旧条目(建议删掉,别留着当免疫):')
        for n, var in stale:
            print('     %s %s' % (n, var))
    return ok


def main():
    only_code = '--code' in sys.argv
    only_env = '--env' in sys.argv
    if only_env:
        ok_c = check_env()
        print()
        print('=' * 72)
        print('VERDICT: %s   (C env 一致性=%s)' % ('PASS' if ok_c else 'FAIL',
                                                  'PASS' if ok_c else 'FAIL'))
        print('=' * 72)
        return 0 if ok_c else 1
    ok_a, lanes = check_code()
    ok_b = True if only_code else check_pool(lanes)
    ok_c = check_env()
    print()
    print('=' * 72)
    verdict = 'PASS' if (ok_a and ok_b and ok_c) else 'FAIL'
    print('VERDICT: %s   (A 代码一致性=%s%s, C env 一致性=%s)' % (
        verdict, 'PASS' if ok_a else 'FAIL',
        '' if only_code else ', B 池覆盖=%s' % ('PASS' if ok_b else 'FAIL'),
        'PASS' if ok_c else 'FAIL'))
    print('=' * 72)
    if verdict == 'FAIL':
        print('改完 CM 只 rollout 一条 lane 是 08-31 踩过的坑,别只重启有流量的那条。')
    return 0 if verdict == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
