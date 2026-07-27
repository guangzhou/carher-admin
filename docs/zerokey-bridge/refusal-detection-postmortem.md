# 拒答检测:为什么正则分类是错的(2026-07-27 复盘)

**结论先行:我用正则分类模型散文来判断"它拒绝干活了吗" —— 这条路业界七个同类项目里没有一个走。
它不是实现没写好,是方向错了。应该删掉,不是改第 5 版。**

本文档修正本目录 `implementation-and-rollout.md` 的 §3.9 和 §5 —— 那两节把 sub2api
当成工具伪造的参考,这个前提是错的(见下 §2)。

---

## 1. 我做了什么,坏在哪

`_looks_like_refusal()`:约 10 条正则、中英双语、6 个顺序依赖的早返回,外加
一个豁免(`_REPORTED_RESULT`)、一个反豁免(主题类否定)、以及一个防"因此"被读成
指示代词"此"的负向后顾。**7 个提交里改了 4 次**,每次修上一次引入的误判。

当前状态:**两个方向同时错。**

| 方向 | 实测样例 | 后果 |
|---|---|---|
| 误判正常答案 | `Findings: 21 deploys cannot read /patch/responses.js because the cp line is absent.` | 丢弃正确答案、解除粘性、最多再烧 6 次 11-20s 付费调用;两次这样就把**好号**关 30 分钟 |
| 漏判真拒答 | `此环境不能提供 shell,请你自己在终端里执行 kubectl get pods 然后把输出贴给我。` | `_HANDOFF` 和 `_DENIAL` 都命中,豁免却先返回 False → 把"你自己跑贴给我"当**成功答案**发给用户,还把用户钉在这个号上 |

第一句误判尤其说明问题:**那正是代码审查本身会产出的句子**。

根因不是正则写得不好:**自然语言否定不是正则能判定的**。第 5 次返工同样会坏。

---

## 2. 调研:七个项目,零个这么做

源码级核查(不是看 README),七个仓库全部 clone 后搜"我无法 / cannot run /
把输出贴给我"这类分类逻辑:

```
chatgpt-adapter → 0    one-api → 0    roo     → 0
cline           → 0    new-api → 0    sub2api → 0
gpt4free        → 0
```

**一个都没有。** sub2api 里约 40 处 `regexp.MustCompile` 全是限流解析、凭证脱敏、
模型 ID 匹配、session ID —— 针对拒答措辞的搜索只命中两条中文代码注释。

### 2.1 先纠正一个我一直搞错的前提

**sub2api 打的是原生 agent 端点,不是网页端点:**

```go
// backend/internal/service/openai_gateway_service.go:31
chatgptCodexURL = "https://chatgpt.com/backend-api/codex/responses"
```

搜 `backend-api/f/conversation`(网页端点)命中 **0 次**;搜 prompt 注入伪造工具
命中 **0 次**。它用 Codex 的 OAuth 凭证打官方接口,**那个接口原生支持工具调用,
所以它根本不需要伪造**。

它那个 `openai_tool_corrector.go` 只是把**已经成型的** tool_call 里的字段名改一改
(`file_path`→`filePath`),不是从文本里解析出工具调用。

→ **本目录 §3.9/§5 把 sub2api 当工具伪造参考,是错的。** 真正的同类项目是
gpt4free 的 `ToolSupportProvider` 和 chatgpt-adapter。

### 2.2 通用中继:只看状态码和错误字段

one-api `monitor/manage.go:11-44` 就是全部的"这个通道坏了"判断:

```go
func ShouldDisableChannel(err *model.Error, statusCode int) bool {
	if statusCode == http.StatusUnauthorized { return true }
	switch err.Type {
	case "insufficient_quota", "authentication_error", "permission_error", "forbidden":
		return true
	}
	...
	lowerMessage := strings.ToLower(err.Message)   // ← 上游的 error 信封,不是模型回复
```

而且 `controller/relay.go` 明确拒绝重试任何 2xx:

```go
if statusCode/100 == 2 { return false }   // 200 永不重试
```

new-api 同形。它唯一"检测拒答"的地方,是读 Claude **官方给的字段**:

```go
// relay/channel/claude/relay-claude.go:26
if strings.EqualFold(stopReason, "refusal") { ... }
```

**让上游自己申报,而不是猜。** 这是关键的思路差异。

### 2.3 sub2api:只数结构信号,不读散文

`openai_silent_refusal.go` 九个布尔标志全部来自 JSON 结构:

```go
func (d *openAIChatSilentRefusalDetector) IsSilentRefusal() bool {
	return !d.sawContent && !d.sawToolCall && !d.sawFunctionCall &&
		!d.sawUsage && !d.sawError && !d.sawReasoning &&
		d.sawFinish && d.finishReason == "stop"
}
```

