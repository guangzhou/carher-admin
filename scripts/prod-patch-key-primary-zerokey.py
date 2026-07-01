#!/usr/bin/env python3
"""
198 prod (litellm-product / NodePort 30402): 让**单个** cursor key 首选 zerokey-pool，
其它 key 与全局路由零影响。

效果（仅目标 key）：客户端发 gpt-5.5 或 chatgpt-gpt-5.5 时
    zerokey-pool  →(挂)  chatgpt-gpt-5.5(ChatGPT 账户池)  →(再挂)  wangsu-gpt-5.5

机制（实测定型，见 docs/zerokey-fleet-pool-plan.md v2.10 / skill litellm-key-provider-swap）：
  1. 「首选」这一跳：在目标 key 上设 per-key `aliases`（gpt-5.5/chatgpt-gpt-5.5 -> zerokey-pool）。
     per-key alias 优先级高于全局 model_group_alias，且只影响该 key。
  2. 「兜底链」：LiteLLM **没有可靠的 per-key fallback**（实测 per-key router_settings.fallbacks
     兜不住，2026-06-23），所以必须在**全局** router_settings.fallbacks 里给 zerokey-pool
     加一条 [chatgpt-gpt-5.5, wangsu-gpt-5.5]。该条目只有调 zerokey-pool 的 key 会触发，
     别人根本不调 zerokey-pool，对其它 key 零影响。
  3. 防漂移：直接 kubectl apply live configmap 会让源文件
     /root/litellm-product-manifests/30-cm-litellm-config.yaml 落后；本脚本同步回写该 manifest。

幂等：重复运行只补未应用的差异；全局 fallback 已存在则不重启 proxy。

用法（本机）：
    # 预览（默认 dry-run，不改任何东西）
    python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian
    # 真正执行
    python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian --apply
    # 回滚某 key（清空 alias，回到全局默认链）
    python3 scripts/prod-patch-key-primary-zerokey.py --key-match cursor-liuguoxian --rollback --apply
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

REMOTE_SCRIPT = r"""
import base64, datetime, json, os, subprocess, sys, urllib.request, yaml

NS = 'litellm-product'
CM = 'litellm-config'
MANIFEST = '/root/litellm-product-manifests/30-cm-litellm-config.yaml'
NODEPORT = '30402'

TARGET = 'zerokey-pool'
ALIAS_SRC = ['gpt-5.5', 'chatgpt-gpt-5.5']
FALLBACK_CHAIN = ['chatgpt-gpt-5.5', 'wangsu-gpt-5.5']

KEY_MATCH = os.environ['KEY_MATCH']
APPLY = os.environ.get('APPLY') == '1'
ROLLBACK = os.environ.get('ROLLBACK') == '1'

def sh(cmd):
    return subprocess.check_output(cmd).decode()

MK = base64.b64decode(
    sh(['kubectl', 'get', 'secret', 'litellm-secrets', '-n', NS,
        '-o', 'jsonpath={.data.LITELLM_MASTER_KEY}'])
).decode()

def api(path, body=None, method='GET'):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f'http://localhost:{NODEPORT}{path}', data=data,
        headers={'Authorization': f'Bearer {MK}', 'Content-Type': 'application/json'},
        method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

# ---------- 1. resolve target key (unique) ----------
matches = []
for pg in range(1, 6):
    try:
        d = api(f'/key/list?page={pg}&size=100&return_full_object=true')
    except Exception:
        break
    keys = d.get('keys') or []
    if not keys:
        break
    for k in keys:
        if not isinstance(k, dict):
            continue
        al = (k.get('key_alias') or '')
        uid = (k.get('user_id') or '')
        if al.startswith(KEY_MATCH) or uid == KEY_MATCH or KEY_MATCH in al:
            matches.append(k)

uniq = {k['token']: k for k in matches}
matches = list(uniq.values())
if len(matches) != 1:
    print(f'ERROR: key match "{KEY_MATCH}" -> {len(matches)} keys: '
          f'{[m.get("key_alias") for m in matches]}', file=sys.stderr)
    sys.exit(2)
key = matches[0]
token = key['token']
cur_aliases = key.get('aliases') or {}
cur_models = list(key.get('models') or [])
print(f'target key: alias={key.get("key_alias")} user_id={key.get("user_id")}')
print(f'  current aliases: {cur_aliases}')

