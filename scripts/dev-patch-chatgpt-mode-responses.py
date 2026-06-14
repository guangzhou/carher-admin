#!/usr/bin/env python3
"""
Patch 198 dev ConfigMap (litellm-dev/litellm-config) so that every chatgpt-*
model entry carries `model_info.mode: responses`.

Required for OpenAI Codex CLI (which talks /v1/responses, not /v1/chat/completions).
Idempotent — re-running is a no-op once all entries are tagged.

Run from your laptop:  python3 scripts/dev-patch-chatgpt-mode-responses.py
The work happens on AIYJY-litellm via jms.
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

raw = subprocess.check_output(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json'])
cm = json.loads(raw)
data = yaml.safe_load(cm['data']['config.yaml'])

stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup = f'/root/litellm-dev/30-cm-litellm-config.yaml.bak-{stamp}'
with open(backup, 'w') as f:
    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
print(f'backup -> {backup}')

patched = []
for m in data['model_list']:
    if m['model_name'].startswith('chatgpt-'):
        info = m.setdefault('model_info', {})
        if info.get('mode') != 'responses':
            info['mode'] = 'responses'
            patched.append(m['model_name'])

if not patched:
    print('all chatgpt-* entries already have mode: responses, exiting')
    sys.exit(0)

print(f'patched {len(patched)} entries: {patched}')

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
                               'AIYJY-litellm:/tmp/_patch_chatgpt_mode.py'])
        return subprocess.call([jms, 'ssh', 'AIYJY-litellm',
                                'python3 /tmp/_patch_chatgpt_mode.py'])
    finally:
        os.unlink(local_path)


if __name__ == '__main__':
    sys.exit(main())
