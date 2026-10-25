---
name: codex-deepseek-tool-triage
description: >-
  排查 Codex CLI 打 198 LiteLLM 的 deepseek 模型时的静默故障 —— 输出裸文本
  `<｜｜DSML｜｜invoke name="...">`、模型"编造"不存在的工具名、工具调用出来了
  但客户端执行不了（"命令执行被中断"）、以及长会话压缩报
  `remote compaction v2 expected exactly one compaction output item`。
  这几类**上游全程 200、零错误**，只看状态码 / 只 grep 400 永远查不到。
  一键体检脚本：`scripts/codex-deepseek-triage.py`。
  Use when: Codex/Cursor 打 deepseek-v4-flash-responses 出现 DSML 裸文本 /
  工具名对不上声明 / 工具不执行 / compaction Fatal / stream disconnected；
  or Codex 打任何 deepseek 名字报 400「API 异常 (req: xxx)」（`--rid` 反查原文）；
  or `Input items array must not be empty` / 空 input / 看截图后报错 / view_image（根因 6）；
  or 改完 deepseek callback 要上线 + 车道回归（`litellm-198-deepseek-lane-regress.py`）；
  or VS Code / Cursor（chat 接口）打 deepseek 带工具历史报 400 / `502 Request Failed: 400`、
  「新消息正常 try again 就挂」（根因 5，`--only sigs` 看全站谁中招）；
  or 排查任何「HTTP 200 但客户端行为不对」的网关问题需要方法论。
---

# Codex × DeepSeek 工具调用排障

> 2026-08-06 生产故障全记录。**这一整天先后在四个方向打了补丁全部打偏**
> （tool_choice / custom tool 改名 / 并行块劈开 / `fc_` id 前缀），
> 真凶是靠「同时段横向对照」逼出来的。先读「方法论」那节再动手。

## 症状 → 根因 速查

| 症状 | 根因 | 修复 |
|---|---|---|
| 输出裸文本 `<｜｜DSML｜｜invoke name="exec">` | 顶层 `tools=[]`，模型无工具可调 → 把调用写成文本 | 提升 `additional_tools`（`2b14561`）|
| 模型"编造"不存在的工具名（`list_files`/`exec_command`，每次都不一样）| **同上** —— 没有声明可依据只能瞎编，不是历史污染 | 同上 |
| 工具调用正常出来了，但客户端执行不了 | 客户端声明 `custom`，收到 `function_call` 认不出 | 出站还原 `custom_tool_call`（`090a1d7`）|
| `remote compaction v2 expected exactly one compaction output item, got 0` | DeepSeek 忽略 `compaction_trigger`，回的是 `reasoning`+`message` | 入站换摘要指令 + 出站包 compaction（`f6a038f`）|
| `stream disconnected: Incomplete response, reason: max_output_tokens` | **客户端**发的 `max_output_tokens` 装不下摘要 | 客户端侧，见下节 |
| 400「API 异常」，原文 `reasoning_content ... must be passed back` 或 `response_format type is unavailable`，**call_type=aresponses** | Codex 打的名字是 **mode:chat 组**，请求走 responses→chat bridge：转换时丢 reasoning、把 `text.format` 译成 chat 的 `response_format` | 建 `<名>-responses` 原生组 + `_RESPONSES_GROUP_REWRITE` 加映射（`e291064`，见「根因 4」）|
| 同一句 `reasoning_content ... must be passed back`，**call_type=acompletion**（VS Code / Cursor 走 chat）；新会话正常、带工具历史的 try again 必挂 | OpenAI 形客户端回放历史**从不带** `reasoning_content`；DeepSeek 要求**每条**带 `tool_calls` 的 assistant 都有 | chat 路径每条补占位（`db186fd`，见「根因 5」）|
| 400 `Input items array must not be empty`，落点 `openrouter-glm-5.3-flash`；进网关几 MB、出去 0 条；前一轮调了 `view_image` | 截图 base64 几 MB 但 token ~1k，`_trim_oversize_input` 按字节判超限删光 | 图片不计字节 + 不切调用块（`ea4da5c`，见「根因 6」）|

