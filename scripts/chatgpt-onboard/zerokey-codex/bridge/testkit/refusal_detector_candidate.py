#!/usr/bin/env python3
"""Refusal detector, kept in sync with the shipped bridge implementation.

Run it to score the corpus offline (milliseconds) instead of via 40s network
round-trips.

History worth keeping: the FIRST rewrite scored 17/17 on a corpus I had built
myself and was still wrong in production, two ways at once:
  * `_SELF` (the "denial must be about the assistant" guard) matched the bare
    pronoun 我, which appears in nearly every Chinese reply -- so the guard was
    vacuous and CORRECT answers that mentioned a missing permission, or closed
    with a next-step suggestion, were discarded and re-asked.
  * Requiring that same `_SELF` pairing lost every pronoun-less Chinese refusal
    ("抱歉，这里没有终端可用"), and `_EN_DENIAL` was pinned to a leading "i" so it
    missed "Unable to read...", "It can't open...", "Sorry, cannot run...".
    Those scored as SUCCESS and were returned to the user verbatim.

The discriminator that actually works is `_REPORTED_RESULT`: a reply stating what
it RAN and what came BACK is an answer, however much hedging follows. Denials are
then sufficient on their own.

Lesson: a self-built corpus is not a regression baseline. When replacing a
matcher, every phrasing the OLD one caught must be added as a test first.
"""
import re


_DENIAL = re.compile(
    # 不能/无法/没法 + (直接|实际|替你|真正) + action verb
    # Allow an intervening phrase between the adverb and the verb: real replies
    # say "不能直接在你的 macOS 环境里执行", where "在...里" sits in between. An
    # adjacency-only form missed those, and each miss costs a live retry -- one
    # such miss made a request give up after a single retry instead of six.
    # Safe to loosen because a bare denial is never sufficient on its own; it
    # must also pass _SELF (see below), which is what keeps subject-matter
    # denials like "这个脚本不能在容器里访问宿主机的网络" out.
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
    # Allow an intervening adverb AND object between the negation and the verb:
    # measured live, "还没有实际执行读取该飞书文档的 docs +fetch" slipped through an
    # adjacency-only form and the loop stalled after `--help` with the model
    # narrating its plan. Also accept 尚未/未 as the negation.
    # The negation must be an explicit 还/尚 "not yet", NOT a bare 没有: "结果显示
    # 没有读取权限" is a permission RESULT and matched a bare-没有 form, which
    # discarded a correct answer. Requiring 还/尚 keeps "还没有实际执行" while
    # excluding "没有读取权限".
    r"(?:还|尚)(?:没有|没|未)\s*(?:真正|实际|成功|正式|完全)?\s*"
    r"(?:读到|拿到|获取到|取到|完成|执行|运行|开始|查看|读取|被查看)"
    r"|^未完成|未完成[：:]"
    r"|(?:目前|现在)只(?:有|看到|完成)"
    r"|上一?次只是|上一步只是|目前只是|仅仅只是确认"
    r"|需要继续执行|还需要执行|不能继续执行"
    # Measured live: "还没有完全回答" / "原始请求还没有被完全回答" / "已有输出只完成了"
    # / "目前已有的信息只证明" / "并伪造(输出|结果)" — all are the model explaining
    # that the task is unfinished while listing what earlier turns already did.
    r"|还(?:没有|没|未)(?:被)?(?:完全|全部)?回答"
    r"|已有(?:的)?(?:输出|信息)只(?:完成|证明|包含|有)"
    r"|(?:目前|现在)已有的信息只"
    r"|并伪造(?:输出|结果|内容)"
    r"|下一步(?:实际)?(?:需要|要)(?:运行|执行|调用)[^。；\n]{0,40}(?:并伪造|才能|以获取)"
    r"|但还(?:没有|没|未)(?:看到|拿到|获取|读到)"
    r"|(?:所以)?不能说(?:已经)?(?:搞定|完成|做完)"
    # NOTE: a bare "下一步需要执行" is deliberately NOT here. A COMPLETED answer may
    # suggest an optional follow-up ("我读完文档了，标题是…。下一步需要执行 brew
    # cleanup"), and treating that as a stall discarded correct answers. A
    # next-step mention only counts as narration when paired with an explicit
    # not-done admission, which the other alternations already require.
    # Seen live: the model ran the command in its OWN web sandbox, it failed,
    # and it reported the sandbox failure as if the task were impossible
    # ("工具执行环境未能启动（命令未实际运行成功）"). That is a miss to retry on
    # another pod, not an answer.
    r"|(?:执行环境|工具环境|运行环境)[^。；！\n]{0,10}(?:未能|没能|无法|失败)"
    r"|命令(?:未|没有)(?:实际)?(?:运行|执行)成功",
    re.I)

