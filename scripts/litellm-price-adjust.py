#!/usr/bin/env python3
"""
litellm-price-adjust.py — LiteLLM ConfigMap 价格批量乘 multiplier

读 k8s/litellm-proxy.yaml (或 198 manifest)，对匹配 model_name 的 4 个 cost
字段乘以 multiplier。行级正则处理（不解析整个 yaml），保留原格式 / 注释 /
缩进，git diff 只显示价格变化不显示重排。

字段：
  - input_cost_per_token
  - output_cost_per_token
  - cache_read_input_token_cost
  - cache_creation_input_token_cost

用法：
  ./scripts/litellm-price-adjust.py --multiplier 2.5 --models "claude-"
  ./scripts/litellm-price-adjust.py --multiplier 2.5 --models "claude-,anthropic.claude-,openrouter-claude-"
  ./scripts/litellm-price-adjust.py --multiplier 2.5 --models "claude-" --apply       # 真改文件
  ./scripts/litellm-price-adjust.py --multiplier 0.4 --models "claude-"               # 反向：×0.4 = 回到原 2.5x 之前

注意：multiplier 是"在当前价格上乘"。如果之前已乘 2.5，现在再乘 2 = 累积 5x。
要做"恢复原价"，git checkout 旧版本 yaml 或反向乘（如 1/2.5 = 0.4）。
"""

import argparse, re, subprocess, sys, pathlib

COST_FIELDS = [
    'input_cost_per_token',
    'output_cost_per_token',
    'cache_read_input_token_cost',
    'cache_creation_input_token_cost',
]


def adjust(yaml_path: pathlib.Path, multiplier: float, model_patterns: list[str]) -> list[tuple[int, str, str, str, str]]:
    """
    返回 list of (line_no, model_name, field, old_val_str, new_val_str)
    并 in-place 改写 yaml_path 的内容（写到内存，不写文件，调用方决定）。
    """
    current_model = None
    matching = False
    changes = []

    cost_re = re.compile(r'^(\s+)(' + '|'.join(COST_FIELDS) + r'):\s+(\S+)\s*(#.*)?$')
    model_re = re.compile(r'^\s*-\s*model_name:\s+(\S+)')

    with open(yaml_path) as f:
        lines = f.readlines()

    for i, line in enumerate(lines, start=1):
        m = model_re.match(line)
        if m:
            current_model = m.group(1)
            matching = any(p in current_model for p in model_patterns)
            continue
        if matching and current_model:
            cm = cost_re.match(line)
            if cm:
                indent, field, val_str, comment = cm.groups()
                try:
                    old_val = float(val_str)
                except ValueError:
                    continue
                new_val = old_val * multiplier
                # 保留 scientific notation 风格（yaml 里普遍 1.5e-05 这种）
                new_val_str = format_scientific(new_val)
                tail = f"  {comment}" if comment else ""
                new_line = f"{indent}{field}: {new_val_str}{tail}\n"
                lines[i-1] = new_line
                changes.append((i, current_model, field, val_str, new_val_str))

    return changes, lines


def format_scientific(v: float) -> str:
    """1.5e-05 风格，跟 yaml 原值习惯一致"""
    if v == 0:
        return "0.0"
    # 5.0e-06 / 2.5e-05 这种风格
    s = f"{v:.4e}"
    # 收尾去多余 0：5.0000e-06 → 5.0e-06
    mantissa, exp = s.split('e')
    mantissa = mantissa.rstrip('0').rstrip('.') or '0'
    if '.' not in mantissa:
        mantissa += '.0'
    # exp: -06 → -06 (保留两位)
    exp = exp.replace('+', '')
    if exp.startswith('-') and len(exp) == 2:
        exp = '-0' + exp[1:]
    elif exp.isdigit() and len(exp) == 1:
        exp = '0' + exp
    return f"{mantissa}e{exp}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--multiplier', type=float, required=True)
    ap.add_argument('--models', default='claude-',
                    help='逗号分隔的 model_name 子串匹配模式 (默认: claude-)')
    ap.add_argument('--yaml', default='k8s/litellm-proxy.yaml',
                    help='yaml 文件路径 (默认: k8s/litellm-proxy.yaml)')
    ap.add_argument('--apply', action='store_true',
                    help='真改文件 (默认 dry-run 只显示 diff)')
    args = ap.parse_args()

    yaml_path = pathlib.Path(args.yaml)
    if not yaml_path.exists():
        print(f"ERROR: {yaml_path} 不存在", file=sys.stderr)
        sys.exit(2)

    patterns = [p.strip() for p in args.models.split(',') if p.strip()]
    print(f"[price-adjust] yaml={yaml_path} multiplier={args.multiplier} patterns={patterns} mode={'APPLY' if args.apply else 'DRY-RUN'}")

    changes, new_lines = adjust(yaml_path, args.multiplier, patterns)

    if not changes:
        print("  (no matching model + cost field found)")
        sys.exit(0)

    print(f"\n  affected: {len(changes)} cost fields across {len(set(c[1] for c in changes))} model_name entries")
    print(f"  preview (first 20):")
    for line_no, model, field, old, new in changes[:20]:
        print(f"    L{line_no:5d}  {model:50s} {field:35s}  {old} → {new}")
    if len(changes) > 20:
        print(f"    ... and {len(changes)-20} more")

    if not args.apply:
        print(f"\n  DRY-RUN: 加 --apply 才会真改 (改完记得 git diff 看具体变更 + kubectl apply)")
        sys.exit(0)

    # 真写
    with open(yaml_path, 'w') as f:
        f.writelines(new_lines)
    print(f"\n  ✅ {yaml_path} 已更新 ({len(changes)} 行)")
    print(f"  下一步:")
    print(f"    git diff {yaml_path} | head -60     # 看变更")
    print(f"    kubectl apply -f {yaml_path}        # 让 LiteLLM Proxy 读到新价")
    print(f"    kubectl rollout restart deployment/litellm-proxy -n carher  # 触发新 Pod 加载")


if __name__ == '__main__':
    main()
