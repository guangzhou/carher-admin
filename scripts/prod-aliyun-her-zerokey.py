#!/usr/bin/env python3
"""
prod Aliyun (carher ns / litellm-proxy): 批量让 N 个 her 的 gpt 走 zerokey-pool。

链路: her -> litellm-proxy(Aliyun prod) -> zerokey-pool(api_base cc.auto-link/pro)
      -> 198 zerokey-pool -> 188 zerokey 容器

机制(沿用 198 / canary 已验证配方):
  - register: prod litellm-proxy 热加 1 个 zerokey-pool deployment(STORE_MODEL_IN_DB=True,免重启)
  - keys:     逐个 her 的 vkey 设 per-key alias {chatgpt-gpt-5.5 -> zerokey-pool} + allowlist += zerokey-pool
              (per-key alias 只影响该 key;别的 her 不受影响)
  - switch:   her CRD spec.model = gpt(默认切到 gpt;rollback 回 sonnet)
  全局 fallback(zerokey-pool -> [chatgpt-gpt-5.5, wangsu-gpt-5.5])单独在 CM 里加 + 滚动重启,不在本脚本。

幂等;支持 --rollback 反向。密钥不入库:LINK_KEY / TARGETS 走环境变量。
在 k8s-work-226(有 carher kubectl)上运行:
  TARGETS=2,3,5 LINK_KEY=sk-xxx python3 prod-aliyun-her-zerokey.py register --apply
  TARGETS=2,3,5 python3 prod-aliyun-her-zerokey.py keys --apply
  TARGETS=2,3,5 python3 prod-aliyun-her-zerokey.py switch --apply
  TARGETS=2,3,5 python3 prod-aliyun-her-zerokey.py verify
"""
import argparse, base64, json, os, subprocess, sys, urllib.request

NS = 'carher'
# k8s-work-226 has kubectl but no in-cluster svc DNS; reach prod litellm-proxy via
# its ClusterIP (override with PROXY_URL env if the svc IP changes).
SVC = os.environ.get('PROXY_URL', 'http://192.168.35.175:4000')
SECRET = 'litellm-secrets'
TARGET = 'zerokey-pool'
ALIAS_SRC = 'chatgpt-gpt-5.5'
LINK_BASE = 'https://cc.auto-link.com.cn/pro/v1'
MODEL_ID = 'zerokey-pool-198-link'


def sh(c):
    return subprocess.check_output(c).decode()


def mk():
    return base64.b64decode(sh(['kubectl', 'get', 'secret', SECRET, '-n', NS,
                                '-o', 'jsonpath={.data.LITELLM_MASTER_KEY}'])).decode()


def api(MK, path, body=None, method='GET'):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SVC + path, data=data,
                                 headers={'Authorization': f'Bearer {MK}',
                                          'Content-Type': 'application/json'},
                                 method=method)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def targets():
    return ['her-' + t.strip() for t in os.environ['TARGETS'].split(',') if t.strip()]


def her_keys():
    d = json.loads(sh(['kubectl', 'get', 'herinstance', '-n', NS, '-o', 'json']))
    by = {it['metadata']['name']: it for it in d['items']}
    out = {}
    for t in targets():
        if t not in by:
            print('WARN missing CRD', t); out[t] = None; continue
        out[t] = (by[t].get('spec') or {}).get('litellmKey')
    return out


def cmd_register(MK, apply, rollback):
    info = api(MK, '/v1/model/info')
    present = [m for m in info.get('data', []) if m.get('model_name') == TARGET]
    if rollback:
        for m in present:
            mid = (m.get('model_info') or {}).get('id')
            print(('deleting ' if apply else 'would delete ') + str(mid))
            if apply:
                api(MK, '/model/delete', {'id': mid}, method='POST')
        return
    if present:
        print('zerokey-pool already present, deployments=', len(present)); return
    if not apply:
        print('would register zerokey-pool ->', LINK_BASE); return
    LINK_KEY = os.environ['LINK_KEY']
    api(MK, '/model/new', {
        'model_name': TARGET,
        'litellm_params': {'model': 'openai/zerokey-pool', 'api_base': LINK_BASE,
                           'api_key': LINK_KEY},
        'model_info': {'mode': 'responses', 'id': MODEL_ID},
    }, method='POST')
    info = api(MK, '/v1/model/info')
    n = len([m for m in info.get('data', []) if m.get('model_name') == TARGET])
    print('registered zerokey-pool; live deployments=', n)


