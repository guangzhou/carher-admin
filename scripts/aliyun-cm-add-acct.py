#!/usr/bin/env python3
"""给阿里云 litellm ConfigMap 的每个 chatgpt model 组追加新 acct entry。

用法: aliyun-cm-add-acct.py <in.yaml> <out.yaml> <acct> [acct...]

为什么走 YAML 结构而不是文本行:
  prod(litellm-config) 是手写格式, model_info 是单行 flow:
      model_info: {mode: responses, id: chatgpt-acct-78/gpt-5.4}
  canary(litellm-config-canary) 被 helm 重新序列化过, 是多行 block 且 key 顺序不同:
      model_info:
        id: chatgpt-acct-78/gpt-5.4
        mode: responses
      model_name: gpt-5.4
  早期版本用"找 model_info 那一行"的文本匹配, 只认 prod 的单行写法, 在 canary 上
  7 个组全部 "no marker" → 加 0 条 → 校验失败(2026-07-25 实证)。
  改成解析 YAML、按 model_info.id 定位模板、deepcopy 后改 api_base/id, 两种格式
  都能吃, 且天然不会写出坏缩进。
"""
import copy
import re
import sys

import yaml

IDS = ["chatgpt-gpt-5.5", "chatgpt-gpt-5.6-sol", "chatgpt-gpt-5.6-terra",
       "chatgpt-gpt-5.6-luna", "gpt-5.4", "gpt-5.3-codex", "gpt-5.3-codex-spark"]


def entry_id(e):
    if not isinstance(e, dict):
        return ""
    return str((e.get("model_info") or {}).get("id", ""))


def main():
    inp, out, accts = sys.argv[1], sys.argv[2], sys.argv[3:]
    doc = yaml.safe_load(open(inp))
    model_list = doc.get("model_list")
    if not isinstance(model_list, list):
        print("  ❌ model_list 缺失或不是 list", file=sys.stderr)
        return 1

    added = 0
    for suffix in IDS:
        pat = re.compile(rf"chatgpt-acct-(\d+)/{re.escape(suffix)}$")
        # 该 model 组已有的 acct + 最后一条当模板
        tmpl_idx, tmpl_n, have = None, None, set()
        for i, e in enumerate(model_list):
            m = pat.fullmatch(entry_id(e))
            if m:
                tmpl_idx, tmpl_n = i, m.group(1)
                have.add(m.group(1))
        if tmpl_idx is None:
            print(f"  ⚠ no marker for {suffix}", file=sys.stderr)
            continue

        new = [n for n in accts if n not in have]
        if not new:
            continue

        block = []
        for n in new:
            e = copy.deepcopy(model_list[tmpl_idx])
            e.setdefault("litellm_params", {})["api_base"] = \
                f"http://chatgpt-acct-{n}.carher.svc:4000"
            e.setdefault("model_info", {})["id"] = f"chatgpt-acct-{n}/{suffix}"
            block.append(e)
        model_list[tmpl_idx + 1:tmpl_idx + 1] = block
        added += len(new)
        print(f"    ✓ {suffix}: +{len(new)} (template from acct-{tmpl_n})")

    with open(out, "w") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=10**6)
    print(f"  total entries added: {added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
