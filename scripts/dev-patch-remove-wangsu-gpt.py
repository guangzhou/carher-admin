#!/usr/bin/env python3
"""
Patch 198 dev ConfigMap (litellm-dev/litellm-config) — 原子操作：

1. 从 model_list 删除 `wangsu-gpt-5.4` 和 `wangsu-gpt-5.5`
2. 在 router_settings.model_group_alias 加 4 个别名兜底，让旧名字调用
   透明改走 ChatGPT Pro 后端：

     wangsu-gpt-5.4   -> chatgpt-gpt-5.4
     wangsu-gpt-5.5   -> chatgpt-gpt-5.5
     gpt-5.4          -> chatgpt-gpt-5.4   (顺便修死引用)
     gpt-5.3-codex    -> chatgpt-gpt-5.3-codex   (顺便修死引用)

背景：dev 上 wangsu-gpt-5.4/5.5 过去 7 天 0 次调用，
但 556+ 个 key 的 allowlist 仍持有这些名字。直接删模型定义会让旧调用
失败，所以用 router-level alias 兜底（用户无感）。

非 GPT 的 wangsu 模型保留：wangsu-gemini-3.1-pro-preview /
wangsu-deepseek-v4-pro / wangsu-deepseek-v4-flash。

幂等：重复运行只补未应用的差异。
Run from your laptop:  python3 scripts/dev-patch-remove-wangsu-gpt.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

REMOTE_SCRIPT = r"""
import datetime, json, subprocess, sys, yaml

NS = 'litellm-dev'
CM = 'litellm-config'
MANIFEST = '/root/litellm-dev/30-cm-litellm-config.yaml'

MODELS_TO_REMOVE = {'wangsu-gpt-5.4', 'wangsu-gpt-5.5'}
DESIRED_ALIASES = {
    'wangsu-gpt-5.4': 'chatgpt-gpt-5.4',
    'wangsu-gpt-5.5': 'chatgpt-gpt-5.5',
    'gpt-5.4':        'chatgpt-gpt-5.4',
    'gpt-5.3-codex':  'chatgpt-gpt-5.3-codex',
}

raw = subprocess.check_output(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json'])
cm = json.loads(raw)
data = yaml.safe_load(cm['data']['config.yaml'])

model_list = data['model_list']
remaining_names = {m['model_name'] for m in model_list if m['model_name'] not in MODELS_TO_REMOVE}

# 1. Sanity check: alias targets must exist after the removals
for src, dst in DESIRED_ALIASES.items():
    if dst not in remaining_names:
        print(f'ERROR: alias target "{dst}" not in model_list', file=sys.stderr)
        sys.exit(1)

# 2. Filter model_list
new_model_list = [m for m in model_list if m['model_name'] not in MODELS_TO_REMOVE]
removed = [m['model_name'] for m in model_list if m['model_name'] in MODELS_TO_REMOVE]

# 3. Patch aliases
router = data.setdefault('router_settings', {})
aliases = router.setdefault('model_group_alias', {})
added_aliases = []
for src, dst in DESIRED_ALIASES.items():
    if aliases.get(src) != dst:
        aliases[src] = dst
        added_aliases.append(f'{src} -> {dst}')

if not removed and not added_aliases:
    print('no changes needed, exiting')
    sys.exit(0)

print(f'removed from model_list ({len(removed)}):')
for m in removed:
    print(f'  - {m}')
print(f'aliases added/updated ({len(added_aliases)}):')
for line in added_aliases:
    print(f'  + {line}')

# 4. Backup + apply
data['model_list'] = new_model_list

stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup = f'/root/litellm-dev/30-cm-litellm-config.yaml.bak-{stamp}'
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
                       '-n', NS, '--timeout=120s'])
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
                               'AIYJY-litellm:/tmp/_patch_remove_wangsu_gpt.py'])
        return subprocess.call([jms, 'ssh', 'AIYJY-litellm',
                                'python3 /tmp/_patch_remove_wangsu_gpt.py'])
    finally:
        os.unlink(local_path)


if __name__ == '__main__':
    sys.exit(main())