## 根因 4：responses 请求打进了 chat 组（2026-10-10）

用户只看得到 `API 异常 (req: 36ece9a2)`。`req` 是 request_id 前 8 位，原文在 SpendLogs：
`python3 scripts/codex-deepseek-triage.py --rid 36ece9a2`（自动对签名）。

单变量实测（同一份请求，只换组）：

| 形状 | v4.1 chat 组（bridge）| 同上游原生 `-responses` 组 |
|---|---|---|
| 工具历史里没带 reasoning 项 | 400 `reasoning_content must be passed back` | 200 |
| `text.format=json_schema` | 400 `response_format type is unavailable` | 200 |
| 原样回传 reasoning 的多轮工具调用 | 200 | 200 |

⇒ 不是 DeepSeek 抖动、也不是客户端坏了：同一份请求换到原生 `/responses` 就是 200。
简单请求两边都能过，所以这个组在 3 天里成功和失败并存（252 / 235），只数成功率看不出来。

防线：DeepSeek 每个 mode:chat 组都要有 `-responses` 孪生组 + 改写映射。
新建组后必跑 `--only groups`：列出所有官方 chat 组，`⚠ 无改写` 的就是下一个坑
（10-10 时 `deepseek-official-v4-flash` 是 ⚠，但 7 天零 responses 流量，暂不处理）。
⚠️ 改写只对 `/v1/responses` 生效，chat / anthropic 流量不受影响；key 白名单校验用的是入站名，不用补。

## 根因 5：chat 路径工具历史缺 reasoning_content（2026-10-14）

用户报「新消息正常、try again 报 `502 Request Failed: 400`」。全站 24h 95 次，一个 VS Code 用户就占 76 次。
**这个病 08-04 就记过**（`feedback_deepseek_pairs_tool_calls_by_adjacency` 末节：「chat 侧没有对应的补丁」），
当时只修了 responses 那条路，chat 这条两个月没人补。建 DeepSeek 组先翻那条记忆。

经 198 打 `deepseek-v4.1-flash`（`custom_openai/deepseek-flash` @ api.deepseek.com）的单变量实测：

| assistant 消息形状 | 结果 |
|---|---|
| 带 tool_calls、无 reasoning_content | 400 |
| 带 tool_calls、`reasoning_content=" "` 或 `""` | 200 |
| 两轮工具，只有一轮带 | **400**（跟 responses「每轮开头补」不一样：chat 要**每条**都带）|
| 普通回复（无 tool_calls）不带 | 200（不用补）|
| 去掉 `tool_calls[].type` | 200（外部同事据此判成「type 被拒」——**相关但不是原因**，上游原话是缺 reasoning_content；为何能过没查，别当修法）|

**LiteLLM 自带补丁为什么没救**：`DeepSeekChatConfig._fill_reasoning_content` 有两道门，这里都过不去：
① 组是 `custom_openai/` 前缀，走的是 `OpenAILikeChatConfig`，根本不进 DeepSeek 配置；
② 就算进了，`_thinking_mode_active` 还要客户端显式传 `thinking={"type":"enabled"}`，VS Code 不传。

**修法**：`deepseek_responses_adapt.async_pre_call_deployment_hook` 加 chat 分支 `_adapt_chat`，
门控 = model 含 deepseek **且** `api_base` 含 `api.deepseek.com`（自建 GPU / 网宿不碰）。
有 `provider_specific_fields.reasoning_content` 就还原，否则补 `" "`。日志计数 `chat_reasoning_content_fill`。
单测 `tests/test_deepseek_chat_reasoning_content_fill.py`（9 条）。

