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
     **换成 fork CM 的 lane 必须登记进 FORKED_CM**,否则它会静悄悄退出检查范围
     (A 段不再验它的字节、B 段把它误报成 dangling)—— 登记后照样验,只是基准换成它自己那份。
  B. 池覆盖(硬门 + 提示):DB 里 cursor-g-* / cr-g-* / cursor-web-fc-pool-* 各别名挂了哪些 lane。
     · 别名指向的 lane 在集群里没有对应 deployment → 红(dangling,会 502/超时)
     · deployment 存在但没进任何池别名 → 黄(孤儿 lane,白养着不分流);**CANARY_LANES 除外**
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
def _require_env(name):
    """凭据只从环境变量读，缺了直接退出。

    不设内置默认值：写死一个真 PG 口令等于把凭据提交进仓库，而且口令轮转后
    老默认值还会静默生效，打出来的认证失败看不出是"忘了设 env"还是"口令真的换了"。
    """
    v = os.environ.get(name, '')
    if not v:
        raise SystemExit(
            '缺少环境变量 %s —— 先 export %s=<litellm-db-0 的 PG 口令>（别写进文件/命令行历史）'
            % (name, name)
        )
    return v


PG_PW = _require_env('LITELLM_PG_PW')
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

# 2026-09-03:[skill-hint] 首轮握手多带一段"本机有 skill 库,说自己没能力前先搜"的提示 +
# 首轮拒绝时网关直接把 grep 编成真 Shell 调用(skill-kick)。真 Cursor 验收:一条 chat 8 轮,
# hi/ls/建飞书文档(lark-cli 独立打开核对)/增量全程一个 conv,全过。
# 01:16 起铺到 135~140 六条腿(用户指令"以 135 为模板改造 136~140")。众数因此翻成 1;
# 下面三条是**没开**的:82 是 canary(用户明令不动)、84 用户这次没点名、101 是只读对照。
# 收敛条件:84 何时跟进由用户定;82 永远按 canary 规矩单独决策。
ACCEPTED_ENV_DRIFT[('zero-cursor-bpi-82', 'ZK_SKILL_HINT')] = '09-03 82 是 canary,用户明令不动;skill-hint 铺池不含 82'
ACCEPTED_ENV_DRIFT[('zero-cursor-bpi-84', 'ZK_SKILL_HINT')] = '09-03 用户只点名 136~140 跟 135;84 未跟进,待用户决定'
ACCEPTED_ENV_DRIFT[('zero-cursor-bpi', 'ZK_SKILL_HINT')] = '09-03 101 是只读对照线,不铺新东西'


