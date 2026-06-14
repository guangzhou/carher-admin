#!/usr/bin/env python3
"""
PROD 环境 ChatGPT Pro 叠加方案（B-Lite）

在 prod LiteLLM ConfigMap (litellm-product/litellm-config) 上：
  1. 新增 4 个 chatgpt-* model_list 条目（指向 188 ChatGPT Pro 容器）
       - chatgpt-gpt-5.5
       - chatgpt-gpt-5.4
       - chatgpt-gpt-5.3-codex
       - chatgpt-gpt-5.3-codex-spark
     （仅 ChatGPT 账户接受的 4 个；不加 -instant / -chat-latest / -5.4-pro）
  2. 新增 2 条 fallback：
       - chatgpt-gpt-5.5  -> wangsu-gpt-5.5
       - chatgpt-gpt-5.4  -> wangsu-gpt-5.4
     （chatgpt 端挂掉 / 撞 ChatGPT 限速时，自动回退到 wangsu）

不动现有任何条目：
  - wangsu-gpt-5.4 / wangsu-gpt-5.5 / 其他 wangsu-* 全部保留（model_list）
  - 现有 router_settings.model_group_alias / fallbacks 全部保留
  - 现有 callbacks 全部保留

== 风险与缓解 ==
- ChatGPT Pro 单账号速率限制：用户主动选用才会撞，且 fallback 兜底
- 188 单点：没新增依赖（只是新增可选路径，旧路径不动）
- 计费体系：旧 wangsu-gpt-* 流量不受影响；新 chatgpt-* 流量 spend=0 是已知问题

== 用户面影响 ==
- 现有 wangsu-gpt-5.4 / wangsu-gpt-5.5 / gpt-5.4 等调用方零感知
- 想用 ChatGPT Pro 模型的用户：
  1. 找运维把 chatgpt-gpt-* 加进自己 prod key 的 allowlist
  2. 客户端调 model 名换成 chatgpt-gpt-5.5 / -5.4 / -5.3-codex / -5.3-codex-spark

幂等：重复运行只补未应用的差异。
Run from your laptop:  python3 scripts/prod-add-chatgpt-overlay.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

REMOTE_SCRIPT = r"""
import datetime, json, os, subprocess, sys, yaml

NS = 'litellm-product'
CM = 'litellm-config'
MANIFEST = '/root/litellm-product-manifests/30-cm-litellm-config.yaml'

API_BASE_188 = 'http://10.68.13.188:4000'
API_KEY_188 = os.environ.get('CHATGPT_188_MASTER_KEY', '')
if not API_KEY_188:
    raise SystemExit('CHATGPT_188_MASTER_KEY is required')

NEW_MODELS = ['chatgpt-gpt-5.5', 'chatgpt-gpt-5.4',
              'chatgpt-gpt-5.3-codex', 'chatgpt-gpt-5.3-codex-spark']

NEW_FALLBACKS = [
    {'chatgpt-gpt-5.5': ['wangsu-gpt-5.5']},
    {'chatgpt-gpt-5.4': ['wangsu-gpt-5.4']},
]

def model_entry(name):
    return {
        'model_name': name,
        'litellm_params': {
            'model': f'openai/{name}',
            'api_base': API_BASE_188,
            'api_key':  API_KEY_188,
        },
        'model_info': {
            'id': f'chatgpt/{name.replace("chatgpt-", "")}',
            'input_cost_per_token':  0.0,
            'output_cost_per_token': 0.0,
            'mode': 'responses',
        },
    }

raw = subprocess.check_output(['kubectl', 'get', 'cm', '-n', NS, CM, '-o', 'json'])
cm = json.loads(raw)
data = yaml.safe_load(cm['data']['config.yaml'])

# 1. 加新模型条目（幂等）
existing_names = {m['model_name'] for m in data['model_list']}
added_models = []
for name in NEW_MODELS:
    if name not in existing_names:
        data['model_list'].append(model_entry(name))
        added_models.append(name)

# 2. 加新 fallback（幂等：检查整条规则是否已存在）
router = data.setdefault('router_settings', {})
fallbacks = router.setdefault('fallbacks', [])
def fb_signature(fb):
    return tuple((k, tuple(sorted(v))) for k, v in sorted(fb.items()))
existing_keys = {fb_signature(efb) for efb in fallbacks}
added_fallbacks = []
for fb in NEW_FALLBACKS:
    if fb_signature(fb) not in existing_keys:
        fallbacks.append(fb)
        added_fallbacks.append(fb)

if not added_models and not added_fallbacks:
    print('all changes already applied, exiting')
    sys.exit(0)

print('changes:')
if added_models:
    print(f'  + model_list: added {len(added_models)} entries')
    for m in added_models:
        print(f'      - {m}')
if added_fallbacks:
    print(f'  + router_settings.fallbacks: added {len(added_fallbacks)} chains')
    for fb in added_fallbacks:
        for k, v in fb.items():
            print(f'      - {k} -> {v}')

# 3. backup + apply
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup = f'/root/litellm-product-manifests/30-cm-litellm-config.yaml.bak-{stamp}'
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
                               'AIYJY-litellm:/tmp/_prod_add_chatgpt_overlay.py'])
        return subprocess.call([jms, 'ssh', 'AIYJY-litellm',
                                'python3 /tmp/_prod_add_chatgpt_overlay.py'])
    finally:
        os.unlink(local_path)


if __name__ == '__main__':
    sys.exit(main())
