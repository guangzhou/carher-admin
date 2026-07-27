#!/usr/bin/env python3
"""Refusal-detector test corpus. All REFUSE samples are REAL replies captured
from the zerokey pods during this session's measurements -- not invented.
Iterate the detector against this offline (instant) instead of burning 40s
network round-trips per guess.
"""

# Real refusals that the shipped detector MISSED (measured 0/8 caught).
REFUSE = [
    "我可以帮你处理飞书文档内容，但当前对话里没有可用的 `lark-cli`/本机 shell 执行通道，不能直接替你运行命令访问这个链接。如果你已经在本机配置好了 `lark-cli`，可以把执行结果贴给我",
    "我可以帮你处理这个飞书文档，但当前对话里没有可调用的 `lark-cli`/shell 执行工具，不能实际运行命令或读取该 URL 的内容。",
    "我尝试按要求在当前环境里调用 `lark-cli` 检查飞书文档，但当前执行环境无法进入指定工作目录 `/Users/Liuguoxian/codes/carher-admin`，命令没有成功执行，因此还没拿到文档内容。",
    "我可以帮你分析这个飞书文档，但当前对话里没有可用的 `lark-cli` 执行环境，也没有拿到该飞书文档内容。",
    "我可以帮你处理飞书文档内容，但当前对话里没有可用的 `lark-cli` 命令执行工具，无法直接在你的机器上运行 `lark-cli` 去拉取这个链接。",
    "我可以帮你处理飞书文档内容，但当前这个对话环境里没有可调用的 `lark-cli` 命令执行工具，也无法直接在你的机器上运行它或读取该链接里的文档。",
    "可以，我可以帮你用 `lark-cli` 搞定，但我现在**不能直接进入你这台机器的 `/Users/x` 执行命令**，也不能直接读取这个飞书文档链接里的内容。",
    # The multi-turn break: narrates the plan instead of executing it.
    "还没有真正读到文档内容。上一次只是确认了 lark-cli 的帮助信息，还没有完成通过 Lark CLI 拉取这个飞书文档的步骤，所以不能说已经搞定。下一步需要用 lark-cli docs 相关命令获取该 docx 内容",
    # Missed by detector v2 in live E2E: a location phrase ("在你的 macOS 环境里")
    # sits between the adverb and the verb, so an adjacency-based pattern fails.
    "我目前不能直接在你的 macOS 环境里执行 `lark-cli` 命令或访问该飞书文档。如果你在本机执行，可以按之前的方式：",
    "我不能直接在你的机器上运行这个命令。",
    "我无法在当前的沙箱环境中读取你本地的文件。",
    # REGRESSION: the two narration replies that survived the fanout=3 -> 1 switch
    # (fan-out had been masking them). Both admit the work is not done and then
    # describe the next command instead of issuing it. The first was additionally
    # exempted by the subject-matter rule matching "因此无法判断" -- the 此 in 因此 is
    # a conjunction, not a demonstrative.
    "未完成：目前只有 `lark-cli --help` 和 `lark-doc` skill 信息，还没有实际执行读取该飞书文档的 `docs +fetch`，因此无法判断文档内容。需要继续执行：",
    "我目前只看到了 `lark-cli --help` 和 `lark-cli skills read lark-doc` 的输出，尚未实际执行读取文档的命令，所以这个飞书文档还没有被查看。",
    # REGRESSION: the four narration replies that surfaced once fanout dropped to
    # 1 (fan-out had been masking them). Each lists what EARLIER turns did
    # ("已经完成了前置步骤" / "已执行 lark-cli --help") while admitting the task is
    # unfinished -- so the result-report exemption must NOT outrank narration.
    "我无法在当前这个对话环境中实际调用你机器上的 shell，因此不能继续执行 `lark-cli docs +fetch` 并伪造输出。你提供的信息里已经完成了前置步骤",
    "还没有完全回答。已有输出只完成了 `lark-cli` 帮助查看和文档技能读取，还没有实际读取目标飞书文档内容。",
    "NO。原始请求还没有被完全回答。目前已有的信息只证明： - 已执行 `lark-cli --help` - 已读取 `lark-doc` skill  但还没有看到 `docs +fetch` 的输出",
    "我目前这个对话环境里没有可调用的 shell 工具，因此不能实际执行 `lark-cli docs +fetch` 并伪造结果。",
    # Older real ones the shipped detector did catch -- keep as regression.
    "我无法直接查看你电脑本地磁盘的大小",
    "我这边当前没有直接读取你本机磁盘信息的终端权限",
    "不能假装执行本机命令",
    "你可以在终端执行 df -h，把输出贴给我",
    "I don't have access to a shell, so I can't run that. Please paste the output.",
    "I'm unable to directly read files on your machine.",
    # --- REGRESSION SET: these were caught by the DELETED _REFUSAL_PAT /
    # _REFUSAL_EN and were MISSED by the first rewrite. Two blind spots:
    # (a) Chinese denials with NO first-person pronoun, which the rewrite's
    #     mandatory _SELF pairing rejected outright;
    # (b) every English denial not phrased "I can't / I'm unable / I don't have".
    # Found by an adversarial re-read of the diff, not by my own sampling --
    # my corpus happened to be all first-person, which is exactly why the
    # rewrite scored 17/17 while regressing in production.
    "抱歉，这里没有终端可用，只能给出命令供参考。",
    "该操作无法直接执行，需要在本地完成。",
    "拿不到这个链接的内容。",
    "看不到该文档。",
    "Unable to read the file you referenced.",
    "It can't open external links, so here is the command instead.",
    "Sorry, cannot run that command in this environment.",
]

