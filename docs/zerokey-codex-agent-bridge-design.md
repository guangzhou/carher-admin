# zerokey 网页额度 × Codex 全 Agent 能力 — 设计备忘

> 状态：**规划中**（尚未实现）。**完整落地方案**见 [zerokey-codex-agent-bridge-plan.md](./zerokey-codex-agent-bridge-plan.md)（MD 审阅版 + [飞书](https://t83dfrspj4.feishu.cn/docx/C4jcdpP0DoBy55x63TDcDsoynMc)）。当前已落地路径见 [chatgpt-web-to-codex-zerokey.md](./chatgpt-web-to-codex-zerokey.md) 与 [索引](./zerokey-codex-artifacts.md)。

## 目标

在 **ChatGPT 网页 Chat 额度**（`f/conversation`）下，让 **Codex CLI** 跑完整 Agent loop（`apply_patch`、`shell`、多轮 tool），而不是仅对话/生成代码文本。

## 为什么 LiteLLM 现有 zerokey 条目不够

| 环节 | 现象 |
|------|------|
| Codex | 只发 `/v1/responses`，工具含 `apply_patch`（freeform `custom_tool_call`） |
| LiteLLM `use_chat_completions_api` | **丢弃** `apply_patch` / `shell` / `custom` 等 Responses-only 工具（[LiteLLM #28696](https://github.com/BerriAI/litellm/pull/28696)、[#29281](https://github.com/BerriAI/litellm/pull/29281)） |
| zerokey `Bearer raw` | 无 ToolCompiler；消息压平为纯文本 |
| 结果 | Codex 无法完成本地 patch 执行链 |

**假设**：LiteLLM 桥 + raw 可实现 Codex Agent。  
**证伪**：上游若保留 tools，应出现 structured tool_calls；LiteLLM 源码 drop + raw 无 parser → 假设不成立。

## 推荐架构：专用桥 + zerokey ToolCompiler

```text
Codex CLI  (wire_api=responses)
    │
    ▼
zerokey-codex-responses-bridge   ← 本地 127.0.0.1:8788 或 198 sidecar（仅自用）
    │  ① Responses input → chat messages
    │  ② 上游 Authorization: Bearer vscode（或未来 Bearer codex）
    │  ③ chat tool_calls → custom_tool_call (apply_patch V4A)
    ▼
188 zerokey :812x  /v1/chat/completions
    ▼
chatgpt.com  f/conversation  +  ToolCompiler ⟦tool¦param⟧ 语法
```

**必须绕开 LiteLLM** 的 responses→chat 桥；198 上其它模型不受影响。

### 工具反译（桥的核心）

| zerokey ToolCompiler 输出 | Codex 期望 |
|---------------------------|------------|
| `replace_string_in_file` / `create_file` | `custom_tool_call` **`apply_patch`** + V4A `input` |
| `run_in_terminal` | `shell` / `shell_command` |
| `read_file` / `list_dir` / `grep_search` | 对应 Codex 内置 read 类 tool（若启用） |

流式必须发 `response.custom_tool_call_input.delta`，不能降级为 `function_call` + `{}`（[9router #1371](https://github.com/decolua/9router/issues/1371)）。

### zerokey 侧建议改动

新增 **`Bearer codex` 模式**（不要进 `RAW_IDES`）：

- 启用 ToolCompiler（工具语法注入 + SSE 解析）
- **无状态**：每轮 `chatSessionId=null`，全量 history（类似 raw，但有工具）
- 可选：独立 `codex` IDE profile，弱化 VS Code workspace XML

## 可 fork 的社区桥（仅协议层，upstream 换成 zerokey）

| 项目 | 说明 |
|------|------|
| [deepseek-responses-proxy](https://github.com/holo-q/deepseek-responses-proxy) | 明确处理 apply_patch freeform；Python |
| [api2codex](https://github.com/talkcozy/api2codex) | 单文件 FastAPI 桥 |
| [va-ai-api-bridge](https://github.com/jazzenchen/va-ai-api-bridge) | Rust universal 模型（Codex #7782 讨论引用） |
| [codex-convert-proxy](https://github.com/soddygo/codex-convert-proxy) | Rust SSE 双向 |

## 替代架构（非 zerokey model provider）

若接受 **Codex + MCP 外脑**，社区成品更省事：

| 方案 | 机制 | 与 zerokey 关系 |
|------|------|-----------------|
| [gpt2agent](https://github.com/robotlearning123/gpt2agent) | MCP → 网页 `agent-mode` | 并行，不替换 zerokey API |
| [webgpt2mcp](https://github.com/maoulee/webgpt2mcp) + WebAI2API | 浏览器 → MCP | 更重，可替代 capture 栈 |

## 实施顺序（PoC）

1. 188 上 curl 验证 `Bearer vscode` 多轮 `tool_calls` SSE 形态  
2. Mac 本地 fork `deepseek-responses-proxy` → upstream `http://10.68.13.188:8123` + `Bearer vscode`  
3. 实现 `replace_string_in_file` → V4A `apply_patch` 反译  
4. `codex -p zerokey-agent` 单文件创建回归  
5. zerokey 合入 `Bearer codex` 无状态 profile  
6. 稳定后再考虑 198 sidecar（独立 NodePort，**不**进 LiteLLM zerokey 模型条目）

## 风险

- 网页模型 + ⟦语法⟧ 非 Codex 训练分布，复杂 repo Agent 成功率低于官方 Responses 后端  
- OpenAI 改 web 协议 → capture + ToolCompiler 同时受影响  
- 多 hunk / delete_file 的 V4A 反译需完整测试矩阵  
