#!/usr/bin/env python3
"""给 198 LiteLLM 的 18 条 kiro-* 道标定 token 价格（按上游模型的官方价）。

用法:
    python3 scripts/litellm-198-kiro-pricing.py <config.yaml 输入> <输出>

只改 `model_info.id` 以 `kiro-rs/` 开头的块，按 `kiro_upstream_model_id` 查价表；
表里没有的（`auto`）原样不动。纯行级手术，不做 YAML round-trip，所以其余 74 条道
以及注释/顺序/缩进零变化 —— 判据是 diff 行数 == 改动条目数 * 期望行数。
"""
import re
import sys

# 单位：USD / token。来源见每行注释；全部为「上游厂商第一方目录价」。
# 交叉核对：Anthropic 官方 pricing 页 + OpenAI platform pricing 页（2026-09-23 实取），
# 其余来自 litellm 内置 model_cost 的第一方条目（zai/ deepseek/ minimax/ bedrock qwen）。
PRICES = {
    # OpenAI 官方：gpt-5.6-sol $4.00 in / $0.40 cached / $20.00 out（实取 platform.openai.com）
    "gpt-5.6-sol":      dict(i=4e-06, o=2e-05,   cr=4e-07,  cw=5e-06),
    "gpt-5.6-terra":    dict(i=2e-06, o=1.2e-05, cr=2e-07,  cw=2.5e-06),
    "gpt-5.6-luna":     dict(i=2e-07, o=1.2e-06, cr=2e-08,  cw=2.5e-07),
    # DeepSeek 官方 V3.2
    "deepseek-3.2":     dict(i=2.8e-07, o=4e-07, cr=2.8e-08),
    # MiniMax 官方
    "minimax-m2.5":     dict(i=3e-07, o=1.2e-06, cr=3e-08,  cw=3.75e-07),
    "minimax-m2.1":     dict(i=3e-07, o=1.2e-06, cr=3e-08,  cw=3.75e-07),
    # z.ai 官方 GLM-5（cache write 不单独计费）
    "glm-5":            dict(i=1e-06, o=3.2e-06, cr=2e-07,  cw=0.0),
    # 阿里官方目录未公开 qwen3-coder-next 单价，取 AWS Bedrock us-east-1 目录价
    "qwen3-coder-next": dict(i=5e-07, o=1.2e-06),
    # Anthropic 官方：Opus 全系 $5/$25，Sonnet 5 $2/$10，Sonnet 4.x $3/$15，Haiku 4.5 $1/$5
    "claude-opus-5":    dict(i=5e-06, o=2.5e-05, cr=5e-07,  cw=6.25e-06),
    "claude-opus-4.8":  dict(i=5e-06, o=2.5e-05, cr=5e-07,  cw=6.25e-06),
    "claude-opus-4.7":  dict(i=5e-06, o=2.5e-05, cr=5e-07,  cw=6.25e-06),
    "claude-opus-4.6":  dict(i=5e-06, o=2.5e-05, cr=5e-07,  cw=6.25e-06),
    "claude-opus-4.5":  dict(i=5e-06, o=2.5e-05, cr=5e-07,  cw=6.25e-06),
    "claude-sonnet-5":  dict(i=2e-06, o=1e-05,   cr=2e-07,  cw=2.5e-06),
    "claude-sonnet-4.6": dict(i=3e-06, o=1.5e-05, cr=3e-07, cw=3.75e-06),
    "claude-sonnet-4.5": dict(i=3e-06, o=1.5e-05, cr=3e-07, cw=3.75e-06),
    "claude-haiku-4.5": dict(i=1e-06, o=5e-06,   cr=1e-07,  cw=1.25e-06),
    # ⚠️ `auto` 由 kiro.rs 自选模型，没有官方对标价。2026-09-23 由用户裁决按
    # claude-sonnet-5 标定 —— 这是**约定价不是官方价**，改口径时先来改这一行。
    "auto":             dict(i=2e-06, o=1e-05,   cr=2e-07,  cw=2.5e-06),
}


def fmt(x: float) -> str:
    """YAML 里写成 repr，避免 1e-06 被某些 parser 当字符串。"""
    return repr(float(x))


def patch(text: str):
    lines = text.split("\n")
    # 切块：每条 model_list 条目以 "- litellm_params:" 起（顶格 2 缩进的 list item）
    starts = [n for n, l in enumerate(lines) if l == "- litellm_params:"]
    if not starts:
        sys.exit("FATAL: 找不到任何 '- litellm_params:' 块，输入文件形状不对")
    starts.append(len(lines))

    out, touched, skipped = [], [], []
    cursor = 0
    for a, b in zip(starts, starts[1:]):
        out.extend(lines[cursor:a])
        block = lines[a:b]
        cursor = b

        body = "\n".join(block)
        m = re.search(r"^    id: kiro-rs/", body, re.M)
        if not m:
            out.extend(block)
            continue
        um = re.search(r"^    kiro_upstream_model_id: (\S+)$", body, re.M)
        if not um:
            skipped.append("<no kiro_upstream_model_id>")
            out.extend(block)
            continue
        upstream = um.group(1)
        p = PRICES.get(upstream)
        if p is None:
            skipped.append(upstream)
            out.extend(block)
            continue

        new, in_params = [], False
        for l in block:
            if l == "- litellm_params:":
                in_params = True
                new.append(l)
                continue
            if l == "  model_info:":
                in_params = False
                new.append(l)
                # model_info 的第一行是 id:，价格行插在 id 之后由下面的分支处理
                continue
            if l == "    input_cost_per_token: 0.0":
                new.append(f"    input_cost_per_token: {fmt(p['i'])}")
                if not in_params:
                    if "cr" in p:
                        new.append(f"    cache_read_input_token_cost: {fmt(p['cr'])}")
                    if "cw" in p:
                        new.append(f"    cache_creation_input_token_cost: {fmt(p['cw'])}")
                continue
            if l == "    output_cost_per_token: 0.0":
                new.append(f"    output_cost_per_token: {fmt(p['o'])}")
                continue
            new.append(l)
        if new == block:
            sys.exit(f"FATAL: {upstream} 块匹配到了却一行没改 —— 价格行形状变了，停手")
        touched.append(upstream)
        out.extend(new)
    out.extend(lines[cursor:])
    return "\n".join(out), touched, skipped


def main():
    src, dst = sys.argv[1], sys.argv[2]
    text = open(src, encoding="utf-8").read()
    new, touched, skipped = patch(text)

    import yaml
    old_y, new_y = yaml.safe_load(text), yaml.safe_load(new)
    if len(old_y["model_list"]) != len(new_y["model_list"]):
        sys.exit("FATAL: model_list 条数变了")
    # 非 kiro 条目必须逐字节等价
    for o, n in zip(old_y["model_list"], new_y["model_list"]):
        if not str(o.get("model_info", {}).get("id", "")).startswith("kiro-rs/"):
            if o != n:
                sys.exit(f"FATAL: 非 kiro 条目被改动: {o.get('model_name')}")
    open(dst, "w", encoding="utf-8").write(new)
    print(f"已标价 {len(touched)} 条: {', '.join(touched)}")
    print(f"跳过（价表中无）{len(skipped)} 条: {', '.join(skipped) or '(none)'}")
    print(f"输出 -> {dst}")


if __name__ == "__main__":
    main()
