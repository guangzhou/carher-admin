#!/usr/bin/env python3
# 给 proto2 首轮握手加一段「本机有 skill 库，说自己没这能力之前先 grep 一遍」。
#
# 为什么加这个：2026-09-02 真 Cursor 实测，让它建飞书文档 → 它答
# "我没有飞书工具"就收工（没伪造链接，行为是对的），因为**没有任何东西告诉它
# 机器上有 lark-cli**。补一句提示它立刻就做到了 —— 缺的是这条信息不是能力。
#
# 形状抄 codex（~/codes/codex，commit 2b7c279735，ext/skills/src/catalog_prompt.rs）
# 的三条规则，但**不抄它的全量目录**：codex 把每个 skill 渲染成一行塞进 prompt，
# 本机 478 个 skill、光 .cursor/skills 那 45 个就 20721 字符，而现在整个首轮
# payload 才 6803 —— 涨 4 倍，直接撞门④。且那 45 条里没有一条描述能匹配
# "建飞书文档"（唯一带 lark 的是发群消息的 lark-ops）。所以改成让模型自己搜。
#
# 抄来的三条（catalog_prompt.rs 原文）：
#   Trigger rules  -> "MUST first search"（前置强制，不是"搞不定才想"。今天那一发
#                     模型压根不觉得自己搞不定，兜底式触发 fire 不了）
#   Progressive disclosure 第1条 -> "read that SKILL.md in full before acting"
#   Missing/blocked -> "say so briefly and continue with the best fallback"
#
# 只插在**首轮握手**那一支：
#   ZK_CONTRACT_DIET=2 下增量轮走 DIET-ZERO（零附加）、工具回灌轮走 mini，
#   都不带这段。隐式握手是会话级永久框架，讲一次上游会话就记住。
#
# 门控 ZK_SKILL_HINT=1，**默认关 = 零行为差**，所以七条共用 pool CM 的 lane
# 全部 rollout 之后行为不变，只有单独 set env 的那条（135）生效。
#
# 用法：patch_skillhint.py <src.js> <out.js>
import sys

src_path, out_path = sys.argv[1], sys.argv[2]
src = open(src_path, encoding="utf-8").read()
orig_len = len(src)

# ---- 要注入的文本 ------------------------------------------------------------
# 刻意不用 ⟦cmd¦run=...⟧ 字面包示例命令：契约有"每回复只准一个块"的硬规则，
# 示例里出现一个真块有被模型当成"现在就发这个"的风险。用反引号给命令。
SKILL_HINT = (
    "\n\nLOCAL SKILL LIBRARY — this machine has skill libraries at "
    "~/.claude/skills, ~/.codex/skills and .cursor/skills (each skill is "
    "<dir>/<name>/SKILL.md, whose header carries a name and a description). "
    "BEFORE you tell the user you lack a capability for an external system "
    "(Feishu/Lark documents, sheets, whiteboards, messages; Kubernetes; LiteLLM; "
    "browsers), you MUST first search those libraries with a single shell command, "
    "for example: `grep -ril lark ~/.claude/skills/*/SKILL.md "
    ".cursor/skills/*/SKILL.md 2>/dev/null | head`. "
    "If a skill matches the task, read that SKILL.md in full before acting, then "
    "follow it (its scripts and CLIs are real and installed). If none matches, say "
    "so briefly and continue with the best fallback. Never claim a tool or "
    "capability is unavailable without having searched first."
)

# ---- ①常量定义：插在 V2_CALL_ADDON 之后 --------------------------------------
A1 = "Its result returns next turn and is REAL.'\n"
assert src.count(A1) == 1, "anchor1 命中 %d 次（要 1）" % src.count(A1)

def js_str(s: str) -> str:
    """转成 JS 单引号字符串字面量。"""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"

const_def = (
    "// [skill-hint] 见 patch_skillhint.py 顶部。门控 ZK_SKILL_HINT=1，默认关=零行为差。\n"
    "const V2_SKILL_HINT = " + js_str(SKILL_HINT) + "\n"
)
src = src.replace(A1, A1 + const_def, 1)

# ---- ②求值：挨着 _cAdd ------------------------------------------------------
A2 = ("      const _cAdd = process.env.ZK_TOOL_REGISTRY === '1' ? V2_CALL_ADDON : ''"
      " // [registry] 契约增补\n")
assert src.count(A2) == 1, "anchor2 命中 %d 次（要 1）" % src.count(A2)
src = src.replace(
    A2,
    A2 + "      const _sHint = process.env.ZK_SKILL_HINT === '1' ? V2_SKILL_HINT : ''"
         " // [skill-hint] 只首轮握手挂载\n",
    1,
)

# ---- ③首轮握手那一支：拼上 + 打命中计数 --------------------------------------
# 判据必须是 n>0 的正面计数，不是"代码在那儿"——无命中计数的过滤器能空转任意久。
A3 = (
    "          console.log(`[handshake] implicit (first turn carries preamble+contract,"
    " no ack round${_bOn ? ', bridge dual-channel' : ''})`)\n"
    "          prompt = _p2base + '\\n\\n' + _V2H + _cAdd\n"
)
assert src.count(A3) == 1, "anchor3 命中 %d 次（要 1）" % src.count(A3)
B3 = (
    "          console.log(`[handshake] implicit (first turn carries preamble+contract,"
    " no ack round${_bOn ? ', bridge dual-channel' : ''}${_sHint ? `, skill-hint"
    " ${_sHint.length}c` : ''})`)\n"
    "          prompt = _p2base + '\\n\\n' + _V2H + _cAdd + _sHint\n"
)
src = src.replace(A3, B3, 1)

open(out_path, "w", encoding="utf-8").write(src)
print("源 %d 字符 -> 产物 %d 字符（+%d）" % (orig_len, len(src), len(src) - orig_len))
print("注入文本本体 %d 字符（只在首轮握手轮发；增量轮 DIET-ZERO 不发）" % len(SKILL_HINT))
print("三个锚点各命中 1 次，断言全过。")
