# 网关压缩治超巨会话（#25）—— harness 心得与作业计划

> 定位：设计+执行文档。背景见 `docs/acct-multiaccount-incremental-transport-goal.md`（主目标：增量传输）；
> 本项治的是增量传输覆盖不了的部分。代码与单测在 `scripts/litellm-callbacks-compact/`，
> 激活步骤见同目录 `RUNBOOK.md`。判据记忆：`project_codex_harness_ws_v2_research_2026_08_23`。
> 撰于 2026-08-23 晚；harness 源码指 `~/codes/codex`（openai/codex，2026-08-19 开源，commit 343074d4）。

---

## 一、为什么做（真实目标，非"做一个压缩功能"）

1. **止血账号额度**：6.5% 的畸形会话（单轮 2-18MB 历史回放）烧掉全池 1/3+ 的 7 天桶额度
   （每轮 ~50 万 token）。账号是真金白银——省桶 = 少买号、少养号、少接码续订。
   【事实：SpendLogs 30min 窗实测，2-8MB 档 24 请求均计费 507K tokens】
2. **让这批会话也吃到 WS 增量红利**：>16MiB 上不了 WS（上游单消息上限，二分实测
   15MB ACCEPT / 16MB REJECT 1009），瘦身后能上。
3. **绝不为省钱伤用户**：质量回退不可接受；灰度、可回滚、看实际内容验证——与
   换号硬约束同族纪律。
4. **方法照抄官方而不是自己发明**：harness 已开源，压缩是它的核心资产之一
   （官方口径：harness 让 token 用量降 6 倍）。

---

## 二、harness 压缩源码深读心得：三层递进哲学

> 核心哲学一句话：**不删东西，只"掏空"东西；用户说过的话一个字不丢。**
> （我们的 v1 设计"丢孤儿输出+只保首尾"被这次深读推翻并重写。）

### 第 1 层：工具输出改写（机械层，我们主要抄这层）

`trim_function_call_history_to_fit_context_window`（core/src/compact_remote.rs:399）：

- **一个 item 都不删、调用配对绝不切断**——只把 `function_call_output` /
  `custom_tool_call_output` / `ToolSearchOutput` 的 **output 内容**替换为截断占位
  （`truncated_output_payload`），`call_id`/`name`/结构原样保留；
- 逐项改写直到 token 估算装进上下文窗；
- remote v2 调用前**必先跑这层**——因为压缩请求自身也要能装进上下文窗。

启示：孤儿问题的正解是**根本不制造孤儿**。历史的"执行骨架"（谁调了什么工具）
永远保留，被掏空的只是陈旧输出的"内容体"——这恰是巨会话的肥肉所在（Cursor 工具输出）。

### 第 2 层：inline 模型摘要（语义层）

`compact.rs`：

- 用 SUMMARIZATION_PROMPT 让**模型自己写交接摘要**（提示词原文大意："你在做上下文
  检查点压缩，为接手的下一个 LLM 写交接总结：当前进展、关键决策、约束与偏好、
  待办、继续工作所需的关键数据"）；
- 新历史 = **全部用户消息**（`COMPACT_USER_MESSAGE_MAX_TOKENS=20K` 预算内倒序优先
  最新，超预算才截）+ `SUMMARY_PREFIX` 标记的摘要；
- `is_summary_message` 识别自家摘要**防递归收集**（幂等思想）；
- 压缩过程中撞 ContextWindowExceeded → `remove_first_item` 从头删一项重试
  （"从头删"注释：保 prefix cache + 保最近消息）；
- 多次压缩后**主动提示用户**："长线程+多次压缩会降低准确性，建议开新会话"。

启示：用户消息=任务定义，全保；"忘事"的正解不是保尾巴，而是**语义摘要 + 用户消息全集**。

### 第 3 层：remote v2 服务端压缩（最新代，参照系）

`compact_remote_v2.rs`：

- 上游有 unary 端点 **`POST /responses/compact`**，服务端完成摘要；
- `RETAINED_MESSAGE_TOKEN_BUDGET = 64_000`——注释明说 mirror 服务端保留默认；
- `MAX_RETAINED_AGENT_MESSAGE_TOKENS = 10_000`——assistant 消息单条保留上限；
- 保留图片、可选保留 developer messages（feature 门）；
- 返回的 `replacement_history` 整体换装本地历史，initial context 注入到
  "最后一个真实用户消息之前"；
- 压缩时**特意保留** `internal_chat_message_metadata_passthrough`——客户端自有
  元数据（与我们在 WS 增量回显匹配中剥掉它的判断互洽：它是易变的客户端字段）。

### 触发机制（session/context_window.rs）

- 按**真实 token 用量**（API usage 累计）触发，不是字节估算；
- 双 scope：`Total` / `BodyAfterPrefix`（只算初始前缀之后新增的）；
- `auto_compact_fallback_buffer_tokens` 缓冲带 + 模型上下文窗硬顶双闸。

启示：网关侧没有跨轮 usage 累计，只能用字节/4 近似——这是我们与官方的已知差距，
对图片类内容失真（字节大 token 小）。

---

## 三、我们的网关版设计（v2.1，官方结构的无模型简化）

- **载体**：外层 litellm-proxy 的 `chatgpt_responses_normalize.py`（litellm-callbacks CM），
  per-key 灰度名单 `_COMPACT_ALIASES`（默认仅 `compact-canary-01`，名单外零变化）。
