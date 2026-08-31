#!/usr/bin/env python3
"""failover_drill.py — "一台挂了，这一发能不能当场换另一台"演练（2026-08-31）。

**要回答的问题**：cursor-g 池有两条 lane，08-24 演练证过的是**后续请求**会甩到健康线；
从没测过**失败的那一发本身**会不会当场换台重试。用户 08-31 要的是后者：82 挂了当场重试 101。

**安全性**：不碰 82/101 的 deployment，不改全局 router_settings。
`/model/new` 临时注册自用组 `zk-failover-drill`，跑完 `/model/delete` 删干净；
临时 key duration 20m 用完即删。对线上零影响。

**四段，缺一段结论就不成立**：
  0. 控制组：组里**只有好 lane** → 必须成功。
     没有这一段，后面的 "0/4" 分不清是"没换台"还是"这条临时通道本来就打不通"。
  B. 坏(weight 50) + 好(weight 1) → 看**这一发**能不能被救活。
     三个必须，少一个就是假绿（08-31 第一版全踩了）：
       ① B 必须跑在 A **之前**。A 失败会给坏 lane 打 180s fail-mark，
          之后的 B 根本不会挑到坏 lane，"4/4 成功"测的是"已拉黑"不是"会换台"。
       ② 每发换新 key。WA 是 key 级黏性，复用 key 的话第一发失败后剩下几发自动走好 lane，
          那是"下一发自愈"不是"这一发换台"。
       ③ 每发之间隔过 fail-mark 窗口，且**逐发从日志里证明坏 lane 真被选中过**。
          没被选中 = 这一发压根没考到，不许计入成功率。
  A. 只有坏 lane → 看错误的形状和耗时。认不出"挂了"就谈不上重试。
  C. 汇总日志决策行。

用法:
  python3 scripts/zk-cursor-web/failover_drill.py [B段发几次=3] [两发间隔秒=200] [--stream] [--hang]
  `--stream` 打流式。**真 Cursor 走的是流式**，非流式的结论不能直接搬过来：
  流式一旦已经吐了字节给客户端，就没法再悄悄换台重发了。
  `--hang` 换一种坏法：lane 不是"解析不到"，而是**连得上但永远不回**（账号变哑、pod 活着但卡死）。
  这是更接近真实故障的形状，天花板 = 全局 `router_settings.timeout=300s`，所以只发 1 发、跳过 A 段。
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

ARGS = [a for a in sys.argv[1:] if not a.startswith('--')]
STREAM = '--stream' in sys.argv
HANG = '--hang' in sys.argv
N = int(ARGS[0]) if len(ARGS) > 0 else (1 if HANG else 3)
GAP = int(ARGS[1]) if len(ARGS) > 1 else 200  # 两发之间的间隔，必须 > WA fail-mark 180s
GROUP = 'zk-failover-drill'
BASE = 'https://cc.auto-link.com.cn/pro/v1/responses'
DEAD_ID = 'zk-drill-dead'
LIVE_ID = 'zk-drill-live'
# 两种坏法，别混为一谈：
#   dns  = svc 不存在，连都连不上 → 毫秒级被认出来，最容易救。
#   hang = 路由黑洞（TEST-NET-1，SYN 被丢弃）→ 连得上的假象，一直等。
#          这才像"账号变哑 / pod 活着但卡死"，天花板是 router_settings.timeout=300s。
DEAD_BASE = ('http://192.0.2.1:8201/v1' if HANG else
             'http://zk-drill-nonexistent.litellm-product.svc.cluster.local:8201/v1')
LIVE_BASE = 'http://zero-cursor-bpi-82.litellm-product.svc.cluster.local:8201/v1'

SSH = ['sshpass', '-p', 'Hn8#mKLp3QxZ', 'ssh', '-o', 'StrictHostKeyChecking=no',
       '-o', 'ConnectTimeout=25', 'cltx@10.68.13.198']
SUDO = "echo 'Hn8#mKLp3QxZ' | sudo -S k3s kubectl -n litellm-product "

TOOLS = [{'type': 'function', 'name': 'shell',
          'description': 'Run a shell command and return its output.',
          'parameters': {'type': 'object',
                         'properties': {'command': {'type': 'string'}},
                         'required': ['command']}}]


def sh(cmd, timeout=180):
    return subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout).stdout


def proxy_py(code, timeout=120):
    """在 litellm-proxy pod 内跑（那里有 LITELLM_MASTER_KEY 且能打 localhost:4000）。"""
    return sh(SUDO + 'exec deploy/litellm-proxy -- python3 -c "%s" 2>&1'
              % code.replace('"', '\\"'), timeout).strip()


def api(path, payload):
    return proxy_py(
        "import urllib.request,urllib.error,json,os\n"
        "d=json.dumps(%r).encode()\n"
        "r=urllib.request.Request('http://localhost:4000%s',data=d,"
        "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],"
        "'Content-Type':'application/json'})\n"
        "try:\n"
        "    print('OK',urllib.request.urlopen(r,timeout=25).status)\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('ERR',e.code,e.read().decode()[:200])\n" % (payload, path))


def add_model(dep_id, api_base, weight):
    return api('/model/new', {
        'model_name': GROUP,
        'litellm_params': {
            'model': 'openai/gpt-5.6-terra', 'api_base': api_base,
            'api_key': 'sk-zerokey-web-noop', 'weight': weight,
            'use_chat_completions_api': False, 'use_in_pass_through': False,
            'use_litellm_proxy': False, 'merge_reasoning_content_in_choices': False,
        },
        'model_info': {'id': dep_id, 'mode': 'chat'},
    })


def del_model(dep_id):
    return api('/model/delete', {'id': dep_id})


def mint_key(n):
    out = proxy_py(
        "import urllib.request,json,os\n"
        "d=json.dumps({'models':['%s'],'key_alias':'fodrill-%d-%d','duration':'20m'}).encode()\n"
        "r=urllib.request.Request('http://localhost:4000/key/generate',data=d,"
        "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],"
        "'Content-Type':'application/json'})\n"
        "print(json.load(urllib.request.urlopen(r,timeout=25))['key'])\n"
        % (GROUP, int(time.time()), n))
    for ln in out.splitlines():
        if ln.strip().startswith('sk-'):
            return ln.strip()
    raise SystemExit('mint key 失败:\n' + out)


def del_keys(keys):
    return api('/key/delete', {'keys': keys})


def pods():
    return sh(SUDO + "get pods -l app=litellm-proxy -o jsonpath='{.items[*].metadata.name}'").split()


def wait_deps(want, timeout=120):
    """等到**每一个** proxy 副本的 router 里都恰好是 want 这组 deployment id。

    proxy 有 4 个副本，`/model/new` 只落到接请求的那一个，其余靠轮询 DB 追（实测 ~20s）。
    只 sleep 固定秒数会打到还没追上的副本 → 400 Invalid model name（08-31 第一次就栽在这）。
    删除同理，所以这里比的是**集合相等**，不是"包含"。
    """
    want = set(want)
    ps = pods()
    t0 = time.time()
    while True:
        state = {}
        for p in ps:
            out = sh(SUDO + "exec %s -- python3 -c \"import urllib.request,json,os;"
                     "r=urllib.request.Request('http://localhost:4000/model/info',"
                     "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']});"
                     "print(','.join(sorted(m['model_info']['id'] "
                     "for m in json.load(urllib.request.urlopen(r,timeout=20))['data'] "
                     "if m['model_info']['id'].startswith('zk-drill-'))))\" 2>/dev/null" % p)
            got = out.strip().splitlines()
            got = set(x for x in (got[-1].split(',') if got else []) if x)
            state[p[-5:]] = got
        if all(v == want for v in state.values()):
            print('     副本同步完成 (%.0fs)  期望=%s' % (time.time() - t0, sorted(want) or ['(空)']))
            return True
        if time.time() - t0 > timeout:
            print('     ⛔ %ds 内副本仍不一致: %s' % (timeout, {k: sorted(v) for k, v in state.items()}))
            return False
        time.sleep(8)


def fire(key, tag, timeout=300):
    if HANG:
        timeout = 900  # 得比 router 的 300s 天花板宽，否则量到的是我自己的超时
    body = json.dumps({
        'model': GROUP, 'stream': STREAM, 'tools': TOOLS,
        'instructions': 'You are a concise assistant.',
        'input': [{'type': 'message', 'role': 'user',
                   'content': [{'type': 'input_text',
                                'text': 'Reply with exactly the word %s and nothing else.' % tag}]}],
    }).encode()
    req = urllib.request.Request(BASE, data=body, headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if not STREAM:
                resp = json.load(r)
                text = ''.join(c.get('text', '')
                               for o in resp.get('output', [])
                               for c in (o.get('content') or [])
                               if c.get('type') in ('output_text', 'text'))
                return {'ok': bool(text.strip()), 'empty200': not text.strip(),
                        'dt': time.time() - t0, 'status': resp.get('status'), 'text': text[:60]}
            # 流式：额外记 ttfb（第一个字节）和流中途是否断掉
            text, ttfb, err_ev = '', None, None
            for raw in r:
                ln = raw.decode('utf-8', 'replace').strip()
                if not ln.startswith('data:'):
                    continue
                try:
                    ev = json.loads(ln[5:].strip())
                except Exception:
                    continue
                t = ev.get('type', '')
                if t == 'response.output_text.delta':
                    if ttfb is None:
                        ttfb = time.time() - t0
                    text += ev.get('delta', '')
                elif t in ('error', 'response.failed', 'response.incomplete'):
                    err_ev = t
            return {'ok': bool(text.strip()) and not err_ev, 'empty200': not text.strip(),
                    'dt': time.time() - t0, 'ttfb': ttfb, 'stream_err': err_ev,
                    'status': 'stream', 'text': text[:60]}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'dt': time.time() - t0, 'http': e.code,
                'body': e.read().decode()[:260]}
    except Exception as e:
        return {'ok': False, 'dt': time.time() - t0, 'err': '%s: %s' % (type(e).__name__, e)}


def show(tag, r):
    if r['ok']:
        d = 'OK  status=%s text=%r' % (r.get('status'), r.get('text'))
        if r.get('ttfb') is not None:
            d += '  ttfb=%.1fs' % r['ttfb']
    elif r.get('stream_err'):
        d = '流中途报错 event=%s，已收到 %r' % (r['stream_err'], r.get('text'))
    elif r.get('empty200'):
        d = '⚠️ 200 但内容为空（"假装成功"形状，LiteLLM 看不见失败→永远不会重试）'
    else:
        d = 'FAIL http=%s %s' % (r.get('http'), (r.get('body') or r.get('err', ''))[:200])
    print('  %-4s %6.1fs  %s' % (tag, r['dt'], d))


def trace(window):
    """把这一发窗口内、跟 drill 有关的路由决策行捞出来（grep 在远端做，日志很大）。"""
    out = sh(SUDO + "logs -l app=litellm-proxy --tail=-1 --since=%ds 2>/dev/null "
             "| grep -aE 'zk-drill|zk-failover-drill' | grep -avE 'register_model'"
             % int(window + 15), 240)
    picked = []
    for ln in out.splitlines():
        for dep in (DEAD_ID, LIVE_ID):
            # 只认"选中了它"的行：WA 的 weighted-pick，和 router 的 Selected deployment
            if dep in ln and ('weighted-pick' in ln or 'Selected deployment' in ln):
                if not picked or picked[-1] != dep:
                    picked.append(dep)
    return picked, out


def classify(r, picked):
    """这一发到底考没考到 failover。"""
    tried_dead = DEAD_ID in picked
    if not tried_dead:
        return 'NOT_EXERCISED', '坏 lane 压根没被选中 → 这一发不算数（不许计入成功率）'
    if r['ok']:
        return 'SAVED', '选了坏 lane 仍拿到内容 → **这一发当场换台成功**'
    return 'DROPPED', '选了坏 lane 然后直接失败 → 没换台，用户吃到这个错'


def main():
    keys = []
    added = []
    try:
        # ---------- 0 控制组 ----------
        print('=' * 74)
        print('0  控制组：组里只有好 lane（82）—— 这一段不绿，后面全部结论作废')
        print('=' * 74)
        print('  注册好 lane:', add_model(LIVE_ID, LIVE_BASE, 1))
        added.append(LIVE_ID)
        if not wait_deps([LIVE_ID]):
            return 2
        k0 = mint_key(0)
        keys.append(k0)
        c = fire(k0, 'DRILL0')
        show('C0', c)
        if not c['ok']:
            print('\n  ⛔ 控制组没通，说明这条临时通道本身就打不通（不是 failover 的问题）。')
            print('     先修通道再跑 A/B，否则"0/4"是假证据。')
            return 2

        # ---------- B 坏+好（必须在 A 之前，见文件头 ①）----------
        print()
        print('=' * 74)
        print('B  坏(weight 50) + 好(weight 1)，每发换新 key、间隔 %ds —— 这一发能不能被救活' % GAP)
        print('=' * 74)
        print('  注册坏 lane:', add_model(DEAD_ID, DEAD_BASE, 50))
        added.append(DEAD_ID)
        if not wait_deps([DEAD_ID, LIVE_ID]):
            return 2
        b = []
        for i in range(1, N + 1):
            if i > 1:
                print('  …等 %ds 让上一发的 fail-mark 过期（否则坏 lane 已被拉黑，这一发白考）' % GAP)
                time.sleep(GAP)
            k = mint_key(10 + i)
            keys.append(k)
            t0 = time.time()
            r = fire(k, 'DRILLB%d' % i)
            show('B%d' % i, r)
            picked, _ = trace(time.time() - t0)
            verdict, why = classify(r, picked)
            r['verdict'] = verdict
            print('       选中顺序: %s' % (' → '.join(x.replace('zk-drill-', '') for x in picked) or '(日志没捞到)'))
            print('       判定: %-13s %s' % (verdict, why))
            b.append(r)

        # ---------- A 只有坏 lane ----------
        a = None
        if HANG:
            print()
            print('  （--hang 模式跳过 A 段：只有坏 lane 时会连着撞 5 分钟天花板重试 N 次，'
                  '量到的只是 300×retries，没信息量）')
        else:
            print()
            print('=' * 74)
            print('A  只有坏 lane —— LiteLLM 认不认得出"它挂了"，多久认出来')
            print('=' * 74)
            print('  删好 lane:', del_model(LIVE_ID))
            added.remove(LIVE_ID)
            if not wait_deps([DEAD_ID]):
                return 2
            ka = mint_key(1)
            keys.append(ka)
            a = fire(ka, 'DRILLA')
            show('A1', a)

        # ---------- 结论 ----------
        print()
        print('=' * 74)
        print('结论')
        print('=' * 74)
        saved = [r for r in b if r['verdict'] == 'SAVED']
        dropped = [r for r in b if r['verdict'] == 'DROPPED']
        skipped = [r for r in b if r['verdict'] == 'NOT_EXERCISED']
        print('  0 控制组      : %s' % ('通' if c['ok'] else '不通'))
        print('  坏法          : %s' % ('hang（连得上但永不回，天花板 router timeout=300s）'
                                        if HANG else 'dns（解析不到，毫秒级可见）'))
        print('  线型          : %s' % ('流式' if STREAM else '非流式'))
        if a is not None:
            print('  A 只有坏 lane : %s，耗时 %.1fs' % ('仍然成功(异常!)' if a['ok'] else
                                                      ('200+空' if a.get('empty200') else
                                                       'HTTP %s' % a.get('http', '连接层错')), a['dt']))
        print('  B 有效样本    : %d 发（另有 %d 发没考到，坏 lane 没被选中）'
              % (len(saved) + len(dropped), len(skipped)))
        if not (saved or dropped):
            print('  → ⚠️ 结论不成立：一发都没真正打到坏 lane。加大 -N 或调权重重跑，')
            print('     别拿"全成功"当 failover 通过（08-31 第一版就是这么假绿的）。')
            return 2
        print('  B 结果        : 救活 %d / 掉了 %d' % (len(saved), len(dropped)))
        if saved:
            worst = max(r['dt'] for r in saved)
            print('  救活的代价    : 最慢 %.1fs（控制组 %.1fs）—— %s'
                  % (worst, c['dt'],
                     '基本无感' if worst - c['dt'] < 15 else
                     '⚠️ 用户要干等这么久才看到第一个字，"救活"了但体验是坏的'))
        if not dropped:
            print('  → ✅ 打到坏 lane 的那几发都当场换台成功，机制已存在。')
        elif saved:
            print('  → ⚠️ 换台时灵时不灵（%d 救活 / %d 掉），不能算有保障。' % (len(saved), len(dropped)))
        else:
            print('  → ❌ 缺口坐实：挑中坏 lane 的那一发直接失败，不会换台重试。')
            print('     耗时 %.1fs 就是用户要吃的等待。'
                  % (sum(r['dt'] for r in dropped) / len(dropped)))
        return 0 if not dropped else 1
    finally:
        print('\n--- 清理 ---')
        if keys:
            print('  del %d keys :' % len(keys), del_keys(keys))
        for dep in list(added):
            print('  del %-14s: %s' % (dep, del_model(dep)))


if __name__ == '__main__':
    sys.exit(main())