def cmd_keys(MK, apply, rollback):
    km = her_keys()
    done = 0
    for t, key in km.items():
        if not key:
            print('SKIP no key', t); continue
        info = api(MK, f'/key/info?key={key}').get('info') or {}
        models = list(info.get('models') or [])
        aliases = dict(info.get('aliases') or {})
        changed = False
        if rollback:
            if aliases.pop(ALIAS_SRC, None) is not None:
                changed = True
        else:
            if aliases.get(ALIAS_SRC) != TARGET:
                aliases[ALIAS_SRC] = TARGET; changed = True
            if TARGET not in models:
                models.append(TARGET); changed = True
        if not changed:
            print('ok  ', t); continue
        if not apply:
            print('would update', t, '-> aliases', aliases); continue
        api(MK, '/key/update', {'key': key, 'aliases': aliases, 'models': models},
            method='POST')
        done += 1
        print('updated', t)
    print(f'keys changed: {done}/{len(km)}')


def cmd_switch(MK, apply, rollback):
    newmodel = 'sonnet' if rollback else 'gpt'
    for t in targets():
        if not apply:
            print('would set', t, 'model=', newmodel); continue
        subprocess.check_call(['kubectl', '-n', NS, 'patch', 'herinstance', t,
                               '--type', 'merge', '-p',
                               json.dumps({'spec': {'model': newmodel}})])
        print('patched', t, '-> model', newmodel)
    print('switch done ->', newmodel)


def cmd_fallback(MK, apply, rollback):
    import yaml, datetime
    CM = 'litellm-config'
    CHAIN = ['chatgpt-gpt-5.5', 'wangsu-gpt-5.5']
    cm = json.loads(sh(['kubectl', 'get', 'cm', CM, '-n', NS, '-o', 'json']))
    cfg = yaml.safe_load(cm['data']['config.yaml'])
    fb = cfg.setdefault('router_settings', {}).setdefault('fallbacks', [])
    cur = next((x[TARGET] for x in fb if TARGET in x), None)
    changed = False
    if rollback:
        cfg['router_settings']['fallbacks'] = [x for x in fb if TARGET not in x]
        changed = cur is not None
        print('rollback: removed zerokey-pool fallback' if changed else 'no zerokey-pool fallback to remove')
    else:
        if cur == CHAIN:
            print('fallback already present:', cur)
        elif cur is None:
            fb.append({TARGET: list(CHAIN)}); changed = True; print('ADD fallback', TARGET, '->', CHAIN)
        else:
            for x in fb:
                if TARGET in x:
                    x[TARGET] = list(CHAIN)
            changed = True; print('FIX fallback', cur, '->', CHAIN)
    if not changed:
        print('no CM change needed'); return
    if not apply:
        print('DRY-RUN (pass --apply to edit CM + rolling restart)'); return
    cm['data']['config.yaml'] = yaml.dump(cfg, default_flow_style=False,
                                          allow_unicode=True, sort_keys=False)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    p = f'/tmp/_pz_cm_{stamp}.json'
    with open(p, 'w') as f:
        json.dump(cm, f)
    subprocess.check_call(['kubectl', 'apply', '-f', p, '-n', NS])
    subprocess.check_call(['kubectl', 'rollout', 'restart', 'deployment/litellm-proxy', '-n', NS])
    try:
        subprocess.check_call(['kubectl', 'rollout', 'status', 'deployment/litellm-proxy',
                               '-n', NS, '--timeout=600s'])
        print('rollout done')
    except subprocess.CalledProcessError:
        print('WARN rollout status timed out (litellm ~90s startup); verify manually')


def cmd_verify(MK, apply, rollback):
    km = her_keys()
    t, key = next(((t, k) for t, k in km.items() if k), (None, None))
    if not key:
        print('no key to verify'); return
    req = urllib.request.Request(
        SVC + '/v1/responses',
        data=json.dumps({'model': 'chatgpt-gpt-5.5',
                         'input': 'reply with the single word pong'}).encode(),
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        method='POST')
    with urllib.request.urlopen(req, timeout=90) as r:
        mid = r.headers.get('x-litellm-model-id')
        body = json.loads(r.read().decode())
    txt = ''
    for o in (body.get('output') or []):
        for c in (o.get('content') or []):
            if c.get('text'):
                txt = c['text']
    print(f'verify {t}: x-litellm-model-id={mid} text={txt[:40]!r}')
    print('PASS' if mid == MODEL_ID else 'WARN: not routed to zerokey-pool')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['register', 'fallback', 'keys', 'switch', 'verify'])
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    MK = mk()
    {'register': cmd_register, 'fallback': cmd_fallback, 'keys': cmd_keys,
     'switch': cmd_switch, 'verify': cmd_verify}[a.cmd](MK, a.apply, a.rollback)


if __name__ == '__main__':
    sys.exit(main())
