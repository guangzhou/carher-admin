#!/usr/bin/env python3
"""
198 prod (litellm-product / NodePort 30402): 把新的 zerokey 账号端口加入 config-managed
`zerokey-pool` 负载均衡组。

prod 的 zerokey-pool 是 **config-managed**（不是 DB-managed）：6+ 个静态 model_list 块在
litellm-config ConfigMap 的 config.yaml 里，每块形如
    - model_name: zerokey-pool
      litellm_params: {model: openai/gpt-5-5, api_base: http://10.68.13.188:<port>/v1,
                       api_key: raw, use_chat_completions_api: true,
                       input_cost_per_token: 0, output_cost_per_token: 0}
（live 实测：GET /v1/model/info 里 zerokey-pool 全部 db_model=False）

本脚本：
  1. 读 live cm config.yaml，按端口幂等追加缺失的 zerokey-pool 块（复刻现有块的全部字段）。
  2. 有变更 → kubectl apply cm + rolling restart litellm-proxy（零中断，4 副本滚动）+ rollout status。
  3. 同步回写 manifest 源文件 /root/litellm-product-manifests/30-cm-litellm-config.yaml（防漂移）。
  4. 校验 GET /v1/model/info 中 zerokey-pool deployment 数。

用法（本机）：
    python3 scripts/prod-add-zerokey-accounts.py --ports 8129-8133          # dry-run
    python3 scripts/prod-add-zerokey-accounts.py --ports 8129-8133 --apply  # 执行
    python3 scripts/prod-add-zerokey-accounts.py --ports 8129,8131 --apply  # 离散端口
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

REMOTE_SCRIPT = r"""
import copy, datetime, json, os, subprocess, sys, urllib.request, yaml

NS = 'litellm-product'
CM = 'litellm-config'
MANIFEST = '/root/litellm-product-manifests/30-cm-litellm-config.yaml'
NODEPORT = '30402'
TARGET = 'zerokey-pool'
HOST = '10.68.13.188'

PORTS = [int(p) for p in os.environ['PORTS'].split(',') if p]
APPLY = os.environ.get('APPLY') == '1'

def sh(cmd):
    return subprocess.check_output(cmd).decode()

MK = __import__('base64').b64decode(
    sh(['kubectl', 'get', 'secret', 'litellm-secrets', '-n', NS,
        '-o', 'jsonpath={.data.LITELLM_MASTER_KEY}'])).decode()

def api(path):
    req = urllib.request.Request(
        f'http://localhost:{NODEPORT}{path}',
        headers={'Authorization': f'Bearer {MK}'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

cm = json.loads(sh(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json']))
cfg = yaml.safe_load(cm['data']['config.yaml'])
model_list = cfg.setdefault('model_list', [])

zk = [m for m in model_list if m.get('model_name') == TARGET]
if not zk:
    print('ERROR: no existing zerokey-pool block to use as template', file=sys.stderr)
    sys.exit(3)

existing_bases = {m.get('litellm_params', {}).get('api_base') for m in zk}
template = zk[-1]

added = []
for port in PORTS:
    base = f'http://{HOST}:{port}/v1'
    if base in existing_bases:
        print(f'  port {port}: already present (skip)')
        continue
    block = copy.deepcopy(template)
    block['litellm_params']['api_base'] = base
    model_list.append(block)
    added.append(port)
    print(f'  port {port}: ADD {base}')

print(f'\n=== plan === add {len(added)} block(s): {added}')
if not added:
    print('nothing to add; verifying live count...')
elif not APPLY:
    print('\nDRY-RUN (pass --apply to execute). No changes made.')
    sys.exit(0)

if added and APPLY:
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    cm['data']['config.yaml'] = yaml.dump(cfg, default_flow_style=False,
                                          allow_unicode=True, sort_keys=False)
    newcm = f'/tmp/_zk_add_cm_{stamp}.json'
    with open(newcm, 'w') as f:
        json.dump(cm, f)
    subprocess.check_call(['kubectl', 'apply', '-f', newcm, '-n', NS])
    subprocess.check_call(['kubectl', 'rollout', 'restart', 'deployment/litellm-proxy', '-n', NS])
    # LiteLLM ~90s initialDelay x 4 replicas rolling can exceed 300s; non-fatal so the
    # manifest-sync + verify steps below always run even if status check times out.
    try:
        subprocess.check_call(['kubectl', 'rollout', 'status', 'deployment/litellm-proxy',
                               '-n', NS, '--timeout=600s'])
        print('proxy rollout done')
    except subprocess.CalledProcessError:
        print('WARN: rollout status timed out (proxy still converging); continuing to '
              'manifest sync + verify')

    # manifest drift sync
    try:
        raw = open(MANIFEST).read()
        try:
            man = json.loads(raw); fmt = 'json'
        except Exception:
            man = yaml.safe_load(raw); fmt = 'yaml'
        live = json.loads(sh(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json']))
        if man.get('data', {}).get('config.yaml') != live['data']['config.yaml']:
            with open(f'{MANIFEST}.bak-{stamp}', 'w') as f:
                f.write(raw)
            man.setdefault('data', {})['config.yaml'] = live['data']['config.yaml']
            with open(MANIFEST, 'w') as f:
                if fmt == 'json':
                    json.dump(man, f)
                else:
                    yaml.dump(man, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            print(f'manifest synced ({fmt}); backup .bak-{stamp}')
        else:
            print('manifest already in sync')
    except FileNotFoundError:
        print(f'WARN: manifest {MANIFEST} not found; skipped sync')

# verify live router (allow a moment for workers to load)
import time
for _ in range(12):
    rows = [m for m in api('/v1/model/info').get('data', []) if m.get('model_name') == TARGET]
    if not added or len(rows) >= len(existing_bases) + len(added):
        break
    time.sleep(5)
print(f'\nlive zerokey-pool deployments: {len(rows)}')
for m in rows:
    lp = m.get('litellm_params', {})
    print('  ', lp.get('api_base'), 'db=' + str(m.get('model_info', {}).get('db_model')))
print('done.')
"""


def parse_ports(spec: str) -> str:
    out = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-')
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return ','.join(str(p) for p in out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--ports', required=True,
                    help='ports to add, e.g. "8129-8133" or "8129,8131"')
    ap.add_argument('--apply', action='store_true', help='execute (default: dry-run)')
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    jms = os.path.join(here, 'jms')

    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(REMOTE_SCRIPT)
        local_path = f.name

    env_prefix = (
        f"PORTS={parse_ports(args.ports)} "
        f"APPLY={'1' if args.apply else '0'} "
    )
    try:
        subprocess.check_call([jms, 'scp', local_path,
                               'AIYJY-litellm:/tmp/_prod_add_zerokey_accounts.py'])
        return subprocess.call([jms, 'ssh', 'AIYJY-litellm',
                                env_prefix + 'python3 /tmp/_prod_add_zerokey_accounts.py'])
    finally:
        os.unlink(local_path)


if __name__ == '__main__':
    sys.exit(main())