配一个"判定出来之前先攥着别发"的缓冲:一见到任何正面信号立刻放行;流结束还没放行
就 `newOpenAISilentRefusalFailoverError` → 换号。**只对"完全空返回"换号。**

还有个精度护栏值得抄:`openAISilentRefusalMinRequestBodyBytes = 64 * 1024` ——
**只对大请求启用**,小请求的空返回是合理的。

### 2.4 chatgpt-adapter:强制哨兵(和我架构最像)

`core/common/agent/agent.go` 注入的模板:

```
你的每次输出都必须以0,1开头,代表是否需要调用工具:
0: 不使用工具。
1: 使用工具,返回工具调用的参数。
```

要求必须用工具时提示词硬化成「**你的本次输必须以1开头**」(等价于 `tool_choice=required`)。

**"它配合了吗"变成读第一个字符 —— 解析问题,不是语义问题。**

解析失败时它**从不分类**,而是走默认值或落回普通补全:

```go
if j == "" {
	if valueDef != "-1" { return toolCallResponse(ctx, completion, valueDef, "{}", created) }
	return false      // ← 当作"没有工具调用",按普通回复处理
}
```

代价:它为工具选择**单独发一次便宜的调用**,所以解析失败只浪费一个小请求,不是一个真实回合。

### 2.5 Cline / Roo:类型检查 + 把"完成"也做成工具

Roo `src/core/task/Task.ts:3481`:

```ts
const didToolUse = this.assistantMessageContent.some(
    (block) => block.type === "tool_use" || block.type === "mcp_tool_use",
)
if (!didToolUse) {
    this.consecutiveNoToolUseCount++
    if (this.consecutiveNoToolUseCount >= 2) { ... }      // ← 连续 2 次才升级
    this.userMessageContent.push({ type: "text", text: formatResponse.noToolsUsed() })
}
```

`src/core/prompts/responses.ts:51`:

```
[ERROR] You did not use a tool in your previous response! Please retry with a tool use.
If you have completed the user's task, use the attempt_completion tool.
```

**这才是那个歧义的真正解法。** 我一直纠缠"纯文字回复到底是正确答案还是拒答",
而它们让 **`attempt_completion`** 也是一个工具 —— **任务做完也必须调工具声明**,
于是"纯文字回复"永远不合法,歧义**在设计上不存在**。

---

## 3. 我做错的三件事

**① 我在做一个没人做的区分。**
"拒答"和"选择用文字回答"在所有七个项目里都是**合并处理**的 —— 因为原因不影响动作。
只有两种处置:要么接受文字当答案(chatgpt-adapter、gpt4free),要么无条件退回重问
(Cline、Roo)。**没人诊断原因。**

**② 我的补救措施用错了。**
Cline/Roo 是**在同一会话里重新提示**;只有 sub2api 换账号,而且**只在完全空返回时**。

> 一个能说出话的账号证明它是活的。**因为散文内容不好就换号,是错的补救。**

而我的 `_refuse` 排除门正是这么干的,还两次误判就关好号 30 分钟。

**③ 答案就在我自己的技术栈里。**
litellm(bridge 就跑在它后面)对同一问题的做法:

```python
percent_fails = num_fails_this_minute / (num_successes + num_fails)
if percent_fails > 0.5 and total_requests_this_minute >= 5:
    cooldown
```

| | litellm | 我的 |
|---|---|---|
| 统计 | **固定 1 分钟桶**的失败率 | EWMA + 时间衰减 |
| 恢复 | **桶到期自动清零**,免费 | 靠衰减 → 收敛到固定点,**永远够不到阈值** |
| 最小样本 | **≥5 个请求**才判 | 无门槛,一次失败就动分数 |
| 触发依据 | HTTP 状态码 / 异常类型 | 模型说的话 |

固定桶的妙处:**恢复不需要任何机制**。而我用衰减,既要它衰减以便恢复、又要它累积
以便排除 —— **两个目的互相打架**,这就是那个数学漏洞(实测连败 40 次仍判"可用")的根源。

---

## 4. 该怎么做

### 删

`_looks_like_refusal()` 的约 10 条正则和顺序依赖的早返回,**整体删除**。
`_SELF`(已是死代码)、主题类豁免、反豁免、`因/由` 负向后顾一并删。

### 换(按价值排序)