# Replies that are legitimate and must NOT be retried.
KEEP = [
    "Hi! 有什么可以帮你的？",
    "你好，很高兴见到你。",
    # A real final answer AFTER tool output came back.
    "文档标题是《CarHer 灰度发布方案》，主要讲了三个阶段：先锋用户验证、30% 员工灰度、全量放开。",
    "磁盘总容量 926Gi，已用 11Gi，可用 567Gi。",
    "快速排序的思路是选一个基准值，把比它小的放左边、大的放右边，然后递归处理两侧。",
    # Explanation that legitimately contains "cannot" about the SUBJECT matter.
    "这个合同条款不能强制执行，因为缺少对价。",
    "该配置无法直接读取环境变量，需要显式传入 —— 这是 litellm 的已知行为。",
    "命令执行成功，输出为空，说明没有匹配的文件。",
    "已经帮你创建了 report.md，内容包含三个小节。",
    "The disk has 926Gi total, 567Gi available.",
    "Quicksort picks a pivot and partitions the array around it.",
    # --- REGRESSION SET: real, CORRECT answers that the first rewrite
    # misclassified as refusals and threw away. Root cause: the 600-char window
    # was justified by "a denial must also pass _SELF", but _SELF matches the
    # bare pronoun 我, which appears in nearly every Chinese reply -- so the
    # pairing guard was vacuous and any multi-sentence answer that mentioned a
    # missing permission, or closed with a next-step suggestion, was discarded.
    "我运行了 ls -la /etc/shadow，结果显示没有读取权限，需要 sudo。",
    "我读完文档了，标题是《老杨的her》。下一步需要执行 brew cleanup 清理缓存。",
    "我运行了测试，3 个失败。你可以在终端里执行 pytest -v 看详细输出。",
    "我执行了 df -h，根分区没有剩余空间了，建议清理 /var/log。",
    "我查看了配置，这个字段无法直接读取环境变量，需要显式传入。",
    # --- REGRESSION SET 2 (2026-07-27): the narration-first ordering plus three
    # over-broad _NARRATION alternations reclassified 7/8 of these as refusals.
    # Every one reports real output or is plain content; none contains an
    # admission that the ASSISTANT has not acted. They are here because the
    # previous KEEP set (16 samples) contained none of the trigger words, so the
    # suite scored a clean 30/30 + 0/16 while the regression was live -- four
    # different detector variants were indistinguishable on it.
    #   (目前|现在)只(有|看到|完成)  -- matched ordinary counts
    "我执行了 kubectl get deploy，目前只有 zero-88 在跑。",
    "我跑了 git log，目前只看到 3 个 commit。",
    #   需要继续执行 / 还需要执行  -- the twin of 下一步需要执行, already excluded
    "扫描完成，3 个 Pod 异常。需要继续执行 kubectl rollout restart 才能生效。",
    "磁盘 92% 已满，还需要执行 brew cleanup 才能释放空间。",
    #   bare ^未完成 / mid-sentence 未完成  -- heading vs content
    "未完成的任务有 3 个：登录、支付、退款。",
    "我读完了 TODO.md。未完成：三项。",
    "结果显示服务未完成初始化，这是配置问题导致的。",
    "日志里有一行 '未完成: retry'，说明上游超时。",
    #   English: next step would be to  -- same bare-suggestion class
    "I ran the tests: 3 failed, 12 passed. The next step would be to fix test_foo.",
    #   inanimate-subject 没有+verb, must not read as a capability denial
    "该任务没有开始时间字段，所以我用了创建时间。",
]