**验收方法**（照抄）：隔离环境 = 生产 pod 里起一个独立 `litellm.Router`，**同一份**载荷先过旧 callback 再过新 callback，
key 从 `LiteLLM_ProxyModelTable` 密文行在 pod 内解密（`/model/info` 不回 api_key）；
**第一格必须是不带工具的阳性对照**，它不是 200 整轮作废（10-14 第一次跑全 500，就是 key 没解出来）。
上线后经 NodePort 按「1轮/2轮/并行 × 流式/非流式」各 8 发，再逐副本数日志里的 fill 计数。

⚠️ 上线后再数失败，**先剔掉自己的探针**：SpendLogs 里 `api_key` 关联不到 key_alias（master key）且时间在新 pod
`startTime` 之前的，是你自己上线前的复现请求，不是「修了还在挂」。

## 根因 6：截图被按字节当超限，整单删成空 input（2026-10-22，10-25 修）

**签名**：400 `Input items array must not be empty`，落点常是兜底链末腿 `openrouter-glm-5.3-flash`
（看着像 OpenRouter 的锅，不是）。`--rid` 或 `litellm-198-key-error-triage.py rid <id>` 会打
「进网关 body xMB / 出去 0 条 / 前一轮调的工具：view_image×N」。

**链条**：Codex `view_image`（detail=original）把截图 base64 原样塞进 `function_call_output`
→ 单张几 MB，**token 才 ~1k** → `_trim_oversize_input` 按**字节**判 >3MB → 从老删到只剩那条
带图 output → 它的 call 已删 → `_repair_tool_pairing` 当孤儿删 → `[]`。

**判别三步（按这个顺序，10-22 我跳步错了三次）**：
1. `content-length`（`proxy_server_request.litellm_metadata.headers`）大、出去 0 条 ⇒ **我们删的**，不是客户端。
   UA `Python/aiohttp` 是 ws-ingress（Codex WS 入口）转发的，不是用户脚本。
2. 看**前一轮 `response.output` 调了什么工具** —— SpendLogs 文本列截到 2048 字符，量不出 item 多大，
   但工具名是完整的。13/13 次前一轮都有 `view_image`。
3. 判「太大」只认 **token**。直打 api.deepseek.com：1 张 3.5MB 图 in_tok=1033，20 张 44.8MB 仍 200；
   纯文本 4MB=400 `maximum context length is 1048576 tokens`。⇒ 图片既不撞 413 也不撞上下文墙。

**排除过的方向（别再走）**：
- 「DeepSeek 10-17 升级 V4.1-Flash-1017」—— 官方 changelog 确有升级，但直打行为与升级无关。
- 「补丁升级时被扔了」—— 三车道 CM 与 git 逐字节一致。
- 「改成截断工具输出头尾」—— 截图不是文本，截了等于丢图；且文本截断也是继续瞒着 Codex。
- 「回 `context_length_exceeded` 让 Codex 压缩」—— Codex 按 API 回报的 token 判压缩
  （`core/src/session/context_window.rs`，窗口 90%）；图片 token 少，不压缩是**对的**。

**修法**（`ea4da5c`）：`_budget_size` 把 `data:image/...` 每张按 4KB 计；切点不落在工具调用块中间；
删不出东西就原样发（上游给可读报错，不再造空请求）。

**上线前后对照**：`scripts/litellm-198-deepseek-lane-regress.py`（10 条）。上线前 prod 7/10（3 条截图
400）、gray 新代码 10/10、上线后 prod 10/10。⚠️ deepseek 看完图常继续调工具 —— 「回答为空」不算失败。

**为什么 10-19 才冒头**：未证实。10-10 起 Codex 直选 deepseek 走原生 `-responses` 组 + 一键目录模板
`gpt-5.5` 带 `input_modalities=[text,image]`、`supports_image_detail_original=true`，可能相关，没验。

