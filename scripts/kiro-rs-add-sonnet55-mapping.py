#!/usr/bin/env python3
r"""给 kiro.rs 源码加 `claude-sonnet-5.5` 映射 —— 在 188 构建机本地跑，不碰生产。

为什么需要改源码（10-02 实测）
------------------------------
上游 10-02 新出 `claude-sonnet-5.5`（1.3x，1M/128K），而 kiro.rs 的
`map_model()` 是**子串匹配**且 **sonnet 这一支没有 5.5 分支**：

    if model_lower.contains("sonnet") {
        if model_lower.contains("sonnet-5") {          // ← 对 "sonnet-5-5" 同样为真
            Some("claude-sonnet-5".to_string())

⇒ 直打 kiro.rs 发 `claude-sonnet-5-5` **返回 200**，但实际落的是 `claude-sonnet-5`。
10-02 实测那一发：`code=200`、响应 `model` 原样回显 `claude-sonnet-5-5`、
`in_tok=6475` 与 sonnet-5 同族 —— **外观与成功完全同形**。
opus 那一支 09-29 加 5.5 时已经写了防劫持注释，sonnet 这支没人加过。

🔴 判「发 X 真的落 X」只有一把真尺子
-----------------------------------
HTTP 200 / 响应 `model` / `in_tok` 差异 / 输出上限 / 倍率 **全都不成立**（见 SKILL §G 那张表）。
唯一真尺子 = `RUST_LOG=debug` 读 `Kiro request body` 里 map **之后**的
`conversationState.currentMessage.userInputMessage.modelId`。
⇒ 本脚本只改源码；**改完必须按 SKILL §G「落点尺子」验，且单测必带老名阳性对照**
（只测新名的绿证明不了没劫走 `claude-sonnet-5`）。

幂等 / 回滚
-----------
已经改过就 exit 2 不重复改。改前自动备份到 `/tmp/converter.rs.bak-<ts>` 并打印回滚命令。
本脚本**只改 188 工作区的源码**，不构建、不推镜像、不碰 242 —— 那几步要停机，由人决定。

用法
----
    scp scripts/kiro-rs-add-sonnet55-mapping.py cltx@10.68.13.188:/tmp/
    ssh cltx@10.68.13.188 'cd /Data/kiro-build/kiro.rs && python3 /tmp/kiro-rs-add-sonnet55-mapping.py'
    ssh cltx@10.68.13.188 'cd /Data/kiro-build/kiro.rs && python3 /tmp/kiro-rs-add-sonnet55-mapping.py --check'

退出码：0 已改 / 2 已经改过（幂等）/ 3 锚点不唯一（源码变了，停手）
"""
from __future__ import annotations

import datetime
import pathlib
import shutil
import sys

CONV = pathlib.Path("src/anthropic/converter.rs")
HANDLERS = pathlib.Path("src/anthropic/handlers.rs")

MAP_OLD = '''    if model_lower.contains("sonnet") {
        if model_lower.contains("sonnet-5") {
            Some("claude-sonnet-5".to_string())'''

MAP_NEW = '''    if model_lower.contains("sonnet") {
        // ⚠️ 顺序敏感：`sonnet-5-5` / `sonnet-5.5` 必须排在 `sonnet-5` 之前。
        // `contains("sonnet-5")` 对 "claude-sonnet-5-5" 同样为真，放后面会被静默
        // 劫持成 claude-sonnet-5（HTTP 200 + 原样回显请求名 + in_tok 同族，
        // 外观与成功完全同形）。2026-10-02 实测过这个劫持：发 sonnet-5-5 得 200、
        // in_tok 6475 与 sonnet-5 同族。判落点只认 RUST_LOG=debug 的上游 modelId。
        if model_lower.contains("sonnet-5-5") || model_lower.contains("sonnet-5.5") {
            Some("claude-sonnet-5.5".to_string())
        } else if model_lower.contains("sonnet-5") {
            Some("claude-sonnet-5".to_string())'''

WIN_OLD = 'Some(mapped) if mapped == "claude-sonnet-5" ||'
WIN_NEW = ('Some(mapped) if mapped == "claude-sonnet-5" '
           '|| mapped == "claude-sonnet-5.5" ||')

