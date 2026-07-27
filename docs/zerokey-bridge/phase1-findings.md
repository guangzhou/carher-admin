# Phase 1 上游形态实测（2026-07-02）

采样点：188:8123 (zerokey-codex 主容器) via `curl 127.0.0.1:8123`

## 关键发现

### F1. Bearer vscode = 标准 OpenAI tool_calls SSE
样例 `/tmp/zk-phase1/vscode-full.sse` (987B)：
```json
{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_0001_write","type":"function",
  "function":{"name":"create_file","arguments":"{\"filePath\":\"hello.txt\",\"content\":\"hello world\"}"}}]}}]}
```
- 非自定义 `⟦...⟧` 语法（那是 ToolCompiler 内部 shim，对外已包装成标准 chunk）
- `finish_reason: "stop"` 收尾（不是 `tool_calls`，需 bridge 端归一化）

### F2. Bearer codex = raw passthrough（不经 ToolCompiler）
`/app/routes/chatgpt.js:12`：`RAW_IDES = new Set(['raw','codex','openai','plain'])`
- Bearer codex 走 `rawComplete()`，返 ChatGPT 原生 chat 格式（无 tools 结构）
- Codex CLI 用 responses wire_api，二者根本不匹配 → 必须桥

### F3. 工具集与 PoC 映射表对齐
zerokey ToolCompiler 内建 VS Code Copilot 三件套：
- `create_file(filePath, content)`
- `replace_string_in_file(filePath, oldString, newString)`
- `run_in_terminal(command)`

← 完美映射 → Codex CLI 侧的：
- `apply_patch` (freeform custom_tool_call, Add/Update/Delete File 三形态)
- `shell` (command array)

`bridge/zerokey-codex-responses-bridge.py` 484 行的方向就是对的，无需重设计。

## 桥的真实工作量

**不是** SSE 格式转换（vscode 已经是 OpenAI 标准）
**而是** 三件事：
1. **入向 schema mapping**: Codex responses.tools[apply_patch, shell] → chat.tools[create_file, replace_string_in_file, run_in_terminal]
2. **出向 tool_calls 翻译**: vscode create_file(fp, content) → Codex apply_patch(`*** Add File: fp\n+content`)
3. **SSE 帧封装**: OpenAI chat.completion.chunk → Codex responses `response.custom_tool_call_input.delta` + `response.output_item.done`

## Phase 2 决策

**Fork 现有 PoC** (`bridge/zerokey-codex-responses-bridge.py` 484 行) 而非 fork `deepseek-responses-proxy`：
- 已有 upstream config 骨架
- 已有 3 工具映射表
- 缺：responses SSE 输出层 + apply_patch 反向拼装

Phase 2 交付：Mac 本地 :8788 桥能过 codex CLI 端到端一次 create/patch/shell 任务。**不动 198 LiteLLM**。

## 未验证但可后置

- 多轮 tool_result 回传（Codex responses input[].content[type=function_call_output]）
- CF sentinel 403 时 vscode 是否也 fail（推测同 raw 一样直挂）
- Bearer vscode 是否也吃 rpm=30 池的路由（推测 rebalancer entry 用 `api_key: "raw"`，切 vscode 要多池 entry 或桥自持一套 8123..8147 upstream 轮询）