**仍未处理**：纯文本真超 1M token 时，DeepSeek 回 400 `maximum context length`，但 `code` 不是
`context_length_exceeded`，Codex 认不出 ⇒ 不会自动压缩。10-25 时没有真用户撞到，没做。

## 先排除：上游自己挂了（2026-10-15）

「很多人说 deepseek 不回 / 是不是你昨天的改动」——**先跑 `--only health`**，别先怀疑自己的补丁。
10-15 北京 08:11 起 DeepSeek 官方自身故障（status.deepseek.com incident，V4 Pro / V4.1 Flash / 网页都受影响）：
`deepseek-v4.1-flash` p50 从 4~7s 跳到 **170~340s**，夹着 `900-second timeout` 和 `Service is too busy`；
阿里云 her（完全不经过 198 的 callback）同一时段同样报错 = 与网关补丁无关的阴性对照。12 点后 p50 回到个位数。
我当时先说「是我补丁导致的」，零数据，作废 —— 见 CLAUDE.md 三段式。
应急切换（改映射到网宿 / 恢复后官方为主网宿兜底）走 skill `litellm-198-fallback-chain` §10。

## 根因 1：Codex responses-lite 把工具塞在 input 里

Codex 的 "responses lite" 线路**不用顶层 `tools`**，而是放在 `input` 的一个 item 里：

```json
{"type": "additional_tools", "role": "developer",
 "tools": [{"type":"custom","name":"exec", ...},
           {"type":"function","name":"wait", ...},
           {"type":"function","name":"request_user_input", ...}]}
```

LiteLLM 1.90.2 全库 grep `additional_tools` = **0 处**，完全不认识，原样透传。
DeepSeek 也不认这个 input item type，于是**收到零个工具**。

**单变量实测**（只改 `tools` 空不空，其余全同，真打 api.deepseek.com）：

```
tools=[]        -> 3 次里第 2 次吐出 <｜｜DSML｜｜invoke name="list_files">
tools=[真工具]  -> 5/5 干净，全部结构化 function_call
```

修：`deepseek_responses_adapt._hoist_additional_tools()` —— 摘出 items → 工具提到
顶层 `tools` → 从 `input` 删掉这些项（`namespace` 类型要摊平）。

**上游 PR #33228 修的是同一问题，但只做在 `bedrock_mantle` provider 里；
deepseek 走 openai provider，那条路上游没做 —— 升级 LiteLLM 到 1.95 也修不了。**

## 根因 2：custom tool 降级后没还原

DeepSeek 的硬约束（实测）：

```
custom/exec          -> 400 Unsupported custom tool: 'exec'. Only 'apply_patch' is supported.
custom/apply_patch   -> 200
function/exec        -> 200
```

所以入站**必须**把 `exec` 降级成 function，没得选。但客户端声明的是 custom，
收到 `function_call` 认不出这条调用 → 一条都执行不了。

**判据**：`custom_out_to_function` 计数恒为 **0** —— 客户端从没回传过 output，
说明它根本没执行（而不是执行失败）。

修：出站在 **SSE 字节层**还原成 `custom_tool_call`，并同步换参数事件名
（`function_call_arguments.*` → `custom_tool_call_input.*`）。

**为什么必须在字节层**：改 pydantic 事件对象的 `type` 会让序列化器失配，
`response.completed` 那一帧崩 `PydanticSerializationError: 'MockValSer' object
is not an instance of 'SchemaSerializer'`，**整条流断在最后一帧**（无 completed、
无 `[DONE]`），比原故障严重。详见 [litellm-hook-dev](../litellm-hook-dev/SKILL.md)。

## 根因 3：compaction v2 —— DeepSeek 不认 `compaction_trigger`

**先纠正一个易犯错误**：`remote compaction v2` **不打 `/v1/responses/compact`**。
那是 V1。直接测那个端点会得 404，据此断言「DeepSeek 不支持压缩」是**错的**
（我 2026-08-06 就这么误判过一轮）。

