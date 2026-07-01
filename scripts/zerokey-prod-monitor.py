#!/usr/bin/env python3
"""
zerokey prod 容量看板 —— 在 k8s-work-226（有 carher kubectl + 能连 prod litellm ClusterIP）跑。

盯三件事：
  1) 容量/溢出：gpt 三路花费 zerokey-pool / chatgpt-acct(fallback1) / wangsu(fallback2) 的占比 +
     两次快照间的 delta（$ 和 $/min）。wangsu 占比/速率上升 = zerokey 饱和在溢出（付费）。
  2) 成本：51 个目标 key 的 spend / budget / 利用率。
  3) 全局流量：今日 api_requests / tokens。

429 显式计数 REST 不直接暴露；用 wangsu(custom_openai/gpt-5.5) 花费速率作饱和代理信号。

state 存 ~/.zerokey-prod-monitor.json，用于算 delta。
用法：
  python3 zerokey-prod-monitor.py            # 文本看板
  python3 zerokey-prod-monitor.py --json      # 输出 JSON（喂 canvas）
  TARGETS=2,3,1000 python3 zerokey-prod-monitor.py
"""
import base64, json, os, subprocess, sys, time, urllib.request, datetime

NS = 'carher'
SVC = os.environ.get('PROXY_URL', 'http://192.168.35.175:4000')
SECRET = 'litellm-secrets'
STATE = os.path.expanduser('~/.zerokey-prod-monitor.json')

DEFAULT_TARGETS = ('2,3,5,7,17,20,22,25,47,50,52,57,62,79,80,82,85,87,94,116,126,127,130,'
                   '138,143,145,147,155,157,171,173,178,179,181,183,185,187,194,195,204,207,'
                   '217,218,220,223,246,253,258,262,269,1000')

ROUTES = [
    ('zerokey-pool  (188 真额度, primary)', 'openai/zerokey-pool'),
    ('chatgpt-acct  (Aliyun Pro, fallback1)', 'openai/chatgpt-gpt-5.5'),
    ('wangsu gpt5.5 (付费, fallback2)', 'custom_openai/gpt-5.5'),
]


def sh(c):
    return subprocess.check_output(c).decode()


def mk():
    return base64.b64decode(sh(['kubectl', 'get', 'secret', SECRET, '-n', NS,
                                '-o', 'jsonpath={.data.LITELLM_MASTER_KEY}'])).decode()


def api(MK, path):
    req = urllib.request.Request(SVC + path,
                                 headers={'Authorization': f'Bearer {MK}'})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def targets():
    return ['her-' + t.strip() for t in os.environ.get('TARGETS', DEFAULT_TARGETS).split(',') if t.strip()]


def collect(MK):
    # model spend (cumulative)
    ms = {r['model']: r.get('total_spend', 0.0) for r in api(MK, '/global/spend/models')
          if isinstance(r, dict)}
    routes = [(label, m, round(ms.get(m, 0.0), 2)) for label, m in ROUTES]

    # per-key spend/budget
    d = json.loads(sh(['kubectl', 'get', 'herinstance', '-n', NS, '-o', 'json']))
    by = {it['metadata']['name']: it for it in d['items']}
    keys = []
    for t in targets():
        k = (by.get(t, {}).get('spec') or {}).get('litellmKey')
        if not k:
            continue
        try:
            i = api(MK, f'/key/info?key={k}').get('info') or {}
        except Exception:
            continue
        keys.append({'her': t, 'alias': i.get('key_alias'),
                     'spend': round(i.get('spend') or 0.0, 2),
                     'budget': i.get('max_budget')})

    # global activity today
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    try:
        act = api(MK, f'/global/activity?start_date={today}&end_date={today}')
        gtoday = {'requests': act.get('sum_api_requests'), 'tokens': act.get('sum_total_tokens')}
    except Exception:
        gtoday = {'requests': None, 'tokens': None}

    return {'ts': time.time(), 'routes': routes, 'keys': keys, 'today': gtoday}


def deltas(cur):
    prev = None
    if os.path.exists(STATE):
        try:
            prev = json.load(open(STATE))
        except Exception:
            prev = None
    out = {}
    if prev:
        dt = max(cur['ts'] - prev['ts'], 1) / 60.0  # minutes
        pmap = {m: s for _, m, s in prev['routes']}
        for label, m, s in cur['routes']:
            d = round(s - pmap.get(m, s), 4)
            out[m] = {'delta': d, 'rate_per_min': round(d / dt, 4)}
        out['_elapsed_min'] = round(dt, 1)
    json.dump(cur, open(STATE, 'w'))
    return out


def fmt(cur, dl):
    L = []
    ts = datetime.datetime.fromtimestamp(cur['ts'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    L.append(f'=== zerokey prod 容量看板 @ {ts} ===')
    el = dl.get('_elapsed_min')
    L.append(f'(距上次快照 {el} min；首次运行无 delta)' if el else '(首次快照，无 delta；再跑一次看速率)')
    L.append('')
    L.append('── GPT 三路花费（累计 $，delta=本次-上次）──')
    tot = sum(s for _, _, s in cur['routes']) or 1
    for label, m, s in cur['routes']:
        d = dl.get(m, {})
        pct = 100 * s / tot
        ds = f"  Δ={d.get('delta'):+.4f}$  {d.get('rate_per_min'):+.4f}$/min" if d else ''
        L.append(f'  {label:38s} {s:>12.2f}$  ({pct:4.1f}%){ds}')
    zk = next((s for lb, m, s in cur['routes'] if m == 'openai/zerokey-pool'), 0)
    wg = next((s for lb, m, s in cur['routes'] if m == 'custom_openai/gpt-5.5'), 0)
    drate = dl.get('custom_openai/gpt-5.5', {}).get('rate_per_min')
    if drate is not None:
        flag = '⚠ wangsu 付费溢出在涨（zerokey 可能饱和）' if drate > 0.001 else '✓ wangsu 溢出无明显增长'
        L.append(f'  → 饱和信号: {flag}')
    L.append('')
    L.append(f"── 今日全局流量: requests={cur['today']['requests']} tokens={cur['today']['tokens']} ──")
    L.append('')
    ks = sorted(cur['keys'], key=lambda x: x['spend'], reverse=True)
    tspend = sum(k['spend'] for k in ks)
    L.append(f'── 51 个目标 key spend（累计 $），合计 {tspend:.2f}$，Top 12 ──')
    for k in ks[:12]:
        b = k['budget']
        util = f"{100*k['spend']/b:4.1f}%" if b else '  -  '
        L.append(f"  {k['alias']:14s} {k['spend']:>10.2f}$ / budget {str(b):>7}  util {util}")
    return '\n'.join(L)


def main():
    MK = mk()
    cur = collect(MK)
    dl = deltas(cur)
    if '--json' in sys.argv:
        print(json.dumps({'snapshot': cur, 'deltas': dl}, ensure_ascii=False))
    else:
        print(fmt(cur, dl))


if __name__ == '__main__':
    sys.exit(main())
