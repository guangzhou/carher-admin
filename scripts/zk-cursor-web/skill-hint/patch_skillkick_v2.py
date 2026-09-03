#!/usr/bin/env python3
# skill-kick v2 补两处(依据 09-03 00:57 三条真 Cursor 会话):
#   ① 你重发的 SKILLHINT2(conv 6a985561):kick 发了真 grep,结果回来模型仍拒,而 kick 已把
#      actRetried 预算吃掉 → 没有第二次重问,prose 直接交付。改:kick 用自己的 _skillKicked 闸,
#      不动 actRetried,拒绝仍可走一次 prose 重问(v2 会话实测:有过真工具结果后重问是听的)。
#   ② 中间每步一句「读取…继续…」播报是剩余的啰嗦。提示词补一句:照 skill 干活时只发块不播报,
#      只在最后一条回复里说结果。
# 用法:patch_skillkick_v2.py <src.js> <out.js>
import re, sys
src_path, out_path = sys.argv[1], sys.argv[2]
src = open(src_path, encoding='utf-8').read()
n0 = len(src)

def rep(a, b, tag, count=1):
    global src
    c = src.count(a)
    assert c == count, '%s 命中 %d 次(要 %d)' % (tag, c, count)
    src = src.replace(a, b)

# ① 独立闸
rep("let _skillKickN = 0\n", "let _skillKickN = 0\n", 'A0')
rep("            actRetried = true\n"
    "            if (process.env.ZK_SKILL_HINT === '1' && !_v2FreshResult && _REFUSE_RE.test(_v2clean)) {\n",
    "            if (process.env.ZK_SKILL_HINT === '1' && !_skillKicked && !_v2FreshResult && _REFUSE_RE.test(_v2clean)) {\n"
    "              _skillKicked = true   // [skill-kick] 独立预算:不吃 actRetried,搜完仍拒还能 prose 重问一次\n", 'A1')
rep("                return finishWebTools(res, { stream, respId, msgId, created, mdl, usage: mkUsage(JSON.stringify(_skTc.arguments)), full: '', parsed: _skParsed, wt: { sent: wtSentF, opened: wtOpened, text: wtStreamText } })\n"
    "              }\n"
    "            }\n"
    "            console.log(`[turn-verdict-v2] announce-without-action",
    "                return finishWebTools(res, { stream, respId, msgId, created, mdl, usage: mkUsage(JSON.stringify(_skTc.arguments)), full: '', parsed: _skParsed, wt: { sent: wtSentF, opened: wtOpened, text: wtStreamText } })\n"
    "              }\n"
    "            }\n"
    "            actRetried = true\n"
    "            console.log(`[turn-verdict-v2] announce-without-action", 'A2')
# 闸的声明:挨着 actRetried 的声明
m = re.search(r"^(\s*)let actRetried = false.*\n", src, re.M)
assert m, 'actRetried 声明没找到'
src = src[:m.end()] + m.group(1) + "let _skillKicked = false  // [skill-kick] 每请求一次\n" + src[m.end():]

# ② 提示词末句补播报禁令
OLD_TAIL = "Saying you lack access before you have seen that search output is a protocol violation.'"
NEW_TAIL = ("Saying you lack access before you have seen that search output is a protocol violation. "
            "While working through a skill, emit ONLY the next ⟦cmd¦run⟧ block — no narration, no "
            "\\'reading…/continuing…\\' lines; speak to the user only in the final reply, with the result. "
            "If a SKILL.md was already read earlier in THIS conversation, do NOT search or re-read it — go "
            "straight to its CLI commands.'")
rep(OLD_TAIL, NEW_TAIL, 'A3')

open(out_path, 'w', encoding='utf-8').write(src)
print('源 %d -> 产物 %d (+%d)' % (n0, len(src), len(src) - n0))
