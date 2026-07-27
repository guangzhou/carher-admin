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
]


def score(fn, verbose=False):
    tp = sum(1 for s in REFUSE if fn(s))
    fp = sum(1 for s in KEEP if fn(s))
    if verbose:
        for s in REFUSE:
            if not fn(s):
                print("  MISS   %s" % s[:78])
        for s in KEEP:
            if fn(s):
                print("  FALSE+ %s" % s[:78])
    print("  caught %d/%d refusals | false-positives %d/%d"
          % (tp, len(REFUSE), fp, len(KEEP)))
    return tp, fp