官方协议（读 `openai/codex` 源码确认，非推测）：

* `codex-rs/core/src/compact_remote_v2.rs` —— v2 **复用标准 Responses 流**，
  只在 `input` 末尾追加 `{"type":"compaction_trigger"}`；计数只认
  `ResponseEvent::OutputItemDone` 里的 `Compaction` 变体，
  `compaction_count != 1` 就 Fatal。
* `codex-rs/protocol/src/models.rs`：

  ```rust
  #[serde(alias = "compaction_summary")]
  Compaction {
      id: Option<ResponseItemId>,   // 可选
      encrypted_content: String,    // 必需
      internal_chat_message_metadata_passthrough: Option<..>,
  }
  ```

  **`encrypted_content` 只是普通 `String`** —— Codex 当不透明字符串存、下轮
  原样回传，不是 OpenAI 私有加密格式。**这条是「网关可以自己造 compaction
  item」的决定性依据**，读源码才敢确认。

DeepSeek 实测（真打 api.deepseek.com，单变量）：

```
带 compaction_trigger  -> reasoning+message，内容是「继续对话」
不带（对照组）          -> 同上，与带 trigger 无差异  ⟹ 完全忽略该标记
显式要摘要             -> message 是真摘要
```

修（`f6a038f`）：入站把 trigger 换成显式摘要指令 → 出站在 **SSE 字节层**把
message 包装成 compaction item **并丢掉 reasoning**（理由是它带
`encrypted_content` 会污染下一轮上下文）。

> **2026-08-07 更正**：这里原先写的理由是"它占 `output_item_count`"，**错的**。
> `compact_remote_v2.rs` 有测试 `collect_compaction_output_accepts_additional_output_items`
> —— 额外的非 compaction item 是允许的，只要 compaction 恰好 1 个。
> 丢 reasoning 的真实理由只有"污染下一轮上下文"这一条。

### ⚠️ 已知短板：大上下文会被客户端的 max_output_tokens 截断

2026-08-06 李云的 case：

```
prompt_tokens = 287,688
completion_tokens = 4096, 4096, 4096 ...  ← 连续 6 次同一个值
报错：stream disconnected: Incomplete response, reason: max_output_tokens
```

**判据：`completion_tokens` 反复等于同一个整数 = 撞上限被截断**，不是模型问题。
那个 4096 是 **Codex 客户端自己发的**（不是我们配的 —— deployment 里是 128000，
callback 的 counts 也证明改写分支没触发）。

DeepSeek 实测支持到 **65536**。287k 上下文的摘要写不进 4096 token。
待办：compaction 请求按输入规模动态给输出预算 + 摘要指令加长度约束。

### 竞品对照（CCX）

CCX issue #179 同款问题。commit `5e322fe7` 只做到「规范化成单个 **message**
item」，**并未真正构造 compaction item**，所以又出 v2.9.2 改走本地 compact 绕开。
我们这版按官方 schema 造的是真 compaction item。

## 🔑 方法论：200 但结果不对，先做横向对照

这一整天走偏的根源是**一直拿出站形状猜入站载荷**。四个白打的补丁：

| 打偏的方向 | 事后如何被证伪 |
|---|---|
| `tool_choice` 对象形态 | 用户真实请求 `tool_choice=None`，压根没发这字段 |
| custom tool 改名 | 用户 tools 全是 `function/xxx` 扁平形状 |
| 并行块被劈开 | 用户序列是纯串联，无并行块 |
| `fc_` id 前缀 | 真 Codex schema 直连测 5 次**零泄漏**，前缀不是原因 |

**真正破局的两步**：

1. **用户问「为啥我的 gpt 完全没有这个问题」** → 跨模型对照把变量锁死在响应侧
2. **同时段横向对照**：39 个 deepseek 请求，**38 个正常、只有他那条异常**