- **触发**：input 序列化 >2MB（对应实测"每轮 50 万 token"档）。
- **变换**（对照官方第 1 层 + 第 2 层的保留原则）：
  1. 用户消息：**全保不动**；
  2. 工具输出项：>8KB 的把 output 内容换成占位 `[gateway-truncated: N chars omitted; head] <前512字符>`，
     call_id/name/配对结构原样；
  3. assistant 消息：>40KB（≈官方 10K tokens）截断留头；
  4. reasoning/调用项等小体积项：不动；
  5. 幂等：占位前缀识别，二次进入不叠加。
- **不做**（v2.1 边界）：模型摘要（第 2 层）、服务端 /responses/compact（第 3 层）——列为 v3 候选。

### ⚠ 关键设计修正记录（K5，防翻车）

v2 初版是"预算驱动、从最老开始掏，掏够就停"。**这与 acct pod 的 WS 增量前缀账本冲突**：
会话每长一轮，掏空前沿前移一格 → 同一位置的 item 两轮内容不同 → 每轮 prefix_break
→ 压缩会话永远全量、目标 2 自我打架。
**修正（v2.1）**：改为**确定性逐项规则**——触发后对**所有** >8KB 工具输出统一掏空。
同一 item 每轮变换结果恒同 ⇒ 前缀稳定 ⇒ 与 WS 增量兼容。
（逻辑必然，另以单测断言：会话追加一轮后旧 item 变换结果逐字节不变。）

---

## 四、缺失信息清单（诚实）

| # | 缺什么 | 影响与获取方式 |
|---|---|---|
| K1 | 计费下降是否真实（上游计费行为不一致：2-8MB 档计 50 万 vs 9MB 单例只计 1 万） | 核心收益假设，S3 canary 实测定 |
| K2 | 巨会话内容构成（工具输出占比/有无 base64 图片） | 决定瘦身幅度；影响字节→token 估算 |
| K3 | /responses/compact 能否用 acct pod 凭据从网关侧调通 | v3 前提，未测 |
| K4 | 质量回退可接受度（受影响的是具体真人用户） | 灰度期人工抽查/反馈通道 |
| K5 | ~~与 WS 增量的前缀交互~~ → 已发现并修正（见上节） | — |
| K6 | **表述纠正**：网关压缩**不省客户端→网关上行**（压缩发生在收到之后）；省的是网关→上游+计费。客户端上行要靠 #24 WS ingress 或客户端自压缩 | 收益口径修正 |

---

## 五、最小可执行步骤（不跳步）

### S1. 设计修正落码：预算驱动 → 确定性逐项规则
- 输入：v2 代码（`scripts/litellm-callbacks-compact/chatgpt_responses_normalize.patched.py`）
- 动作：改为"触发后所有 >8KB 工具输出统一掏空"；新增单测"追加一轮后旧 item 变换恒同"
- 预期输出：v2.1 文件 + 全套单测
- 验收：全部单测过 + 前缀稳定性断言过

### S2. 激活（前置：Phase 2 观察窗收口，两变量不同时动）
- 输入：v2.1 文件、CM 备份、RUNBOOK
- 动作：备份 CM → 替换单 key → proxy 滚动重启 → 建 canary key `compact-canary-01`
- 预期输出：proxy 全 Running；默认流量日志零 compact 计数
- 验收：滚动零中断 + 抽 3 条非 canary 请求确认无压缩痕迹

### S3. 合成会话 E2E（K1/K5 首次实测）
- 输入：canary key + 脚本化 >2MB 多轮会话（事实埋早期工具输出、任务埋用户消息）
- 动作：跑 5 轮，收集：compact 计数日志 / 上游 200 / 答案连续性 / SpendLogs 逐轮计费 / acct pod ws_incr 命中
- 预期输出：kb_before/after、逐轮 token 数列、命中率
- 验收：①答案连续 ②计费较对照组（同剧本非 canary key）降 ≥50%〔阈值为推测，实测后修〕③压缩会话命中增量 ④零 400

### S4. 真实流量单 key 灰度
- 输入：S3 全过；巨会话真实 key 清单（cursor-zhuge 等已定位）
- 动作：名单加一把真实 key → restart → 观察 24h（体积/计费/错误率/反馈）
- 预期输出：该 key 前后对比数字
- 验收：计费下降兑现 + 零投诉零错误增量；否则摘 key 秒回滚

### S5. 扩灰度与缺省化决策
- 输入：S4 数据
- 动作：逐批加剩余巨会话 key；两周稳定后评估"默认开+豁免名单"
- 验收：全池桶消耗周环比下降可测（quota 表口径）；缺省化交用户拍板

### S6.（可选 v3）服务端摘要
- 输入：K3 最小探针（acct pod 凭据调 /responses/compact）
- 动作：可行则把占位升级为模型交接摘要（官方第 2/3 层）
- 验收：S3 同套判据下质量优于 v2.1

---

## 六、事实 / 推测标注

- **事实**（数据或源码钉着）：巨会话规模与计费实测；16MiB 上限；官方三层机制全部细节
  （file:line 可查）；单测结果；K6 纠正。
- **逻辑必然**（无需实验）：K5 前缀不稳定机制；确定性变换恒同 ⇒ 前缀稳定。
- **推测**（等实测）：计费降幅；质量无感（依据"被截内容本就被上游丢弃"，但
  "上游丢弃的恰好是模型不需要的"仍是推断）；桶容量收益 15-20%（30min 窗外推）；
  K3 可行性；S3 的 50% 阈值。
