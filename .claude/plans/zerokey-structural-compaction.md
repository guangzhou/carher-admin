# zerokey 架构改动：结构压缩（照 Codex 本地 auto-compact），不是截断

## 核心机制（全部有源码依据）

Codex 靠**两层**让长会话不爆：

**第一层：本地结构压缩** —— `codex-rs/core/src/compact.rs:348-368`
当 token 用到窗口的 90%（`openai_models.rs:471`，`(context_window*9)/10`），触发：
1. `collect_user_messages`（`compact.rs:520`）从历史里**只挑 user 消息**，
   工具调用、工具结果、assistant 回复**全丢弃**
2. `summary_suffix` = 最后一条 assistant 消息原文（`compact.rs:344`）
3. 新历史 = 【最近 N 条 user 消息原文，共 2 万 token 上限】+【SUMMARY_PREFIX + summary_suffix】
4. `replace_compacted_history` 用新历史**替换整段旧历史**

**纯本地操作，不调模型，0 额外调用、0 延迟。** 这是我之前没看到的——Codex 本地压缩根本不靠模型做摘要，是纯结构替换，信任"最后一条 assistant + user 诉求原文"足够让下一个模型接着干。

**第二层：工具输出截断** —— 切片 1 已做（`bpi-codex.js` 的 `truncateToolOutput`）。

## 这对 zerokey 的意义

我们的天然优势：网页会话每次本来就是新会话（`responses.js:257` 传 `chatSessionId=null`）。
所以**网关收到 Codex 客户端的全量 input 后，在 `prepareCodexInput` 里做一次结构压缩**，
只把"压缩后的轻历史"发给网页后端。0 额外调用、0 延迟。

**这不是截断（切片 1），也不是调模型做摘要（我验证过可行但贵）。**
是照 Codex 本地压缩做纯结构操作：丢工具结果、留 user 原文 + 最后 assistant。

## 改动点

**文件**：`bpi-codex.js` 的 `prepareCodexInput`（`bpi-codex.js:300`）

**现状**：`replayed = kept.map(replayItemToText)` —— 把**所有**工具调用/结果都 replay 成文本塞进去，一条不丢。这是撑爆的根源：多轮后工具结果累积几万字符，9 轮撞 10 万附件阈值。

**改后**：在 replay 之前先做结构压缩——
```
保留：
  - 所有 developer/system 消息（模型人格、环境，剥掉 permissions 那条不变）
  - AGENTS.md 那条 user 消息（extractCwd 依赖它，丢了模型瞎猜路径）
  - 最近 N 条 user 消息原文（N 先取大，比如最近 8 条）
  - 最后一条 assistant 消息（若有）
  - 最新一条 user 诉求（数组的最后一条 user）
丢弃：
  - 早于最近 N 条的 user 消息
  - 所有 custom_tool_call / custom_tool_call_output（工具调用和结果）
```

## 硬约束（已查实，不能踩）

1. **AGENTS.md 那条必须保留** —— `extractCwd`（`bpi-codex.js:234`）从它取工作目录，
   丢了模型会瞎猜路径。匹配判据：`AGENTS.md instructions for /path`。
2. **additional_tools 必须保留** —— 工具声明，丢了模型不知道有 exec。
3. **developer 系统指令（You are Codex 等）保留** —— 模型人格，剥掉等于自断一臂
   （8/8 vs 7/8 已证伪过不能剥）。
4. **只在总长超阈值时才压** —— 短会话不动，避免无谓丢历史。
   阈值取附件阈值的 70%（7 万字符），对齐 Codex "90% 窗口才压"的思路。

## 为什么不调模型做摘要

我验证过网页会话能当压缩器（5/5 关键信息保留），但：
- 多花一次调用 + 9 秒延迟
- 64 万字符会触发附件上传（鸡生蛋，要分块才能解）
- Codex 本地压缩根本不调模型，是纯结构操作

调模型做摘要（remote compaction v2）是 Codex 的**另一条**路，更重、用于更狠的场景。
我们照它**轻的那条**（本地结构压缩）即可。

## 验证计划（先复现再改）

1. **造一个真实多轮载荷**（含工具结果，不是首轮），量"改前每轮发多大、几轮撞阈值"
2. **实现 `compactInput` 函数**，量"改后每轮发多大"
3. **多轮连贯题回归**（改前的 3/3 那个）—— 压缩后模型还能接着干，不能退化
4. **单测**：压缩丢工具结果、保留 cwd/系统指令/最近 N 条 user
5. **上线 + 前后对比**：撞附件阈值的请求占比、filecite 发生率

## 风险与缓解

- **风险**：丢掉的工具结果里有模型还需要的信息
- **缓解**：
  - N 取宽松（最近 8 条 user + 最后 assistant）
  - 只在超 7 万字符才压，短会话不丢任何东西
  - 打"压了什么"的日志（只含 type/role 计数，不含内容），出问题能查
  - 保留"最后一条 assistant"——Codex 的做法，它通常含本轮结论
- **回退**：`prepareCodexInput` 是纯函数，单测覆盖；线上回退就是还原那一个函数

## 不做什么

- 不调模型做摘要（本地结构压缩已够）
- 不碰 Codex 客户端（全量重发改不掉，也不需要改）
- 不碰已达成的目标（工具调用、引用剥离、首字节）
- 不搭 MCP（你说了 skill+智能体，不需要）

## 顺序

1. 先造多轮载荷、量基线（只读，不改）
2. 实现 `compactInput` + 单测
3. 上线、前后对比
4. 有效则继续 skill 层（切片 3）；无效则回退、重新诊断
