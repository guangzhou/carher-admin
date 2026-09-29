#!/usr/bin/env python3
r"""往 198 生产车道 CM 加一条 `kiro-*` 道 —— 外科式文本插入，不 round-trip YAML。

为什么不用 `yaml.safe_load` + `dump`
------------------------------------
整份 config.yaml 2000+ 行。round-trip 会按 PyYAML 自己的键序/引号风格重排
**上千行无关内容**，diff 变成不可审计的一团，也就没法证明"只加了一条道"。
本脚本按一条既有条目的**文本块**原样复制、只改该改的字段，于是 diff 恒定是
纯增 N 行。断言写在代码里而不是靠肉眼比 diff。

🔴 读哪个 CM：**deploy 真挂的那个派生 CM**（`litellm-cwfb-<lane>-<hash>`）
---------------------------------------------------------------------
车道 pod 挂的不是 `cm/litellm-config`，是内容哈希派生的
`litellm-cwfb-stable-<hash>`，且 `subPath` 挂载**永不热更**。
2026-09-29 实测两者**已漂 161 行**（09-25/09-26 那批 prometheus 配置只进了派生 CM）。
⇒ 拿 `cm/litellm-config` 当输入 = 把生产**根本没挂**的东西当底改，
一次 cutover 就会把那 161 行悄悄回滚掉。取法：

    sudo kubectl -n litellm-product get deploy litellm-proxy \
      -o jsonpath='{.spec.template.spec.volumes[0].configMap.name}'

见 [[feedback_198_lane_pods_mount_derived_cwfb_cm_not_litellm_config]]。

🔴 上游 id 的连字符/点号是两套写法，别混
--------------------------------------
`model:` 里写 LiteLLM 侧的名（与既有条目同形，连字符，如 `claude-opus-5-5`），
kiro.rs 的 `map_model()` 再把它映到真上游（点号，`claude-opus-5.5`）。
`kiro_upstream_model_id` 记的是**上游真名**，是给人看的账本，不参与路由。
⚠️ `map_model()` 是 `contains()` 子串匹配 ——
`"claude-opus-5-5".contains("opus-5")` 为真，kiro.rs 侧新分支必须排在旧分支之前，
否则发新名静默落老模型且**每一格可观察量都正常**。
见 [[feedback_kiro_map_model_substring_needs_landing_ruler]]。

动了啥 / 备份 / 回滚
-------------------
本脚本**只读输入文件、只写输出文件**，不碰集群。cutover 由它打印的命令做，
带 `test` op 守卫（卷名不是预期值就整条 patch 失败，不会盖到别人刚切的 CM）。
回滚 = 把 `volumes/0/configMap/name` patch 回旧 hash，旧 CM 不删就一直在。

用法
----
    # 1. 取生产真挂的 CM 内容
    MOUNTED=$(sudo kubectl -n litellm-product get deploy litellm-proxy \
                -o jsonpath='{.spec.template.spec.volumes[0].configMap.name}')
    sudo kubectl -n litellm-product get cm "$MOUNTED" \
      -o jsonpath='{.data.config\.yaml}' > /tmp/lane-cur.yaml

    # 2. 生成新内容（纯文本，可反复跑，幂等：名字已存在就 exit 2）
    python3 scripts/kiro-lane-add.py /tmp/lane-cur.yaml /tmp/lane-new.yaml \
        --name kiro-claude-opus-5-5 \
        --template kiro-claude-opus-5 \
        --litellm-model claude-opus-5-5 \
        --upstream-id claude-opus-5.5 \
        --rate-multiplier 2.0

    # 3. 人工看 diff（应当是纯增），再按它打印的命令 cutover

退出码：0 成功 / 2 名字已存在 / 3 模板条目不唯一 / 4 缩进判不出 / 5 断言失败
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


def build_entry(src_block: str, *, name: str, litellm_model: str,
                upstream_id: str, rate_multiplier: str | None,
                tmpl_name: str, tmpl_model: str) -> str:
    """从模板文本块逐字段改出新条目。只替换点名的那几行，其余原样继承。

    刻意**不**重排/重建这个块：继承模板意味着 cache 价、window、mode 这些
    容易漏写的字段自动跟着来，而漏写一个（如 `mode`）会让 `/测模型`
    这类量具报「⚠️ 未测」—— 和"模型坏了"同形。
    """
    out = src_block
    # model_info.id 跟对外名走：add→verify→remove 的共存窗口里新旧两行 id 不能撞
    out = out.replace(f"id: kiro-rs/{tmpl_model}\n", f"id: kiro-rs/{litellm_model}\n")
    out = out.replace(f"model: anthropic/{tmpl_model}\n", f"model: anthropic/{litellm_model}\n")
    out = out.replace(f"model_name: {tmpl_name}\n", f"model_name: {name}\n")
    out = re.sub(r"kiro_upstream_model_id: \S+\n",
                 f"kiro_upstream_model_id: {upstream_id}\n", out)
    if rate_multiplier is not None:
        out = re.sub(r"kiro_rate_multiplier: \S+\n",
                     f"kiro_rate_multiplier: {rate_multiplier}\n", out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="生产真挂的那份 config.yaml（见 docstring 第 1 步）")
    ap.add_argument("dst", help="输出文件；不覆盖已存在的文件")
    ap.add_argument("--name", required=True, help="新车道名，如 kiro-claude-opus-5-5")
    ap.add_argument("--template", required=True,
                    help="照抄哪条既有道的 model_name，如 kiro-claude-opus-5")
    ap.add_argument("--litellm-model", required=True,
                   help="litellm_params.model 里的名（连字符写法，如 claude-opus-5-5）")
    ap.add_argument("--upstream-id", required=True,
                    help="kiro.rs 映射后的**上游真名**（点号写法，如 claude-opus-5.5）")
    ap.add_argument("--rate-multiplier",
                    help="kiro 的 rateMultiplier；现读 kiro-probe.py catalog，别照抄模板的")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的 dst")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    if dst.exists() and not args.force:
        # 上一轮留下的陈旧 dst 会让你 diff 一个旧产物却以为在看新的（09-29 实际踩过）
        print(f"REFUSE: {dst} 已存在 —— 先 rm，或加 --force。"
              f"陈旧输出会让 diff 骗你。", file=sys.stderr)
        return 5
    text = src.read_text()

    if f"model_name: {args.name}\n" in text:
        print(f"REFUSE: {args.name} 已在这份 CM 里 —— 幂等退出，没写任何东西",
              file=sys.stderr)
        return 2

    # 定位模板条目块：从 `- litellm_params:` 起到 `model_name: <tmpl>` 那行止
    tmpl_model = None
    m = re.search(
        r"(^(\s*)- litellm_params:\n(?:\2[ \t].*\n|\n)*?\2  model_name: "
        + re.escape(args.template) + r"\n)",
        text, re.M)
    if not m:
        print(f"REFUSE: 找不到模板道 {args.template} 的条目块", file=sys.stderr)
        return 3
    block, indent = m.group(1), m.group(2)
    if text.count(block) != 1:
        print(f"REFUSE: 模板块在文件里出现 {text.count(block)} 次，预期 1",
              file=sys.stderr)
        return 3
    mm = re.search(r"model: anthropic/(\S+)\n", block)
    if not mm:
        print("REFUSE: 模板块里没有 `model: anthropic/<id>` 行 —— "
              "这条道可能不是 kiro 形状", file=sys.stderr)
        return 4
    tmpl_model = mm.group(1)

    entry = build_entry(block, name=args.name, litellm_model=args.litellm_model,
                        upstream_id=args.upstream_id,
                        rate_multiplier=args.rate_multiplier,
                        tmpl_name=args.template, tmpl_model=tmpl_model)

    # 逐条断言。任何一条不过就一个字节都不写。
    fail = []
    if f"model_name: {args.name}\n" not in entry:
        fail.append("新条目里没有新 model_name（模板块形状与预期不符）")
    if f"model_name: {args.template}\n" in entry:
        fail.append("新条目里还留着模板的 model_name")
    if f"id: kiro-rs/{args.litellm_model}\n" not in entry:
        fail.append("新条目的 model_info.id 没改过来 —— 会和模板道撞 id")
    if f"kiro_upstream_model_id: {args.upstream_id}\n" not in entry:
        fail.append("上游 id 没写进去")
    if fail:
        for f in fail:
            print(f"REFUSE: {f}", file=sys.stderr)
        return 5

    out = text.replace(block, block + entry, 1)

    # 纯增断言：只多一条 kiro 道，模板道一字未动，长度只增
    lanes_before = text.count("model_name: kiro-")
    lanes_after = out.count("model_name: kiro-")
    checks = [
        (len(out) > len(text), "输出没有变长 —— 不是纯增"),
        (lanes_after - lanes_before == 1,
         f"kiro 道数增量 {lanes_after - lanes_before}，预期 1"),
        (out.count(f"model_name: {args.template}\n")
         == text.count(f"model_name: {args.template}\n"),
         f"模板道 {args.template} 的条目被扰动了"),
        (out.startswith(text[:m.start(1)]), "插入点之前的内容不是逐字节不变"),
    ]
    for ok, why in checks:
        if not ok:
            print(f"REFUSE: {why}", file=sys.stderr)
            return 5

    # YAML 能解析是必要条件，但**不是**充分条件：解析通过的 YAML 也可能语义错
    try:
        import yaml  # noqa: PLC0415  — 可选依赖，缺了只是少一道闸
        doc = yaml.safe_load(out)
        names = [d.get("model_name") for d in (doc.get("model_list") or [])]
        if names.count(args.name) != 1:
            print(f"REFUSE: 解析后 {args.name} 出现 {names.count(args.name)} 次，预期 1",
                  file=sys.stderr)
            return 5
        if len(names) != len(set(names)) and names.count(args.name) == 1:
            dup = {n for n in names if names.count(n) > 1}
            print(f"note: CM 里本来就有重名道（非本次引入）：{sorted(dup)}")
    except ImportError:
        print("note: 没有 pyyaml，跳过解析闸 —— cutover 前请让 LiteLLM 自己验")

    dst.write_text(out)
    new_hash = hashlib.sha256(out.encode()).hexdigest()
    print(f"OK: {src} {len(text)}B -> {dst} {len(out)}B (+{len(out) - len(text)}B)")
    print(f"    kiro 道 {lanes_before} -> {lanes_after}；"
          f"model_name 总数 {text.count('model_name:')} -> {out.count('model_name:')}")
    print(f"    新内容 sha256 = {new_hash}")
    print(f"    列表缩进 = {len(indent)} 空格；模板上游 = {tmpl_model}")
    print()
    print("下一步（先看 diff 应当是纯增，再 cutover）：")
    print(f"  diff {src} {dst}")
    print(f"  NEW=litellm-cwfb-stable-{new_hash[:12]}")
    print("  sudo kubectl -n litellm-product create cm $NEW "
          f"--from-file=config.yaml={dst}")
    print("  # 🔴 带 test op 的守卫 patch：卷名不是 <OLD> 就整条失败，")
    print("  #    不会盖掉别人刚切的 CM。<OLD> 填你第 1 步读到的那个名字。")
    print("  sudo kubectl -n litellm-product patch deploy litellm-proxy --type=json -p "
          "'[{\"op\":\"test\",\"path\":\"/spec/template/spec/volumes/0/configMap/name\","
          "\"value\":\"<OLD>\"},"
          "{\"op\":\"replace\",\"path\":\"/spec/template/spec/volumes/0/configMap/name\","
          "\"value\":\"'$NEW'\"}]'")
    print("  sudo kubectl -n litellm-product rollout status deploy/litellm-proxy")
    print("  # 验收三层：① 每个**在役**副本（⚠️ 排掉 Terminating 的）内 "
          "`wc -c /app/config.yaml` == 上面的字节数")
    print("  #           ② /model_group/info 里有这个组且窗口/价对")
    print("  #           ③ scripts/kiro-lane-regress.sh（EXPECT_LANES 记得 +1）")
    print(f"  # 回滚：把卷名 patch 回 <OLD>，旧 CM 不删就一直在")
    return 0


if __name__ == "__main__":
    sys.exit(main())
