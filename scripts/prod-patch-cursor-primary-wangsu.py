#!/usr/bin/env python3
"""
198 prod: cursor 类用户默认走网宿 wangsu-gpt-5.5，客户端仍发 OpenAI 原生名 gpt-5.5。

改动（litellm-product/litellm-config）：
  1. model_group_alias: gpt-5.5 -> wangsu-gpt-5.5（原 chatgpt-gpt-5.5）
  2. 可选 identity: wangsu-gpt-5.5 -> wangsu-gpt-5.5（防止历史 alias 指回 chatgpt）
  3. 批量给 cursor-* key allowlist 补上 wangsu-gpt-5.5（走 /key/update，不直写 DB）

不动：chatgpt-gpt-5.5 仍在 allowlist；显式请求 chatgpt-gpt-5.5 仍走 ChatGPT 池。
fallback chatgpt-gpt-5.5 -> wangsu-gpt-5.5 保留。

Run: python3 scripts/prod-patch-cursor-primary-wangsu.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

REMOTE_SCRIPT = r"""
import datetime, json, subprocess, sys, yaml

NS = 'litellm-product'
CM = 'litellm-config'
MANIFEST = '/root/litellm-product-manifests/30-cm-litellm-config.yaml'
NODEPORT = '30402'

ALIAS_CHANGES = {
    'gpt-5.5': 'wangsu-gpt-5.5',
    'wangsu-gpt-5.5': 'wangsu-gpt-5.5',
}

raw = subprocess.check_output(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json'])
cm = json.loads(raw)
data = yaml.safe_load(cm['data']['config.yaml'])

known = {m['model_name'] for m in data['model_list']}
for src, dst in ALIAS_CHANGES.items():
    if dst not in known:
        print(f'ERROR: alias target "{dst}" not in model_list', file=sys.stderr)
        sys.exit(1)

router = data.setdefault('router_settings', {})
aliases = router.setdefault('model_group_alias', {})

alias_updates = []
for src, dst in ALIAS_CHANGES.items():
    if aliases.get(src) != dst:
        aliases[src] = dst
        alias_updates.append(f'{src} -> {dst}')

if alias_updates:
    print('alias updates:')
    for line in alias_updates:
        print(f'  {line}')
else:
    print('aliases already correct')

if alias_updates:
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = f'{MANIFEST}.bak-cursor-wangsu-{stamp}'
    with open(backup, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f'backup -> {backup}')

    cm['data']['config.yaml'] = yaml.dump(
        data, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    with open(MANIFEST, 'w') as f:
        json.dump(cm, f)

    subprocess.check_call(['kubectl', 'apply', '-f', MANIFEST, '-n', NS])
    subprocess.check_call(['kubectl', 'rollout', 'restart', 'deployment/litellm-proxy', '-n', NS])
    subprocess.check_call(['kubectl', 'rollout', 'status', 'deployment/litellm-proxy',
                           '-n', NS, '--timeout=180s'])
    print('proxy rollout done')
else:
    print('skip proxy restart (no alias change)')

# --- allowlist: append wangsu-gpt-5.5 to cursor-* keys missing it ---
import urllib.request

MK = subprocess.check_output(
    ['kubectl', 'get', 'secret', 'litellm-secrets', '-n', NS,
     '-o', 'jsonpath={.data.LITELLM_MASTER_KEY}']
).decode()
MK = __import__('base64').b64decode(MK).decode()

psql = [
    'kubectl', 'exec', 'litellm-db-0', '-n', NS, '--',
    'psql', '-U', 'litellm', '-d', 'litellm', '-t', '-A', '-F', '|', '-c',
    '''SELECT token, key_alias, array_to_json(models)::text
       FROM "LiteLLM_VerificationToken"
       WHERE key_alias LIKE 'cursor-%'
         AND NOT ('wangsu-gpt-5.5' = ANY(models));'''
]
rows = subprocess.check_output(psql).decode().strip().split('\n')
rows = [r for r in rows if r.strip()]

print(f'cursor keys missing wangsu-gpt-5.5 in allowlist: {len(rows)}')
ok = fail = 0
for row in rows:
    token, alias, models_json = row.split('|', 2)
    models = json.loads(models_json)
    if 'wangsu-gpt-5.5' in models:
        continue
    models = list(models) + ['wangsu-gpt-5.5']
    body = json.dumps({'key': token, 'models': models}).encode()
    req = urllib.request.Request(
        f'http://localhost:{NODEPORT}/key/update',
        data=body,
        headers={'Authorization': f'Bearer {MK}', 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode())
        if out.get('key_alias') or out.get('key'):
            ok += 1
        else:
            fail += 1
            print(f'FAIL {alias}: {out}')
    except Exception as e:
        fail += 1
        print(f'FAIL {alias}: {e}')

print(f'allowlist patch: ok={ok} fail={fail}')
if fail:
    sys.exit(1)
print('done.')
"""


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    jms = os.path.join(here, 'jms')

    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(REMOTE_SCRIPT)
        local_path = f.name

    try:
        subprocess.check_call([jms, 'scp', local_path,
                               'AIYJY-litellm:/tmp/_prod_patch_cursor_wangsu.py'])
        return subprocess.call([jms, 'ssh', 'AIYJY-litellm',
                                'python3 /tmp/_prod_patch_cursor_wangsu.py'])
    finally:
        os.unlink(local_path)


if __name__ == '__main__':
    sys.exit(main())