MARK = 'claude-sonnet-5.5'


def patch(path: pathlib.Path, pairs: list[tuple[str, str]], label: str) -> bool:
    src = path.read_text(encoding="utf8")
    for old, _ in pairs:
        n = src.count(old)
        if n != 1:
            sys.exit(f"3: {path} 的 {label} 锚点命中 {n} 次（预期 1）⇒ 源码变了，停手")
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = pathlib.Path(f"/tmp/{path.name}.bak-{ts}")
    shutil.copy2(path, bak)
    for old, new in pairs:
        src = src.replace(old, new)
    path.write_text(src, encoding="utf8")
    print(f"  ✅ {path} 已改；备份 {bak}")
    print(f"     回滚：cp {bak} {path}")
    return True


# `/v1/models` 两个条目，照 09-29 加 opus-5-5 的同一形状（含 -thinking 变体）。
# created 用上游 catalog 首次出现的日子（10-02）。
MODELS_OLD = '''        Model {
            id: "claude-opus-5-5".to_string(),'''

MODELS_NEW = '''        Model {
            id: "claude-sonnet-5-5".to_string(),
            object: "model".to_string(),
            created: 1790985600, // Oct 2, 2026 —— 上游 catalog 首次出现
            owned_by: "anthropic".to_string(),
            display_name: "Claude Sonnet 5.5".to_string(),
            model_type: "chat".to_string(),
            max_tokens: 128_000,
        },
        Model {
            id: "claude-sonnet-5-5-thinking".to_string(),
            object: "model".to_string(),
            created: 1790985600, // Oct 2, 2026
            owned_by: "anthropic".to_string(),
            display_name: "Claude Sonnet 5.5 (Thinking)".to_string(),
            model_type: "chat".to_string(),
            max_tokens: 128_000,
        },
        Model {
            id: "claude-opus-5-5".to_string(),'''


def main() -> int:
    if not CONV.is_file() or not HANDLERS.is_file():
        sys.exit(f"3: 请在 /Data/kiro-build/kiro.rs 目录下运行（找不到 {CONV}）")

    conv_src = CONV.read_text(encoding="utf8")
    hand_src = HANDLERS.read_text(encoding="utf8")
    done_conv = MARK in conv_src
    done_hand = "claude-sonnet-5-5" in hand_src

    if "--check" in sys.argv:
        print(f"converter.rs  有 sonnet-5.5 映射: {done_conv}")
        print(f"handlers.rs   有 sonnet-5-5 条目: {done_hand}")
        # 顺序检查：5.5 分支必须出现在 sonnet-5 分支之前
        if done_conv:
            i55 = conv_src.index('contains("sonnet-5-5")')
            i5 = conv_src.index('} else if model_lower.contains("sonnet-5") {')
            print(f"顺序正确(5.5 在 5 之前): {i55 < i5}")
        return 0 if (done_conv and done_hand) else 1

    if done_conv and done_hand:
        print("2: 已经改过了（幂等，不重复改）")
        return 2

    print("改 kiro.rs 源码加 claude-sonnet-5.5：")
    if not done_conv:
        patch(CONV, [(MAP_OLD, MAP_NEW), (WIN_OLD, WIN_NEW)], "map_model/窗口表")
    else:
        print(f"  ⏭ {CONV} 已有映射")
    if not done_hand:
        patch(HANDLERS, [(MODELS_OLD, MODELS_NEW)], "/v1/models")
    else:
        print(f"  ⏭ {HANDLERS} 已有条目")

    print()
    print("🔴 下一步（都要停机，由人决定，本脚本不做）：")
    print("  1. 单测（必带老名阳性对照）：cargo test map_model")
    print("  2. 构建 + 回归 + save/scp/import + set image —— 见 SKILL §C 流水线")
    print("  3. 🔴 deploy 是 strategy:Recreate 单副本 ⇒ 切镜像会短暂全断")
    print("  4. 落点验收：SKILL §G「落点尺子」RUST_LOG=debug 读上游 modelId，")
    print("     并断言发 claude-sonnet-5 仍落 claude-sonnet-5（没被新分支劫走）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