# 已知且**已决策**的 CM fork。键 = deployment 名,值 = (它自己的 CM 名, 原因(必须带日期))。
# 为什么需要这一项:lane 是靠「谁挂了 CM zk-cursor-bpi-patch」反查的,一条 lane 换成
# fork 出来的 CM 之后就**从检查范围里消失**了 —— A 段不再校验它的字节(最危险的那部分),
# B 段还会把它报成 dangling。所以 fork 必须在这里登记:登记之后它照样进 A 段,只是
# 比对基准换成它自己那份 CM。**这不是消音器**:82 的 pod 与 82 的 CM 不一致照样红。
FORKED_CM = {
    'zero-cursor-bpi-82': (
        'zk-cursor-bpi-patch-82',
        '2026-09-01:82 是 canary,新改动先在它身上试。共用 CM 改不动单线,故 fork。'
        '收敛条件=灰度结论出来后要么推广到池 CM(见下面四条)、要么整条回滚'
        '(deploy 改回挂 %s,删本条)。' % CM),
}
# 2026-09-03:135 是 skill-hint 的灰度靶子,fork 了自己的 CM(第一版错误地改了共用 `-pool` 并
# 重启了六条有流量的腿 —— 单条 lane 的小闭环测试不该让别人停机,这条教训留着)。
# 同日 01:16 用户拍板"以 135 为模板改造 136~140":五条腿改挂 `-135` 这份 CM + ZK_SKILL_HINT=1,
# 逐条 rollout、逐条核对容器内 sha == CM。**现在 `-135` 是六条腿共用的 CM,改它必须六条全滚**
# (pool_consistency 会逐条验字节)。84 仍挂 `-pool`。
# 收敛条件:84 也跟进后把 `-135` 的内容推回 `-pool`、六条腿改回挂 `-pool`、删 `-135`;
# 或者反过来把 `-pool` 废掉。回滚见 docs/skill-hint-rollback-20260903.md。
FORKED_CM.update({
    'zero-cursor-bpi-%s' % n: (
        'zk-cursor-bpi-patch-135',
        '2026-09-03:skill-hint 六腿(135~140)共用 `-135`;84 未跟进仍在 `-pool`;82 canary 不动。')
    for n in ('135', '136', '137', '138', '139', '140')
})
# 2026-09-02:池腿从共用 CM 迁到 `-pool`(= `-82` 的逐字节拷贝)。
# 起因:共用 CM 的 responses.js 是退化版(701a7f50),缺会话复用最长前缀修复、
# stream_handoff 轮询、handoff 抢跑闸;82 那份(ba2f5e77)是验好的。同事被 WA 亲和到
# 哪条腿就随机拿到好代码还是坏代码。
# 三份 CM 各管各的:`-82`=canary,`-pool`=生产池,`zk-cursor-bpi-patch`=101(旧方案只读对照)。
# **收敛条件**:`-pool` 与 `-82` 出现差异时,要么是 82 在灰度新东西(暂时的,验完推 pool),
# 要么是漏推(缺陷)。判别方法写在 docs/cr-g-pool-rollback-20260902.md,不要靠猜。
#
# **腿表当天换过一次(2026-09-02 傍晚)**:原池腿 81/83/84/85 里的 81/83/85 背后账号是
# **free 档**(`/backend-api/models` 只有 10 个 slug,没有 thinking/pro/instant)——
# 它们**物理上答不出**菜单里的大多数名字,不是"慢"也不是"账号不稳"。已从全部池别名摘腿、
# deploy/svc 删除(备份 /Data/backups/zk-bpi-{deploy,svc}-{81,83,85}-20260902-174450-pre-delete.json)。
# 换成 135~140 六个 pro 号 + 保留 84 = 七腿。入池门 = lane_model_catalog.py:19+ slug 且
# 含 thinking/pro/instant,六条新腿实测 19~20 slug,全是 84 的超集。
FORKED_CM.update({
    'zero-cursor-bpi-84': (
        'zk-cursor-bpi-patch-pool',
        '2026-09-02:生产池腿,挂 `-pool`(= 82 那份验好的代码逐字节拷贝)。'
        '09-03 起 135~140 迁到 `-135`(见上),84 是唯一还在 `-pool` 的腿,待用户决定是否跟进。'),
})

# B 段用:canary lane 不参与池分流,**它不在池别名里是设计不是缺陷**。
# 不登记的话它会被报成"孤儿 lane(白养着不分流)",而那句提示会诱导人把它塞回池里 ——
# 恰好销毁 canary。
CANARY_LANES = {'82'}


def lane_cm(d):
    """这条 deployment 该拿哪份 CM 当基准;都没挂返回 None(不是本池的 lane)。"""
    name = d['metadata']['name']
    mounted = {(v.get('configMap') or {}).get('name')
               for v in (d['spec']['template']['spec'].get('volumes') or [])}
    if CM in mounted:
        return CM
    fk = FORKED_CM.get(name)
    if fk and fk[0] in mounted:
        return fk[0]
    return None


def sh(cmd, timeout=180):
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def kjson(args, timeout=120):
    out = sh(SUDO + args + ' -o json 2>/dev/null', timeout=timeout)
    if not out.strip():
        raise SystemExit('kubectl 无输出(namespace/权限?): ' + args)
    return json.loads(out)