| # | 做法 | 抄自 | 为什么对我们有效 |
|---|---|---|---|
| 1 | **注入提示词加哨兵**,回复必须以 `0:`/`1:` 开头 | chatgpt-adapter | 配合与否变成**解析**问题;要求工具时硬化成"必须以1开头" |
| 2 | **把"完成"做成必需工具**(`attempt_completion`) | Cline / Roo | 纯文字永远不合法 → **歧义在设计上消失**,不再需要判断文字是否算答案 |
| 3 | **只对完全空返回做结构化换号** + 攥着别发 | sub2api | 无内容/无工具调用/无 usage/无 error/finish=stop,是纯结构判断 |
| 4 | **健康分改固定桶失败率 + 最小样本门槛** | litellm(自己栈里就有) | 恢复免费,且一次误判不会关掉好号 |
| 5 | **连续计数而非一击**(≥2 次才升级) | Roo | 单次杂音自动纠正,不惊动账号层 |

### 结构信号:是标准做法,但不是判决

"历史以 tool 结果结尾 + 本轮没吐 tool_call"**是**标准公式,但两处用法都当**前置条件或提示**,不当失败判决:

- chatgpt-adapter `NeedExec`:最后一条是 tool 结果且没强制工具 → **跳过工具选择,让它写文字**。
  **tool 结果之后的文字回复被推定为正确。**
- Roo:没吐工具 → 追加固定的 `noToolsUsed` 重新提示,**要求连续 ≥2 次**才升级。

**要砍掉我原来那个第三个条件"且任务未明显完成"** —— 没人算这个,而且它离开语义判断
就不可知,正是我陷进去的那个坑。Cline/Roo 用 `attempt_completion` 替代它:
**让模型自己声明完成**,和 new-api 读 Claude `stop_reason == "refusal"` 是同一个动作 ——
**让上游结构化地说出来,而不是从散文里推断。**

---

## 5. 方法论教训(比技术结论更重要)

**① 自建语料不是回归基线。**
我的语料 30/30、误报 0/26 **一直显示 PASS**,而上面所有 bug 都同时存在。它只覆盖
我想到的措辞。替换一个匹配器时,**旧匹配器能抓的每一种措辞都必须先变成测试**。

**② 只验证自己想要的方向,等于没验证。**
衰减那个 bug:我测了"两个半衰期后坏号能回来"(我想要的),**没测"坏号会不会被正确排除"**
(我依赖的)。所以漏了"排除门整体失效"。

**③ 先证明代码真的在跑,而且是**部署产物**在跑。**
heartbeat 那次我测了源文件说"10/10 通过",而 `litellm-proxy.yaml` 里内嵌的**部署副本**
还是旧的 —— 仓库自己的 `sync-litellm-callbacks.py check` 一跑就露。

**④ 先调研再动手。**
这七个项目的答案(不要正则分类散文)如果第一天就查,能省掉 4 次返工和至少 4 个
架构性缺陷。**"先把小闭环做顺畅"的前提是方向对**,方向错的话闭环只会把错误固化。

---

## 6. 未核实 / 待办

- **第二项调研(健康分该怎么算)尚未回收**:Envoy outlier detection
  (`consecutive_5xx`、`success_rate_request_volume`、`base_ejection_time` 递增)、
  Finagle `FailureAccrualFactory` 的 probation 机制。预期结论是
  **"弹出时长指数增长 + 回来时先探一个请求"**,把"学会它坏"和"偶尔复查"解耦 —— 待确认。
- **无人处理我们这个上游形态**:七个项目没有一个面对"被强扭成 `/v1/responses` 的
  ChatGPT 网页会话"。sub2api 和 chatgpt-adapter 最近,但都不面对"网页模型叙述计划
  而不继续执行"这个具体故障。**所以"没人用正则分类"是强证据,但不是完美可迁移的。**
- **chatgpt-adapter 的哨兵真实遵从率未实测**。但它代码里有三层解析兜底
  (`j == ""` / `name == ""` / unmarshal 失败),说明**不遵从很常见** ——
  这本身是有用的信息:即使是(C)方案也假定遵从不完美,只是**降级方式是结构化的,不是语义的**。
- **Cline 的 `noToolsUsed` 调用点不在开源仓库里**(只有提示词字符串),决策逻辑已挪进闭源层。
  引用的是 Roo(Cline 的 fork)保留的完整调用点。

---

## 7. 关联

- 本目录 `implementation-and-rollout.md` §3.9 / §5 —— **被本文档修正**(sub2api 前提错误)
- `scripts/chatgpt-onboard/zerokey-codex/bridge/testkit/README.md` —— 闭环测试工具
- 记忆:`feedback_dont_trust_metric_moves_without_verifying_code_is_live`、
  `feedback_multiturn_bugs_need_closed_loop_harness`