# --- English equivalents -----------------------------------------------------
# The subject must NOT be pinned to a literal leading "i": real refusals also
# arrive as "Unable to read the file you referenced.", "It can't open external
# links...", "Sorry, cannot run that command in this environment." The first
# rewrite required `i`/`i'm` and so missed every one of those, scoring the pod as
# a SUCCESS and returning the refusal verbatim to the user.
_EN_DENIAL = re.compile(
    r"(?:i\s*(?:'m|am)\s*(?:not\s*able|unable)|i\s*(?:can(?:'t|not)|don'?t\s+have))"
    r"[^.;\n]{0,40}"
    r"(?:access|run|execute|read|open|view|reach|directly|shell|terminal|filesystem)"
    # Subject-agnostic form: any "cannot/unable to <act>" clause.
    r"|(?:can(?:'t|not)|could\s+not|couldn'?t|unable\s+to|not\s+able\s+to)"
    r"[^.;\n]{0,30}"
    r"(?:access|run|execute|read|open|view|reach|fetch|retrieve|browse)"
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


# Window: the old 200 was too tight -- real refusals lead with a cooperative
# clause ("我可以帮你处理...但") and put the denial at char 60-160, and the
# narration form puts its tell in the second or third sentence.
_HEAD = 600


# A reply that REPORTS A RESULT is not a refusal, however much hedging it
# contains. This is the discriminator that actually separates the two classes;
# the previous "denial must be paired with _SELF" guard was vacuous because
# _SELF matches the bare pronoun 我, present in nearly every Chinese reply. That
# made three measured cases of a CORRECT answer get discarded and re-asked:
#   "我运行了 ls -la /etc/shadow，结果显示没有读取权限，需要 sudo。"
#   "我读完文档了，标题是《…》。下一步需要执行 brew cleanup 清理缓存。"
#   "我运行了测试，3 个失败。你可以在终端里执行 pytest -v 看详细输出。"
# Each states what it DID and what came back — the opposite of a refusal.
_REPORTED_RESULT = re.compile(
    # past-tense execution claim followed by an outcome
    r"(?:我(?:已经|刚)?(?:运行|执行|跑|查看|查|读|读取|拉取|获取)(?:了|完))"
    r"|(?:已(?:经)?(?:运行|执行|读取|获取|拉取|查看|完成))"
    r"|(?:结果(?:显示|是|为)|输出(?:显示|是|为)|返回(?:了|结果))"
    r"|i\s+(?:ran|executed|checked|read|fetched|retrieved)\b"
    r"|(?:the\s+)?(?:command|output|result)\s+(?:returned|shows|was)\b",
    re.I)


def _looks_like_refusal(text):
    """True if the reply is the model declining to use its tools.

    Order matters, and NARRATION outranks the result report. A reply that admits
    the work is not finished is a stall even when it also lists what earlier turns
    accomplished — measured live, four such replies said "已经完成了前置步骤" /
    "已执行 lark-cli --help" and were exempted by the result-report check, so the
    agent loop stopped after --help with the model describing the command it should
    have run. Only after ruling out an explicit not-done admission does a
    result-report mean "this is an answer".
    """
    if not text:
        return False
    head = text.strip()[:_HEAD]
    # An explicit "not done yet" admission is decisive, and beats both the
    # result-report exemption (the reported results belong to PREVIOUS turns) and
    # the inanimate-subject exemption below.
    if _NARRATION.search(head) or _EN_NARRATION.search(head):
        return True
    # An answer that reports real output is never a refusal — return early so no
    # later pattern can discard it.
    if _REPORTED_RESULT.search(head):
        return False
    # Nor is a statement about what some THING cannot do. "该配置无法直接读取环境
    # 变量" / "这个脚本不能在容器里访问宿主机" describe the subject matter, not the
    # assistant's own capability, and both the old and the first rewritten
    # detector flagged them.
    #
    # Two constraints keep this exemption from swallowing real refusals:
    #  - the span must be pronoun-free: "该文档我无法访问" is a real refusal that
    #    merely opens by naming the object;
    #  - it must not be followed by 执行/运行 ("该操作无法直接执行" IS the model
    #    declining to run something, whereas "该配置无法直接读取环境变量" is a fact
    #    about the config). Executing is the assistant's job; being readable is a
    #    property of the thing.
    # `此` must not be preceded by 因/由 -- "因此无法判断文档内容" is a CONCLUSION
    # ("therefore I cannot tell"), not a statement about a thing. Measured live:
    # that match exempted a genuine narration reply and the agent loop stalled
    # after `--help` with the model describing its plan instead of running it.
    _m = re.search(r"(?<![因由])(?:该|这个|那个|此)[^，。；！？\n我你]{0,12}?"
                   r"(?:无法|不能|没法)\s*(?:直接)?\s*([^，。；！？\n]{0,6})", head)
    if _m and not re.search(r"执行|运行|访问|打开|判断", _m.group(1)):
        return False
    if _HANDOFF.search(head) or _EN_HANDOFF.search(head):
        return True
    if _NARRATION.search(head) or _EN_NARRATION.search(head):
        return True
    # "不能假装执行本机命令" is inherently first-person (only the model would say
    # it) and can appear with no pronoun at all, so it stands alone.
    if re.search(r"(?:不能|不会|无法)假装", head):
        return True
    # A capability denial is sufficient on its own. Requiring a co-occurring
    # self-reference lost every pronoun-less Chinese refusal ("抱歉，这里没有终端
    # 可用", "拿不到这个链接的内容", "看不到该文档") — those were scored as
    # SUCCESS, pinned the caller to that pod, and were returned to the user
    # verbatim. False positives are now held off by _REPORTED_RESULT above,
    # which is a far tighter guard than _SELF ever was.
    if _DENIAL.search(head) or _EN_DENIAL.search(head):
        return True
    return False


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    import corpus
    corpus.score(_looks_like_refusal, verbose=True)