def check_code():
    """A. CM ↔ 每个 pod 内文件逐字节一致。fork 出去的 lane 比对它自己那份 CM。"""
    print('=' * 72)
    print('A. 代码一致性(CM ↔ 容器内文件)')
    print('=' * 72)

    cm_want = {}          # cm 名 → {容器内路径: sha256}

    def want_for(cmname):
        if cmname in cm_want:
            return cm_want[cmname]
        cm = kjson('get cm ' + cmname)
        w = {}
        for key, path in WATCH.items():
            if key not in cm['data']:
                print('  ❌ CM %s 里没有 key %s' % (cmname, key))
                return None
            w[path] = hashlib.sha256(cm['data'][key].encode()).hexdigest()
            print('  CM %-24s %-16s sha256=%s  chars=%d'
                  % (cmname, key, w[path][:16] + '…', len(cm['data'][key])))
        cm_want[cmname] = w
        return w

    deploys = kjson('get deploy')['items']
    lanes = []            # [(deployment 名, 它的 CM 名)]
    for d in deploys:
        c = lane_cm(d)
        if c:
            lanes.append((d['metadata']['name'], c))
    if not lanes:
        print('  ❌ 没有任何 deployment 挂载 %s(或其已登记 fork) —— 检查范围为空,不算通过' % CM)
        return False, []
    for dname, cmname in sorted(lanes):
        if cmname != CM:
            print('  ⚠️  %s 用的是 fork 出来的 CM %s(已决策,照样验字节)\n     ∵ %s'
                  % (dname, cmname, FORKED_CM[dname][1]))
    print('\n  纳入检查的 deployment: %s\n'
          % ', '.join('%s→%s' % t for t in sorted(lanes)))

    ok = True
    for dname, cmname in sorted(lanes):
        want = want_for(cmname)
        if want is None:
            ok = False
            continue
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
    return ok, sorted(n for n, _ in lanes)


def check_pool(lane_deploys):
    """B. DB 里池别名的 lane 覆盖 vs 集群里实际存在的 lane。"""
    print()
    print('=' * 72)
    print('B. 池覆盖(别名 → lane)')
    print('=' * 72)
    # `cr-g-%` 里要排掉 `%-82`:那些是 canary 的**直连名**(一名一腿,钉死 82),不是池别名。
    # 混进来有两个后果,都是把门变成噪音源:①「各别名 lane 数不齐」会常态告警(1 腿 vs 4 腿);
    # ②它们的 id(`zerokey-cr-g-5.6-82`、裸载体名 `gpt-5.6-luna-wm`)抽不出 lane 号,报成 `?`。
    sql = ("select model_name, model_info->>'id' from \\\"LiteLLM_ProxyModelTable\\\" "
           "where model_name like 'cursor-g-%' or model_name like 'cursor-web-fc-pool-%' "
           "or (model_name like 'cr-g-%' and model_name not like '%-82') "
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

    # dangling:别名指向的 lane 没有对应 deployment。
    # ⚠️ lane_deploys 来自 A 段,含**登记正确**的 fork lane。这里刻意不额外兜底:
    # 09-03 曾给 have 并上 FORKED_CM 的键去消 135 的"假 dangling",结果那根本不是假红 ——
    # 是 135 的 fork 登记被批量表覆盖成 `-pool`、CM 名对不上而退出了 A 段,B 段的红是
    # 唯一还在喊的那张嘴。兜底一加,两段一起哑,门照样 PASS。
    # ⇒ 这里报 dangling 时先查那条 lane 是不是掉出了 A 段,别急着改这一行。
    have = {d.split('zero-cursor-bpi')[-1].lstrip('-') or '101' for d in lane_deploys}
    dangling = sorted(referenced - have)
    orphan = sorted(have - referenced - CANARY_LANES)
    canary_idle = sorted((have - referenced) & CANARY_LANES)
    print()
    print('  集群里挂 CM 的 lane : %s' % ','.join(sorted(have)))
    print('  池别名引用的 lane   : %s' % ','.join(sorted(referenced)))
    if dangling:
        print('  ❌ dangling(池里有、集群没有): %s → 打到它必超时/502' % ','.join(dangling))
        ok = False
    if canary_idle:
        print('  ℹ️  canary lane(不进池是设计,不是孤儿): %s —— 只挂直连名,'
              '新改动先在它身上验' % ','.join(canary_idle))
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
        if not lane_cm(d):
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
