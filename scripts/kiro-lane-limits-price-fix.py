#!/usr/bin/env python3
r"""给 198 生产车道 CM 的 19 条 `kiro-*` 道修三件事 —— 外科式文本手术，不 round-trip YAML。

2026-10-01 加 kiro id14 后逐格核对 CM / kiro 活目录 / 官方第一方价，查出三处偏差：

1. **`max_output_tokens` 19 条道全缺**（整份 CM 89 条道里只有 4 条有这个字段）。
   09-25 盘点已实测「kiro 自己的目录带这个值、可以照抄不用实测」，但没落地。
2. **gpt-5.6 三条道 `max_input_tokens` 填了 272000，而 kiro 这条上游给 1000000**。
   272000 是 **codex/ChatGPT 账号那条路**的窗口，被抄到 kiro 道上了。
   开着 `enable_pre_call_checks` ⇒ 超 272k 的请求被我们这侧提前判超长并转走兜底，
   而上游其实吃得下 ⇒ 白扔 73% 可用窗口。
3. **`kiro-claude-opus-5-5` 价贵标了 25%**：写的是 opus-5 的 $5/$25/$0.5/$6.25，
   而 Anthropic 官方 opus-5.5 是 **$4/$20/$0.2/$5**（claude.com/pricing 实取 2026-10-01，
   litellm `model_prices_and_context_window.json` 第一方条目逐格一致）。
   根因：09-29 加这条道时 `--template kiro-claude-opus-5` 照抄了模板的价，
   而 opus-5.5 是降价的一代，模板没跟着改。

🔴 取值真源 = **kiro 自己的 `ListAvailableModels`**（`--catalog` 喂进来），不是硬编
----------------------------------------------------------------------------------
窗口/输出上限一律从 catalog 的 `tokenLimits` 读。**不读 `litellm-198-model-ceilings.json`**：
那张表是 codex 目录口径（`gpt-5.6-sol: 872000`），而 kiro 道走的是 AWS CodeWhisperer
这条**完全不同的上游**，同一个模型名在两条上游的窗口本就不同（872k vs 1M）。
拿 codex 的数去填 kiro 道，就是 272000 这个错值的同一类错误、换个方向再犯一次。
⇒ 修完要往那张表补 `kiro-*` 前缀的条目，否则审计尺子会把正确值判成 OVER（见 §尾）。

🔴 价格两块都写
--------------
LiteLLM **计费读 `litellm_params`**；只写 `model_info` 会 200 且读得回，但照旧按老价记
（`litellm-198-fill-missing-prices.py` 的实证）。所以 opus-5.5 的四个价
`litellm_params` 与 `model_info` 两处都改。窗口/输出上限只在 `model_info`。

门（缺一不改）
-------------
1. 每处改动**断言旧值等于预期**，旧值不对 ⇒ 立刻停（前提错了）。
2. 结构门：改后重新 YAML 解析，`model_list` 条数不变，
   **除目标字段外每条 deployment 逐字段相等**（含 19 条道自己的其余字段）。
3. 只增不删：`max_output_tokens` 是新增行，断言新增行数 == 19。
4. 本脚本**只读输入文件、只写输出文件，不碰集群**。cutover 由它打印的命令做，
   带 `test` op 守卫（卷名不是预期值就整条 patch 失败，不会盖掉别人刚切的 CM）。

用法
----
    MOUNTED=$(sudo kubectl -n litellm-product get deploy litellm-proxy \
                -o jsonpath='{.spec.template.spec.volumes[0].configMap.name}')
    sudo kubectl -n litellm-product get cm "$MOUNTED" \
      -o jsonpath='{.data.config\.yaml}' > /tmp/cm-cur.yaml
    python3 scripts/kiro-probe.py catalog --json > /tmp/kiro-cat.json
    python3 scripts/kiro-lane-limits-price-fix.py /tmp/cm-cur.yaml /tmp/cm-new.yaml \
        --catalog /tmp/kiro-cat.json --old-cm "$MOUNTED"

退出码：0 成功 / 2 输入形状不对 / 3 断言失败（旧值与预期不符）/ 4 结构门失败
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# Anthropic 官方 opus-5.5 价（claude.com/pricing 2026-10-01 实取；
# litellm model_prices_and_context_window.json 的第一方 `claude-opus-5-5` 条目逐格一致）。
# 旧值是 opus-5 的价（模板照抄留下的），写进 EXPECT 作为断言前提。
OPUS55_PRICE = {
    "input_cost_per_token":            ("5e-06",    "4e-06"),
    "output_cost_per_token":           ("2.5e-05",  "2e-05"),
    "cache_read_input_token_cost":     ("5e-07",    "2e-07"),
    "cache_creation_input_token_cost": ("6.25e-06", "5e-06"),
}

# gpt-5.6 三条道的输入窗口：272000 是 codex 口径，kiro 这条上游给 1000000。
# 新值不写死在这里 —— 从 catalog 读；这里只记「旧值必须是这个」作为断言前提。
GPT_OLD_MAX_INPUT = "272000"
GPT_LANES = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


def load_catalog(path: str) -> dict[str, dict]:
    """从 kiro `ListAvailableModels` 读 tokenLimits —— 窗口/输出上限的唯一真源。"""
    raw = Path(path).read_text(encoding="utf8")
    i = raw.find("{")
    if i < 0:
        sys.exit("2: catalog 文件里找不到 JSON 对象")
    obj, _ = json.JSONDecoder().raw_decode(raw[i:])
    models = obj.get("models") or []
    if not models:
        sys.exit("2: catalog 的 models 是空的 —— 空列表是假红，作废这一轮")
    out = {}
    for m in models:
        tl = m.get("tokenLimits") or {}
        mi, mo = tl.get("maxInputTokens"), tl.get("maxOutputTokens")
        if not (isinstance(mi, int) and isinstance(mo, int)):
            sys.exit(f"2: catalog 里 {m.get('modelId')} 的 tokenLimits 不是整数对：{tl}")
        out[m["modelId"]] = {"mi": mi, "mo": mo}
    return out


def split_blocks(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """按 `- litellm_params:` 顶格 list item 切块（与 kiro-lane-add.py 同一条切法）。"""
    lines = text.split("\n")
    starts = [n for n, l in enumerate(lines) if l == "- litellm_params:"]
    if not starts:
        sys.exit("2: 找不到任何 '- litellm_params:' 块，输入文件形状不对")
    bounds = list(zip(starts, starts[1:] + [len(lines)]))
    return lines, bounds


def section_span(block: list[str], section: str) -> tuple[int, int]:
    """定位 `litellm_params` / `model_info` 两块的行范围。

    🔴 两块的字段缩进**相同**（都是 4 空格），所以价格字段在整块里恒出现 2 次。
    不分段就会撞上 set_line 的唯一性断言 —— 那个断言是对的，它拦住的正是
    「改了一处以为改了全部」这种病（计费只读 litellm_params）。
    """
    if section == "litellm_params":
        lo = 0  # block[0] == '- litellm_params:'
    else:
        m = [n for n, l in enumerate(block) if l == f"  {section}:"]
        if len(m) != 1:
            sys.exit(f"2: 块里 `{section}:` 命中 {len(m)} 次，预期 1")
        lo = m[0]
    nxt = [n for n, l in enumerate(block)
           if n > lo and re.match(r"^  \w+:", l)]
    return lo, (nxt[0] if nxt else len(block))


def set_line(block: list[str], field: str, old: str, new: str, *,
             where: str, indent: int, span: tuple[int, int] | None = None) -> bool:
    """把 `<indent>field: old` 改成 `: new`。断言旧值等于预期，不符就退出。

    `span` 限定只在某一块（litellm_params / model_info）里找。
    """
    lo, hi = span if span else (0, len(block))
    pat = re.compile(rf"^{' ' * indent}{re.escape(field)}: (\S+)$")
    hit = [n for n in range(lo, hi) if pat.match(block[n])]
    if not hit:
        return False
    if len(hit) != 1:
        sys.exit(f"3: {where} 的 {field} 在同一缩进出现 {len(hit)} 次，形状不对")
    cur = pat.match(block[hit[0]]).group(1)
    if cur != old:
        sys.exit(f"3: {where} 的 {field} 旧值是 {cur}，预期 {old} ⇒ 前提错了，停手")
    block[hit[0]] = f"{' ' * indent}{field}: {new}"
    return True


def insert_after(block: list[str], anchor_field: str, newline: str, *,
                 where: str, indent: int) -> None:
    """在 `model_info` 里某个字段行之后插一行（锚点唯一，否则退出）。"""
    pat = re.compile(rf"^{' ' * indent}{re.escape(anchor_field)}: \S+$")
    hit = [n for n, l in enumerate(block) if pat.match(l)]
    if len(hit) != 1:
        sys.exit(f"3: {where} 的插入锚点 {anchor_field} 命中 {len(hit)} 次，预期 1")
    block.insert(hit[0] + 1, newline)


def structure_gate(before: str, after: str, *, expect_changed: set[str]) -> None:
    """结构门：条数不变；除目标道的目标字段外，每条 deployment 逐字段相等。"""
    try:
        import yaml
    except ImportError:
        print("note: 没有 pyyaml ⇒ 跳过结构门。cutover 前必须让 LiteLLM 自己验配置。")
        return
    a, b = yaml.safe_load(before), yaml.safe_load(after)
    if set(a) != set(b):
        sys.exit(f"4: 顶层键变了：{set(a) ^ set(b)}")
    ml_a, ml_b = a["model_list"], b["model_list"]
    if len(ml_a) != len(ml_b):
        sys.exit(f"4: model_list 条数 {len(ml_a)} -> {len(ml_b)}")
    touched = set()
    for ea, eb in zip(ml_a, ml_b):
        name = ea.get("model_name")
        if name != eb.get("model_name"):
            sys.exit(f"4: 条目顺序变了：{name} vs {eb.get('model_name')}")
        if ea == eb:
            continue
        if name not in expect_changed:
            sys.exit(f"4: 误伤了不该动的道 {name}")
        touched.add(name)
        # 只允许这三类字段变
        allowed = set(OPUS55_PRICE) | {"max_input_tokens", "max_output_tokens"}
        for sec in ("litellm_params", "model_info"):
            da, db = ea.get(sec) or {}, eb.get(sec) or {}
            for k in set(da) | set(db):
                if da.get(k) != db.get(k) and k not in allowed:
                    sys.exit(f"4: {name}.{sec}.{k} 变了（{da.get(k)} -> {db.get(k)}），"
                             "不在允许改的字段里")
    missed = expect_changed - touched
    if missed:
        sys.exit(f"4: 这些道本该改却逐字段相同：{sorted(missed)}")
    print(f"  结构门 ✅ model_list {len(ml_a)} 条不变；"
          f"改动 {len(touched)} 条，其余 {len(ml_a) - len(touched)} 条逐字段相等")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="生产真挂的派生 CM 内容（不是 cm/litellm-config）")
    ap.add_argument("dst", help="输出文件")
    ap.add_argument("--catalog", required=True,
                    help="kiro-probe.py catalog --json 的输出（窗口/输出上限真源）")
    ap.add_argument("--old-cm", required=True,
                    help="第 1 步读到的派生 CM 名，写进 cutover 的 test op 守卫")
    a = ap.parse_args()

    cat = load_catalog(a.catalog)
    text = Path(a.src).read_text(encoding="utf8")
    lines, bounds = split_blocks(text)

    out_lines: list[str] = []
    cursor = 0
    changed: set[str] = set()
    added_mo = 0
    rows: list[tuple] = []

    for lo, hi in bounds:
        out_lines.extend(lines[cursor:lo])
        block = lines[lo:hi]
        cursor = hi
        body = "\n".join(block)
        if not re.search(r"^    id: kiro-rs/", body, re.M):
            out_lines.extend(block)
            continue

        nm = re.search(r"^  model_name: (\S+)", body, re.M)
        um = re.search(r"^    kiro_upstream_model_id: (\S+)$", body, re.M)
        if not (nm and um):
            sys.exit(f"2: 某条 kiro 道缺 model_name 或 kiro_upstream_model_id")
        lane, upstream = nm.group(1), um.group(1)
        k = cat.get(upstream)
        if not k:
            sys.exit(f"3: catalog 里没有上游 {upstream}（道 {lane}）⇒ "
                     "这个号的 entitlement 变了，先查清再改")

        acts: list[str] = []

        # ① 补 max_output_tokens（新增行，锚在 max_input_tokens 之后）
        if re.search(r"^    max_output_tokens: ", body, re.M):
            sys.exit(f"3: {lane} 已经有 max_output_tokens ⇒ 本脚本的前提是全缺，停手")
        insert_after(block, "max_input_tokens", f"    max_output_tokens: {k['mo']}",
                     where=lane, indent=4)
        added_mo += 1
        acts.append(f"+max_output_tokens={k['mo']}")

        # ② gpt 三条道的输入窗口：272000(codex 口径) -> catalog 实际值
        if upstream in GPT_LANES:
            set_line(block, "max_input_tokens", GPT_OLD_MAX_INPUT, str(k["mi"]),
                     where=lane, indent=4)
            acts.append(f"max_input_tokens {GPT_OLD_MAX_INPUT}->{k['mi']}")
        else:
            # 其余道断言本来就与 catalog 一致（不改，但要证明前提成立）
            cur = re.search(r"^    max_input_tokens: (\S+)$", "\n".join(block), re.M)
            if not cur:
                sys.exit(f"3: {lane} 没有 max_input_tokens")
            if int(cur.group(1)) != k["mi"]:
                sys.exit(f"3: {lane} 的 max_input_tokens={cur.group(1)} "
                         f"与 catalog 的 {k['mi']} 不符 ⇒ 本轮范围外的偏差，先报告再改")

        # ③ opus-5.5 的四个价：litellm_params(计费读这块) + model_info 两处都改
        if upstream == "claude-opus-5.5":
            n_touch = 0
            for field, (old, new) in OPUS55_PRICE.items():
                for sec in ("litellm_params", "model_info"):
                    # span 每轮重算：model_info 块的行号会被上面插入的 max_output_tokens 推移
                    if set_line(block, field, old, new, where=f"{lane}/{sec}",
                                indent=4, span=section_span(block, sec)):
                        n_touch += 1
            # 4 个价 × 2 块，但 litellm_params 里只有 in/out 两个（cache 两个只在 model_info）
            if n_touch != 6:
                sys.exit(f"3: {lane} 价格字段命中 {n_touch} 处，预期 6 "
                         "(in/out 各两块 + cache read/write 仅 model_info)")
            acts.append("价 5/25/0.5/6.25 -> 官方 4/20/0.2/5（两块共 6 处）")

        changed.add(lane)
        rows.append((lane, upstream, "; ".join(acts)))
        out_lines.extend(block)

    out_lines.extend(lines[cursor:])
    out = "\n".join(out_lines)

    for lane, upstream, acts in rows:
        print(f"  {lane:24} ({upstream:18}) {acts}")
    print(f"\n  kiro 道 {len(changed)} 条全部处理；新增 max_output_tokens {added_mo} 行")
    if added_mo != 19 or len(changed) != 19:
        sys.exit(f"3: 预期 19 条 kiro 道，实际 changed={len(changed)} added={added_mo}")

    structure_gate(text, out, expect_changed=changed)

    # 只增不删：行数差 == 新增的 max_output_tokens 行数
    d = len(out.split("\n")) - len(lines)
    if d != added_mo:
        sys.exit(f"4: 行数差 {d} != 新增行数 {added_mo} ⇒ 有行被删或多加")
    print(f"  行数门 ✅ +{d} 行，全部是 max_output_tokens")

    Path(a.dst).write_text(out, encoding="utf8")
    h = hashlib.sha256(out.encode()).hexdigest()
    # 🔴 新 CM 名**从 `--old-cm` 的前缀派生**，不写死 `litellm-cwfb-stable-`。
    # 2026-10-04 LiteLLM 升级后生产换了命名体系：
    #   旧 `litellm-cwfb-stable-<sha256(内容)[:12]>`（名字=内容 sha，自校验成立）
    #   新 `litellm-product-proxy-config-<hash>`   （**名字 ≠ 内容 sha**，实测
    #       内容 sha 是 556e2e32d2f9 而实名 29b11b9fce9a）
    # ⇒ 写死旧前缀会造出一个生产体系里不存在的名字；而「名字对上=内容对上」
    #   那条自校验**只对旧体系成立**，新体系下必须用 `wc -c` / sha 另外核。
    import re as _re
    m = _re.match(r"^(.*?)-[0-9a-f]{12}$", a.old_cm)
    prefix = m.group(1) if m else a.old_cm
    new_cm = f"{prefix}-{h[:12]}"
    if not m:
        print(f"  ⚠️ `--old-cm`({a.old_cm}) 不是 `<前缀>-<12位hex>` 形状 ⇒ "
              f"新名直接拼在后面，cutover 前自己看一眼合不合理")
    print(f"\nOK: {a.src} {len(text)}B -> {a.dst} {len(out)}B (+{len(out) - len(text)}B)")
    print(f"    新内容 sha256 = {h}")
    print(f"\n下一步（先看 diff，应当只有 19 条道的目标字段）：")
    print(f"  diff {a.src} {a.dst}")
    print(f"  NEW={new_cm}")
    print(f"  sudo kubectl -n litellm-product create cm $NEW --from-file=config.yaml={a.dst}")
    print("  # 🔴 test op 守卫：卷名不是下面那个就整条 patch 失败，不会盖掉别人刚切的 CM")
    print("  sudo kubectl -n litellm-product patch deploy litellm-proxy --type=json -p "
          f"'[{{\"op\":\"test\",\"path\":\"/spec/template/spec/volumes/0/configMap/name\","
          f"\"value\":\"{a.old_cm}\"}},"
          f"{{\"op\":\"replace\",\"path\":\"/spec/template/spec/volumes/0/configMap/name\","
          f"\"value\":\"{new_cm}\"}}]'")
    print("  sudo kubectl -n litellm-product rollout status deploy/litellm-proxy")
    print(f"\n  🔴 新体系下 CM 名**不等于**内容 sha（10-04 实测）⇒ 别拿名字当内容判据，"
          f"\n     只认副本内 `wc -c /app/config.yaml` == {len(out)}")
    print(f"\n  验收三层：① 每个**在役**副本（⚠️排掉 Terminating）内 "
          f"`wc -c /app/config.yaml` == {len(out)}")
    print("            ② 逐副本 /v1/model/info：19 条道的 max_input/output_tokens 与 "
          "catalog 逐格相等，opus-5-5 四个价 == 官方")
    print("            ③ scripts/kiro-lane-regress.sh --rounds 1（19/19 + in_tok 分层）")
    print(f"  回滚：把卷名 patch 回 {a.old_cm}，旧 CM 不删就一直在")
    return 0


if __name__ == "__main__":
    sys.exit(main())
