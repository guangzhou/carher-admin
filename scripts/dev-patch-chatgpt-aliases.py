#!/usr/bin/env python3
"""
Patch 198 dev ConfigMap (litellm-dev/litellm-config) to add three
`router_settings.model_group_alias` entries so that Codex Desktop IDE's
hardcoded model picker (which can only write OpenAI native names back to
config.toml) routes correctly to the ChatGPT Pro backend on 188.

Aliases added:
  gpt-5.5         -> chatgpt-gpt-5.5
  gpt-5.4-mini    -> chatgpt-gpt-5.3-codex-spark
  gpt-5.2         -> chatgpt-gpt-5.4

(Note: chatgpt-gpt-5.3-instant and chatgpt-gpt-5.3-chat-latest are NOT
valid alias targets — the upstream ChatGPT Pro backend rejects them with
"not supported when using Codex with a ChatGPT account".)

Bug reference: https://github.com/openai/codex/issues/19694
  "Codex Desktop App model picker filters out models returned from
   model_catalog_json" — open, no fix.

Idempotent — re-running adds only the missing aliases.
Run from your laptop:  python3 scripts/dev-patch-chatgpt-aliases.py
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

DESIRED_ALIASES = {
    'gpt-5.5':      'chatgpt-gpt-5.5',
    'gpt-5.4-mini': 'chatgpt-gpt-5.3-codex-spark',
    'gpt-5.2':      'chatgpt-gpt-5.4',
}

raw = subprocess.check_output(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json'])
cm = json.loads(raw)
data = yaml.safe_load(cm['data']['config.yaml'])

router = data.setdefault('router_settings', {})
aliases = router.setdefault('model_group_alias', {})

# Sanity check: target models exist in model_list
known = {m['model_name'] for m in data['model_list']}
for src, dst in DESIRED_ALIASES.items():
    if dst not in known:
        print(f'ERROR: alias target "{dst}" not in model_list', file=sys.stderr)
        sys.exit(1)

added = []
for src, dst in DESIRED_ALIASES.items():
    if aliases.get(src) != dst:
        aliases[src] = dst
        added.append(f'{src} -> {dst}')

if not added:
    print('all 3 aliases already present, exiting')
    sys.exit(0)

print(f'adding {len(added)} aliases:')
for line in added:
    print(f'  {line}')

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
                               'AIYJY-litellm:/tmp/_patch_chatgpt_aliases.py'])
        return subprocess.call([jms, 'ssh', 'AIYJY-litellm',
                                'python3 /tmp/_patch_chatgpt_aliases.py'])
    finally:
        os.unlink(local_path)


if __name__ == '__main__':
    sys.exit(main())
