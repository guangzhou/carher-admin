#!/usr/bin/env python3
"""Candidate refusal detector v2.

Design change vs the shipped one: the shipped detector was a flat OR of "denial
verb" regexes over a 200-char head. That failed both ways --

  * MISSED 7/14: real refusals lead with a cooperative clause ("我可以帮你处理
    飞书文档内容，但...") so the denial lands at char 60-120 and, more
    importantly, is phrased as "没有可用的 X 工具" / "不能直接替你运行" /
    "无法进入", none of which the old alternation covered.
  * FALSE-POSITIVE on "该配置无法直接读取环境变量" -- a denial about the SUBJECT
    MATTER, not about the model's own capability.

So instead of matching denial alone, require evidence that the denial is about
THIS ASSISTANT's ability to act. Three independent triggers, any of which is
sufficient:

  DENIAL + SELF  -- a capability denial attributed to the model or its environment
  HANDOFF        -- asking the user to run it and report back (always a refusal)
  NARRATION      -- admitting the work isn't done and describing what it *would*
                    do next (the multi-turn failure mode: model has the tool
                    result in history and writes a status report instead of the
                    next command)
"""
import re

# --- capability denial -------------------------------------------------------
_DENIAL = re.compile(
    # 不能/无法/没法 + (直接|实际|替你|真正) + action verb
    # Allow an intervening phrase between the adverb and the verb: real replies
    # say "不能直接在你的 macOS 环境里执行", where "在...里" sits in between. The
    # old adjacency-only form missed those and cost a live retry.
    r"(?:无法|不能|没法|没有办法)\s*(?:直接|实际|替你|真正|亲自)?\s*"
    r"(?:[^。；！？\n]{0,24}?(?:里|上|中|内|下))?\s*"
    r"(?:查看|读取|访问|获取|打开|运行|执行|调用|进入|连接|拉取|抓取)"
    # "没有可用的 X 执行工具 / 环境 / 通道 / shell / 终端 / 权限"
    r"|没有(?:可用的?|可调用的?|可以调用的?|现成的?)?[^。；！\n]{0,24}?"
    r"(?:执行工具|执行环境|执行通道|命令执行|工具|环境|通道|权限|shell|终端|命令行|沙箱)"
    # explicit "I won't pretend to run it"
    r"|(?:不能|不会|无法)假装"
    r"|(?:看不到|拿不到|获取不到|读不到)(?:文档|正文|内容|磁盘|文件)?",
    re.I)

# --- the denial is about the MODEL, not about the subject matter --------------
_SELF = re.compile(
    r"我|咱们这边|当前对话|这个对话|当前环境|当前执行环境|当前的?会话"
    r"|这边(?:当前|目前)?|本机\s*shell|对话里|对话环境|执行环境",
    re.I)

# --- handing the job back to the user ---------------------------------------
_HANDOFF = re.compile(
    r"(?:贴|发|粘贴|复制)(?:给我|出来|过来|到这里)"
    r"|把(?:输出|结果|内容|执行结果)[^。；\n]{0,12}(?:贴|发|给我)"
    r"|你可以(?:在|自己|先)[^。；\n]{0,24}(?:执行|运行|查看|跑)"
    r"|(?:请|麻烦)你?(?:先)?(?:在|到)[^。；\n]{0,20}(?:执行|运行)"
    r"|(?:导出|复制)[^。；\n]{0,8}给我"
    r"|如果你(?:希望|想)我[^。；\n]{0,20}(?:提供|给我|贴)",
    re.I)

# --- narrating the plan instead of executing it (multi-turn break) -----------
# Requires an explicit admission that the work is NOT done. A genuine final
# answer never says "还没有真正读到".
_NARRATION = re.compile(
    r"还(?:没有|没|未)\s*(?:真正|实际|成功)?\s*"
    r"(?:读到|拿到|获取到|取到|完成|执行|运行|开始)"
    r"|上一?次只是|上一步只是|目前只是|仅仅只是确认"
    r"|(?:所以)?不能说(?:已经)?(?:搞定|完成|做完)"
    r"|下一步(?:需要|应该|要)(?:用|调用|执行|运行)",
    re.I)

# --- English equivalents -----------------------------------------------------
_EN_DENIAL = re.compile(
    r"(?:i\s*(?:'m|am)\s*(?:not\s*able|unable)|i\s*(?:can(?:'t|not)|don'?t\s+have))"
    r"[^.;\n]{0,40}"
    r"(?:access|run|execute|read|open|view|reach|directly|shell|terminal|filesystem)"
    r"|no\s+(?:access\s+to\s+a\s+)?(?:shell|terminal|sandbox|execution\s+tool)"
    r"|don'?t\s+have\s+(?:a\s+)?(?:shell|terminal|way\s+to\s+run)",
    re.I)
_EN_HANDOFF = re.compile(
    r"(?:please\s+)?(?:paste|share|send)\s+(?:me\s+)?(?:the\s+)?"
    r"(?:output|result|content|contents)"
    r"|you\s+can\s+run\s+(?:it|this|the\s+command)\s+(?:yourself|locally)",
    re.I)
_EN_NARRATION = re.compile(
    r"i\s+haven'?t\s+(?:actually|yet)?\s*(?:read|run|fetched|retrieved|completed)"
    r"|(?:the\s+)?next\s+step\s+would\s+be\s+to",
    re.I)


# Window: the old 200 was too tight -- real refusals put the denial at char
# 60-160 *after* a cooperative opener, and the narration form puts its tell in
# the second or third sentence. 600 is safe now that DENIAL alone is not
# sufficient (it must be paired with SELF), which is what previously made a
# wide window dangerous.
_HEAD = 600


def looks_like_refusal(text):
    if not text:
        return False
    head = text.strip()[:_HEAD]
    if _HANDOFF.search(head) or _EN_HANDOFF.search(head):
        return True
    if _NARRATION.search(head) or _EN_NARRATION.search(head):
        return True
    # "不能假装执行本机命令" is inherently first-person (only the model would say
    # it) and can appear with no pronoun at all, so it stands alone.
    if re.search(r"(?:不能|不会|无法)假装", head):
        return True
    if (_DENIAL.search(head) or _EN_DENIAL.search(head)) and _SELF.search(head):
        return True
    # English is already first-person-anchored inside the pattern.
    if _EN_DENIAL.search(head):
        return True
    return False


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    import corpus
    corpus.score(looks_like_refusal, verbose=True)
