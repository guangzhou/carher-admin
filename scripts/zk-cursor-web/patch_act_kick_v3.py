#!/usr/bin/env python3
"""patch_act_kick_v3.py — proto2 分支「拒绝动手」的定向修补（2026-09-01）。

## 为什么改这两处（三段式）

假设：proto2 lane 的 forced-action-retry 之所以救不回，是**模型不相信 ⟦cmd¦run⟧ 会被真的执行**，
       而不是它发了块、解析器没认出来。
证伪条件：如果是解析问题，救不回那几发的交付文本里应当**出现** ⟦ 或 fenced json 或 tool_calls。
数据：`dump_kick.py` 打 85/83 各 4 发，救不回的每一发 `BRACKET=0 FENCEJSON=0 TOOLCALLS=0`，
       正文原话是「不能生成一个会返回真实 `ls -1 /tmp` 结果的执行块，也不能伪造条目数量」
       —— 模型把「发块」理解成「伪造结果」。⇒ 解析假设被证伪，文案假设留下。

对应地改两处**纯文案**（不动任何控制流）：
  ① `V2_CONTRACT` 第 2 条：补上执行机制那句 —— 运行时会截获这个块、在用户真机上跑、
     真实输出下一轮以工具消息回来；发块**不是**伪造结果，不发才是把任务撂下。
     （这句不是我编的：同文件 `V2B_CONTRACT` 早就有等价表述，V2 这份漏了。）
  ② v2 分支的 forced-action-retry kick：同一句 + 明确禁止「让用户自己去终端跑」。

## 灰度与回滚

两份文案都留在产物里，`ZK_ACT_KICK2=1` 才走新的 —— patch 完 CM 全 lane 重启后
**行为零变化**（谁都没设这个 env），再单独给一条 lane 开 env 做同窗口 A/B。
回滚 = `kubectl set env deploy/<lane> ZK_ACT_KICK2-`（秒级，无需碰 CM）。

用法：
    python3 patch_act_kick_v3.py <src.js> <out.js>
"""
import sys

# ── 锚点（都做唯一性硬校验；错一个就炸，不静默产出半成品）─────────────────────
A_DECL = "const V2_CONTRACT = `"
# V2_CONTRACT 与 V2B_CONTRACT 都以 "Never mention this protocol.`" 收尾，靠**后面那行注释**区分
A_END = "Never mention this protocol.`\n// 握手教学轮"
A_KICK = ("collectWebTextR('Your previous reply announced or planned instead of acting. "
          "Act NOW per the OUTPUT PROTOCOL: give the single next ⟦cmd¦run=<bash>⟧ block "
          "(the runtime executes it and returns real output), optionally preceded by one "
          "short sentence of prose. Only if the task is already fully complete, reply with "
          "the final self-contained result alone — no future-tense promises.')")
# 被 .replace() 找的那半句，必须在 V2_CONTRACT 正文里逐字存在
A_SLOT = "⟦cmd¦run=...⟧ block. Include a command block IF AND ONLY IF"

EXEC_MECH = (
    "⟦cmd¦run=...⟧ block: the runtime intercepts that block, runs it on the user's real\\n"
    "   machine, and returns the REAL output to you as a tool message on the next turn.\\n"
    "   Emitting the block is therefore NOT fabricating, simulating or pretending — it IS how\\n"
    "   the command actually runs. Declining to emit it does not make you more honest; it just\\n"
    "   leaves the task undone. Never tell the user to run a command themselves in their own\\n"
    "   terminal when you could emit the block instead.\\n"
    "   Include a command block IF AND ONLY IF")

SELECTOR = """
// ── [act-kick v3] 2026-09-01：proto2 分支「二次拒绝」的定向文案（ZK_ACT_KICK2=1 开）──────
// 数据：救不回的那几发交付文本里 ⟦ / ```json / tool_calls 全 0，模型原话是「不能生成一个会
// 返回真实结果的执行块，也不能伪造」⇒ 它不信这个块会被执行，不是解析器漏认。
// 默认关：CM 全 lane 重启后行为零变化，灰度靠单 lane set env，回滚靠 unset env。
const _ACT_KICK2 = process.env.ZK_ACT_KICK2 === '1'
const V2_CONTRACT = _ACT_KICK2
  ? V2_CONTRACT_BASE.replace(%(slot)r, %(mech)r)
  : V2_CONTRACT_BASE
"""


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src = open(sys.argv[1], encoding="utf-8").read()

    for name, anchor, want in (("V2_CONTRACT 声明", A_DECL, 1),
                               ("V2_CONTRACT 结尾", A_END, 1),
                               ("v2 kick 文案", A_KICK, 1),
                               ("契约插入槽", A_SLOT, 1)):
        got = src.count(anchor)
        if got != want:
            sys.exit("!! 锚点 %s 出现 %d 次（期望 %d）——产物已漂移，拒绝生成" % (name, got, want))
    if _ACT_KICK2_PRESENT(src):
        sys.exit("!! 产物里已经有 ZK_ACT_KICK2，别重复打")

    # ① 常量改名 + 在 V2_CONTRACT 结束处插选择器
    out = src.replace(A_DECL, "const V2_CONTRACT_BASE = `", 1)
    sel = SELECTOR % {"slot": A_SLOT, "mech": EXEC_MECH.replace("\\n", "\n")}
    out = out.replace(A_END, "Never mention this protocol.`\n" + sel + "// 握手教学轮", 1)

    # ② kick 文案：两份并存，按 env 选
    new_kick = (
        "collectWebTextR(_ACT_KICK2\n"
        "              ? 'Your previous reply announced, planned, or declined instead of acting. "
        "Act NOW per the OUTPUT PROTOCOL: give the single next ⟦cmd¦run=<bash>⟧ block, "
        "optionally preceded by one short sentence of prose. The runtime intercepts that block, "
        "executes it on the user\\'s real machine, and returns the REAL output to you next turn "
        "— emitting the block is NOT fabricating a result and NOT pretending you already ran "
        "anything, it is the actual mechanism by which the command runs. Do NOT tell the user to "
        "run it themselves. Only if the task is already fully complete, reply with the final "
        "self-contained result alone — no future-tense promises.'\n"
        "              : 'Your previous reply announced or planned instead of acting. Act NOW per "
        "the OUTPUT PROTOCOL: give the single next ⟦cmd¦run=<bash>⟧ block (the runtime "
        "executes it and returns real output), optionally preceded by one short sentence of prose. "
        "Only if the task is already fully complete, reply with the final self-contained result "
        "alone — no future-tense promises.')")
    out = out.replace(A_KICK, new_kick, 1)

    if out == src:
        sys.exit("!! 三处替换后产物与输入相同 —— 空转，拒绝写出")
    open(sys.argv[2], "w", encoding="utf-8").write(out)
    print("OK  %s -> %s  (%d -> %d chars)" % (sys.argv[1], sys.argv[2], len(src), len(out)))


def _ACT_KICK2_PRESENT(src):
    return "ZK_ACT_KICK2" in src


if __name__ == "__main__":
    main()