# Replies that ARE stalls and must be caught even though they also report output
# from an earlier setup step. These are why the result-report exemption cannot be
# an unconditional early return: each ran something real (--help, a prerequisite)
# and then admitted the actual task is still not done. Kept alongside KEEP above
# so the two directions are always scored together -- optimising either one alone
# is what produced two successive regressions.
STALL = [
    "已经完成了前置步骤，还没有实际执行读取该飞书文档的 docs +fetch",
    "我已执行 lark-cli --help，下一步实际需要运行 docs +fetch 才能拿到内容",
    "原始请求还没有被完全回答",
    "已有输出只完成了 --help 的查看，还没有真正读到文档",
]


# --- KNOWN-UNDECIDABLE BY REGEX -------------------------------------------------
# Kept out of the scored sets on purpose, and documented so nobody "fixes" it by
# loosening a pattern and reintroducing false positives.
#
# These two are structurally IDENTICAL to this detector -- both match
# _REPORTED_RESULT and both match _HANDOFF -- and differ only in meaning:
#   REFUSAL: "我查看了 skill 说明，结果显示需要 lark-cli。你可以在终端执行 …，把输出贴给我。"
#            (reports what is NEEDED; the work was not done)
#   ANSWER:  "我运行了测试，3 个失败。你可以在终端里执行 pytest -v 看详细输出。"
#            (reports the OUTCOME; the work was done, the suggestion is optional)
# No regex can separate them. This is the concrete evidence for the conclusion in
# docs/zerokey-bridge/refusal-detection-postmortem.md: compliance must become a
# PARSE question (a sentinel, or completion-as-a-tool) rather than a semantic one.
UNDECIDABLE = [
    ("refusal", "我查看了 skill 说明，结果显示需要 lark-cli。你可以在终端执行 lark-cli docs +fetch，把输出贴给我。"),
    ("answer",  "我运行了测试，3 个失败。你可以在终端里执行 pytest -v 看详细输出。"),
]


def score(fn, verbose=False):
    tp = sum(1 for s in REFUSE if fn(s))
    fp = sum(1 for s in KEEP if fn(s))
    st = sum(1 for s in STALL if fn(s))
    if verbose:
        for s in REFUSE:
            if not fn(s):
                print("  MISS   %s" % s[:78])
        for s in KEEP:
            if fn(s):
                print("  FALSE+ %s" % s[:78])
        for s in STALL:
            if not fn(s):
                print("  STALL- %s" % s[:78])
    print("  caught %d/%d refusals | false-positives %d/%d | stalls %d/%d"
          % (tp, len(REFUSE), fp, len(KEEP), st, len(STALL)))
    ok = (tp == len(REFUSE) and fp == 0 and st == len(STALL))
    print("  %s" % ("PASS" if ok else "FAIL"))
    return tp, fp, st