```
正常:  tools=['function/execute_shell_command', ...]  tool_choice=None
异常:  tools=[]  tool_choice='auto'  seq_in=['additional_tools', ...]
```

**先做对照，再读代码。** 日志里没有错误可读时，对照组是唯一能锁死变量的东西。

### 还踩过的坑

- **把别人的流量当成用户的**：日志里那批 46 个工具（`ask`/`bash`/`todo_write`/
  `web_fetch`）是 **Claude Code** 的工具集，不是 Codex。基于错样本分析了好几轮。
  → 抓流量必须用 key hash 或独有工具名锁定是谁的。
- **线索早就在探针输出里但被跳过**：`tools=[]` 第一次抓包就打出来了，因为只盯着
  自己想验证的字段而漏掉。→ 探针输出要整行读，不要只 grep 想看的 key。

## 排查 SOP

**第 0 步：跑一键体检**（把下面手工步骤 1~2 全自动化了）

```bash
python3 scripts/codex-deepseek-triage.py            # 默认 30 分钟窗口
python3 scripts/codex-deepseek-triage.py --since 2h --key cursor-liyun1-t6xm
python3 scripts/codex-deepseek-triage.py --rid 36ece9a2    # 用户只给了 req id
python3 scripts/codex-deepseek-triage.py --only groups     # chat 组 × 改写覆盖
python3 scripts/codex-deepseek-triage.py --only sigs --since 24h  # 全站失败按 组×call_type×签名 + 受影响 key
python3 scripts/codex-deepseek-triage.py --only health --since 8h # 按小时 p50/p95（OWUI 单列）：先判上游是不是自己挂了
```

🔴 pod 日志只保留几分钟，用户报错几十分钟后再 grep 一定零命中。直接用 `--rid` 查 SpendLogs。

它会自动标注 counts 含义、扫静默故障关键词、并提示
「`completion_tokens` 反复同值 = 客户端 max_output_tokens 撑爆」。
只读不写，安全。

手工路径（脚本不够用时）：

```bash
# 0. 198 直连（不走 jms）
sshpass -p '<pw>' ssh -o StrictHostKeyChecking=no cltx@10.68.13.198 \
  'echo "<pw>" | sudo -S kubectl -n litellm-product get pods -l app=litellm-proxy'
```

**1. 先确认是谁的流量**（别拿别人的分析）

```sql
SELECT s."startTime", COALESCE(k."key_alias",'?') who, s."model_group",
       COALESCE(s."model_id",'') deployment
FROM "LiteLLM_SpendLogs" s
LEFT JOIN "LiteLLM_VerificationToken" k ON k."token"=s."api_key"
WHERE s."startTime" > NOW() - INTERVAL '30 minutes' AND s."model" ILIKE '%deepseek%'
ORDER BY 1 DESC LIMIT 20;
```

**2. 看 counts 判断网关做了什么**（这是最快的判据）

```bash
kubectl -n litellm-product logs <pod> --since=15m | grep -o "counts={[^}]*}" | sort | uniq -c
```

| counts | 含义 |
|---|---|
| `additional_tools_hoisted: N` | 提升生效了，根因 1 已覆盖 |
| `custom_call_restored: N` | 出站还原生效了，根因 2 已覆盖 |
| `arg_event_renamed: N` | 参数事件名已同步 |
| `custom_to_function: N` | 入站降级了（正常，DeepSeek 硬约束）|
| `{}` (no-op) 紧跟 400 | **网关没动手**，是客户端载荷问题 |
| `reasoning_insert: N` 紧跟 400 | **网关自己插的**，见 [[feedback_deepseek_pairs_tool_calls_by_adjacency]] |

**3. 横向对照**：同时段其它 deepseek 请求的 `tools` / `tool_choice` 长什么样，
异常那条差在哪。

**4. 单变量复现**：拿 DeepSeek key 直连 `api.deepseek.com`，只改一个变量。
key 在 `LiteLLM_ProxyModelTable.litellm_params` 里是密文，pod 内解：

