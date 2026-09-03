#!/usr/bin/env python3
# skill-hint v2 —— 只换 V2_SKILL_HINT 常量的文本，别的一个字节不动。
#
# v1 (807c) 为什么没用（09-03 00:44 真 Cursor 实测，会话 6a985272）：
#   提示确实发到了上游（user 消息 len=11086、结尾就是提示末句），模型 2 秒回
#   「我目前无法直接访问你本机的 Feishu/Lark CLI 环境」—— 它把 "Lark CLI" 都复述了，
#   说明读到了，但它的信念是「命令块跑不到用户机器 / lark-cli 没登录」。v1 只说
#   "去搜"，没打掉这条信念，也没把第一步定死。
#
# v2 改两点（都在同一段文本里）：
#   ① 明说 ⟦cmd¦run⟧ 在**用户自己的电脑**上执行，skill 库与已登录的 CLI 都在那台机器上；
#   ② 飞书类请求第一条回复**必须就是**那个搜索命令块（给出完整块，契约允许每回复一个块，
#      这正好是那一个），看到输出前不许说没权限。
#
# 用法：patch_skillhint_v2.py <src.js> <out.js>
import re, sys

src_path, out_path = sys.argv[1], sys.argv[2]
src = open(src_path, encoding="utf-8").read()

HINT_V2 = (
    "\n\nLOCAL SKILL LIBRARY (read this before answering anything about Feishu/Lark, "
    "Kubernetes, LiteLLM or browsers). Every ⟦cmd¦run=…⟧ block you emit is executed on "
    "the USER'S OWN COMPUTER, and that computer has skill libraries at ~/.claude/skills, "
    "~/.codex/skills and .cursor/skills (each skill = <dir>/<name>/SKILL.md with a name and "
    "description in its header). Their CLIs — including `lark-cli` for Feishu/Lark "
    "documents, sheets, whiteboards and messages — are installed there and ALREADY "
    "AUTHENTICATED as the user. You do NOT need Feishu access of your own: the command runs "
    "as the user. Therefore, when the request touches Feishu/Lark or any external system, "
    "your FIRST reply MUST be exactly this one block and nothing else:\n"
    "⟦cmd¦run=grep -ril lark ~/.claude/skills/*/SKILL.md ~/.codex/skills/*/SKILL.md "
    ".cursor/skills/*/SKILL.md 2>/dev/null | head -20⟧\n"
    "(swap `lark` for the relevant keyword). Next turn, `cat` the best-matching SKILL.md in "
    "full, then follow it step by step with further ⟦cmd¦run⟧ blocks until the task is "
    "done and you can hand the user the real result (e.g. the document URL the CLI printed). "
    "If the search finds nothing, say so briefly and continue with the best fallback. "
    "Saying you lack access before you have seen that search output is a protocol violation."
)

def js_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"

pat = re.compile(r"^const V2_SKILL_HINT = '.*'\n", re.M)
hits = pat.findall(src)
assert len(hits) == 1, "V2_SKILL_HINT 常量命中 %d 次（要 1）" % len(hits)
old_len = len(hits[0])
src = pat.sub(lambda m: "const V2_SKILL_HINT = " + js_str(HINT_V2) + "\n", src, count=1)
open(out_path, "w", encoding="utf-8").write(src)
print("常量行 %d -> %d 字符；提示本体 v2 = %d 字符（v1 807）" % (old_len, len("const V2_SKILL_HINT = " + js_str(HINT_V2) + "\n"), len(HINT_V2)))
