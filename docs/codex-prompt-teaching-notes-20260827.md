# codex 提示语料全集消化笔记(2026-08-27)

三个 Explore 并行精读 `~/codes/codex`(commit 343074d):①六份模型提示+templates 两棵子树(15 份全读);②运行时注入的全部教学字符串(context-fragments 体系);③全部工具描述。本文是消化后的技法目录,原文引文见各 file:line。

## 一、系统提示层(教学文本)

- **turn 语义是核心教义**:`gpt_5_2_prompt.md:30/111` "keep going until completely resolved before ending your turn / persevere even when function calls fail / Only terminate when sure solved"。演化史:老版 "Please keep going"(prompt_with_apply_patch:125)→ 5.1/5.2 硬化 "You must…persevere"——**强化路径=被验证有效**。
- **宣布≠动手**:`gpt_5_2:32` "it's bad to output your proposed solution in a message, you should go ahead and actually implement"。realtime 版换句式再讲:"Do not use conversation as a substitute for execution"(backend_prompt.md:27)。
- **教学剂量因模型而异(显式设计)**:codex-tuned 模型的提示(gpt_5_codex_prompt.md)整篇**没有任何 persistence 宣讲**——协议已在权重里;base 模型(5.1/5.2)才长篇教。→ 我们的上游是网页聊天模型=最需要大剂量的那端,握手+契约方向正确。
- **前沿层 continuation.md**(跨 turn goal-loop,防御密度最高):Fidelity"不许换更窄更好测的目标凑绿"(:29);Completion audit"逐条需求找权威证据,绿票只有覆盖需求才算证据,不确定=未完成"(:31-40);Blocked audit"同一 blocker 连续 3 个 goal turn 才许报 blocked,难/慢/不确定都不算"(:43-51)。
- **技法清单**:数字化门槛(3 次格式重试/easiest 25% 跳 plan/3 turns 才 blocked);正反例对照(plan 3 好 3 坏);条件分支指令;plan 状态机六硬约束且 turn 结束前必须结账;compact 四栏账本(进度决策/上下文偏好/待办/关键引用)+ "另一个 LLM 会接手"第三人称定锚。

## 二、运行时注入层(harness 中途对模型说的话)——本轮最大新收获

- **失败当教材(failure-as-teaching)**:超时→`command timed out after {ms} ms\n{部分输出}`(部分输出**前置保留**,不丢,tools/mod.rs:130);审批超时→"Do not assume the action is unsafe based on the timeout alone. You may retry once, or ask the user"(guardian/review.rs:70);Guardian 拒绝→附行为指令"must not attempt the same outcome via workaround…"(review.rs:62);用户打断→`<turn_aborted>` 明说"进程可能还在后台跑/命令可能部分执行"。**失败文本不只报错,还教下一步怎么办**。
- **世界态 diff 注入**:环境/权限/AGENTS.md 等每样一对标记标签,只在哈希变化时重发;**撤销必须显式告知**("These instructions replace all previously provided…"/"no longer apply"),不许静默饿死旧指令。
- **孤儿修复**:缺 output 的 call 原位合成 `aborted`,id 用 UUIDv5 确定性派生保 prompt-cache。
- `## My request for Codex:`(USER_MESSAGE_BEGIN)把上下文与真实请求分界——同我们的 `<user_query>` 抽取。

## 三、工具描述层

- **面故意窄**:核心 5 件套(exec/apply_patch/plan/view_image/web_search),没有 read_file/grep 单件——shell 打天下(与我们单 Shell 工具同构,验证了方向)。
- 技法:参数描述里写默认值+边界("Defaults to 10000 ms; effective range 250-30000");教"什么时候**不用**这个工具";NEVER/IMPORTANT 句式;工具间优先级("Prefer resources over web search");反 busy-wait("Prefer longer waits to avoid busy polling");apply_patch=语法 grammar 硬约束+prompt 只教策略的**双层**;spawn_agent v1 长篇训导→v2 用更强原语(canonical name/fork_turns)收敛描述——**用机制替代说教的演化方向**。

## 四、对我们 web 线的直接可搬项(消化结论)

1. **失败教学注入**(新,高 ROI):我们已有 [TOOL RESULT] 包装层(tr-vis L1),对失败形状的工具结果(超时"did not complete in 30000ms"/非零退出/拒绝)追加一行 codex 式行为指令——超时≠躺平,继续下一条命令。直接打 cap-4 里"安装超时→模型被动等死"的实锤死法。
2. **契约补 continuation.md 三句**:不缩目标凑完成/证据式收尾/blocked 三次门槛(多步任务收尾质量)。
3. compact 四栏账本 → 我们 diet/长上下文路径的改造模板(后置)。
4. 注入打标记+先撕再塞(anchors 清单第 1 条,卫生项)。
5. 已搬:TURN DISCIPLINE(50d5f6a)、方言转译(=apply_patch 双层思路)、诚实三分法、握手(=把"教学剂量"做成会话级机制)。