```python
import os
os.environ.setdefault("LITELLM_SALT_KEY", os.environ.get("LITELLM_MASTER_KEY",""))
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper as d
print(d(value="<密文>", key="api_key"))   # key= 传字段名,不是密钥
```

## 验收清单（缺一不可）

```
□ Codex 真实形状(additional_tools + tools=[])多轮 ≥4 轮全 completed
□ 输出是 custom_tool_call/exec，不是 function_call
□ 逐 SSE 事件核对：response.completed + [DONE] 都在，事件数与修复前一致
□ 参数事件是 custom_tool_call_input.delta，不是 function_call_arguments.delta
□ 零 DSML 泄漏（检查 message 文本里有无 'DSML' / 全角 '｜' / 'invoke name='）
□ 隔离对照组：2~3 个其它模型 custom事件=0、流完整、function_call 未被改动
□ compaction：带 trigger -> output_item.done=1 且 compaction=1；
  不带 trigger -> compaction=0（普通请求零影响）
□ 单测全绿（k8s/litellm-callbacks/tests/，86+ 条）
```

**只测目标模型不算验收** —— 改动挂在全局 SSE 收口上，必须证明没串到别人。

## 相关文件

- `k8s/litellm-callbacks/deepseek_responses_adapt.py` — 入站：hoist + custom 降级 + 补 reasoning
- `k8s/litellm-callbacks/deepseek_id_prefix.py` — 出站：id 前缀 + SSE 层还原 custom
- `k8s/litellm-callbacks/tests/test_deepseek_*.py` — 4 套回归测试
- `scripts/codex-deepseek-triage.py` — 一键体检（`--rid` / `--only groups` 是 10-10 加的）
- `scripts/litellm-198-deepseek-lane-regress.py` — 换 deepseek callback 前后的 10 条车道回归（截图/文本/工具/chat/对照）
- 上线：`scripts/litellm-198-callback-update.py plan|apply <file> [--deploy litellm-proxy-gray]`
  先 gray 再 prod；它只换这一个 key、门禁 git 对账、共享 CM 只改 data 不重启 acct pod。
  🔴 10-10 起三车道有带外改动，`lane-callback-swap.sh` 会 STOP —— 换已在线的 callback 用本脚本
- 提交：`2b14561`（hoist）、`090a1d7`（custom 还原）、`f6a038f`（compaction v2）、`456b317`（id 前缀，见下）、`e291064`（v4.1 改写）

**关于 `456b317`**：它给 DeepSeek 的裸 UUID 补 `fc_`/`rs_` 前缀。当时以为是根因，
后来用真 Codex schema 直连测 5 次**零泄漏**证伪了。留着无害（格式更规范、GPT 未受
影响），但它**不是**任何一个症状的原因 —— 别把它当作参考案例。

## 相关 skill

- [litellm-hook-dev](../litellm-hook-dev/SKILL.md) — Responses 流式改写的两层坑 + pydantic 红线
- [deepseek-compat-custom-tool-fix](../deepseek-compat-custom-tool-fix/SKILL.md) — 更早的 custom tool 兼容
- [add-litellm-model](../add-litellm-model/SKILL.md) / [owui-key-swap-proxy](../owui-key-swap-proxy/SKILL.md) — 建 DeepSeek 组时要同时建 `-responses` 孪生组

## 另一条上游

zerokey（ChatGPT 网页会话）那条路的同款实现见 skill
`codex-compaction-v2-nonnative` —— 那边还多三个坑（volumeMount / contextvar 层 /
回调顺序）和一个 9 格回归脚本 `scripts/codex-compaction-regression.py`。

## 同一类病的另一条路

`additional_tools` 这个坑在 zerokey 上也踩了一遍（而且那边还多三段：
账号级契约、BPI→exec 编译、**回程**）。见 skill `codex-zerokey-agent-loop`。