# ---------- 2. global fallback (live cm) ----------
cm = json.loads(sh(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json']))
cfg = yaml.safe_load(cm['data']['config.yaml'])
router = cfg.setdefault('router_settings', {})
fallbacks = router.setdefault('fallbacks', [])

known = {m['model_name'] for m in cfg.get('model_list', [])}
# zerokey-pool may be DB-managed (not in config model_list) -> verify via live router
live_groups = {m.get('model_name') for m in api('/v1/model/info').get('data', [])}

def fb_has_target():
    for fb in fallbacks:
        if TARGET in fb:
            return fb[TARGET]
    return None

cm_changed = False
if ROLLBACK:
    print('rollback mode: leaving global fallback in place (harmless; only target key triggers it)')
else:
    if TARGET not in live_groups:
        print(f'ERROR: model group "{TARGET}" not present in live router '
              f'(register the pool first)', file=sys.stderr)
        sys.exit(3)
    existing = fb_has_target()
    if existing == FALLBACK_CHAIN:
        print(f'global fallback ok: {TARGET} -> {existing}')
    else:
        if existing is None:
            fallbacks.append({TARGET: list(FALLBACK_CHAIN)})
            print(f'global fallback ADD: {TARGET} -> {FALLBACK_CHAIN}')
        else:
            for fb in fallbacks:
                if TARGET in fb:
                    fb[TARGET] = list(FALLBACK_CHAIN)
            print(f'global fallback FIX: {TARGET} {existing} -> {FALLBACK_CHAIN}')
        cm_changed = True

# ---------- 3. per-key aliases + allowlist ----------
new_aliases = dict(cur_aliases)
new_models = list(cur_models)
key_changed = False
if ROLLBACK:
    for s in ALIAS_SRC:
        if new_aliases.pop(s, None) is not None:
            key_changed = True
    print(f'rollback aliases -> {new_aliases}')
else:
    for s in ALIAS_SRC:
        if new_aliases.get(s) != TARGET:
            new_aliases[s] = TARGET
            key_changed = True
    if TARGET not in new_models:
        new_models.append(TARGET)
        key_changed = True
    print(f'target aliases -> {new_aliases}')
    print(f'allowlist has {TARGET}: {TARGET in new_models}')

# ---------- summary / apply ----------
print('\n=== plan ===')
print(f'  cm fallback change: {cm_changed}')
print(f'  key change:         {key_changed}')
if not APPLY:
    print('\nDRY-RUN (pass --apply to execute). No changes made.')
    sys.exit(0)

if cm_changed:
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    cm['data']['config.yaml'] = yaml.dump(cfg, default_flow_style=False,
                                          allow_unicode=True, sort_keys=False)
    newcm = f'/tmp/_zk_cm_{stamp}.json'
    with open(newcm, 'w') as f:
        json.dump(cm, f)
    subprocess.check_call(['kubectl', 'apply', '-f', newcm, '-n', NS])
    subprocess.check_call(['kubectl', 'rollout', 'restart',
                           'deployment/litellm-proxy', '-n', NS])
    subprocess.check_call(['kubectl', 'rollout', 'status', 'deployment/litellm-proxy',
                           '-n', NS, '--timeout=300s'])
    print('proxy rollout done')

# ---------- 4. sync manifest source file (drift fix), always ----------
try:
    raw = open(MANIFEST).read()
    try:
        man = json.loads(raw); man_fmt = 'json'
    except Exception:
        man = yaml.safe_load(raw); man_fmt = 'yaml'
    live = json.loads(sh(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json']))
    if man.get('data', {}).get('config.yaml') != live['data']['config.yaml']:
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        with open(f'{MANIFEST}.bak-{stamp}', 'w') as f:
            f.write(raw)
        man.setdefault('data', {})['config.yaml'] = live['data']['config.yaml']
        with open(MANIFEST, 'w') as f:
            if man_fmt == 'json':
                json.dump(man, f)
            else:
                yaml.dump(man, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        print(f'manifest synced ({man_fmt}); backup .bak-{stamp}')
    else:
        print('manifest already in sync')
except FileNotFoundError:
    print(f'WARN: manifest {MANIFEST} not found; skipped sync')

if key_changed:
    out = api('/key/update', {'key': token, 'aliases': new_aliases, 'models': new_models},
              method='POST')
    info = api(f'/key/info?key={token}').get('info', {})
    print(f'key updated: aliases={info.get("aliases")} '
          f'{TARGET}_in_models={TARGET in (info.get("models") or [])}')
else:
    print('key already correct; no update')

print('done.')
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--key-match', required=True,
                    help='key_alias prefix / user_id / substring uniquely identifying the key')
    ap.add_argument('--apply', action='store_true', help='execute (default: dry-run)')
    ap.add_argument('--rollback', action='store_true', help='remove per-key aliases')
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    jms = os.path.join(here, 'jms')

    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(REMOTE_SCRIPT)
        local_path = f.name

    env_prefix = (
        f"KEY_MATCH={args.key_match} "
        f"APPLY={'1' if args.apply else '0'} "
        f"ROLLBACK={'1' if args.rollback else '0'} "
    )
    try:
        subprocess.check_call([jms, 'scp', local_path,
                               'AIYJY-litellm:/tmp/_prod_patch_key_primary_zerokey.py'])
        return subprocess.call([jms, 'ssh', 'AIYJY-litellm',
                                env_prefix + 'python3 /tmp/_prod_patch_key_primary_zerokey.py'])
    finally:
        os.unlink(local_path)


if __name__ == '__main__':
    sys.exit(main())
