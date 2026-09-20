---
name: deepseek-compat-custom-tool-fix
description: >-
  维护 DeepSeek-V4 compat_proxy（36.151.241.10:8000）的两个已知 vLLM 兼容坑：
  (1) Responses API `/v1/responses` 报 "'ResponseCustomToolCall' object has no attribute 'get'"
  HTTP 500 —— vLLM(0.23.0，官方 main 至今未修)不支持 OpenAI custom(freeform) tool 的历史
  input item，兜底 return item 漏出 pydantic 对象污染 message 列表。修复：把 custom
  全家桶转成标准 function 格式。
  (2) Chat Completions `/v1/chat/completions` 报 "body.tools[0].function: Field required"
  HTTP 500 —— compat_proxy 的 _flatten_tools 对 chat path 误伤：chat 端点要 nested
  `{"type":"function","function":{...}}`，被无条件 flatten 后 vLLM 拒收。修复：`_rewrite_json_body`
  按 `path.endswith("/responses")` 门控，所有改写只对 Responses 路径生效，chat 路径透传。
  适用场景：Cursor/Codex 等带 custom tool（exec 等 freeform）走 deepseek fallback 时 500、
  阿里云 carher `custom_openai/deepseek-v4-flash`（chat 模式）fallback 时报 "Field required"、
  litellm 日志出现 ResponseCustomToolCall/Field required、主路径 chatgpt 429 后 fallback 链断裂。
metadata:
  requires:
    bins: ["ssh", "python3", "systemctl", "kubectl", "sshpass"]
  related_skills:
    - litellm-sse-bare-response-fix
    - litellm-chatgpt-provider-prefix-fix
    - add-litellm-model
  related_memories:
    - project_deepseek_compat_custom_tool_fix_2026_07_17
    - project_deepseek_compat_chat_path_flatten_fix_2026_07_20
    - feedback_zerokey_no_responses_api_support
    - feedback_litellm_no_tool_call_routing_rule
    - feedback_patch_fail_2_stop_verify
---

# DeepSeek compat_proxy custom tool 转换修复

## 故障签名（先对签名再动手）

compat_proxy 目前有**两种**已知崩溃签名，先对签名再动手：

### 签名 A：Responses API custom_tool_call 崩 `.get()`（2026-07-17 已修）

1. 用户报告 Cursor/Codex 请求 500，或 litellm fallback 到 deepseek 后失败。
2. litellm-proxy 日志 / 响应体出现：
   ```
   OpenAIException - {"error":{"message":"'ResponseCustomToolCall' object has no attribute 'get'",
   "type":"InternalServerError","param":null,"code":500}}
   ```
3. 该错误**被 litellm 包装转发**——真正 500 来自下游
   `http://36.151.241.10:8000/v1/responses`（compat_proxy → vLLM）。
4. 触发前提：请求 input 里含 `custom_tool_call` / `custom_tool_call_output`
   历史项（即模型之前调用过 custom/freeform 工具，多轮对话回传历史）。
   **单纯 tools 里有 type:custom 不会崩**（vLLM 接受 custom tool 定义），
   崩的是 input 里的历史 custom tool call 项。

### 签名 B：Chat Completions nested tools 被 flatten（2026-07-20 已修）

1. litellm 响应体（`num_retries=2` 后）出现：
   ```
   Custom_openaiException - N validation errors:
     {'type':'missing','loc':('body','tools',0,'function'),'msg':'Field required',
      'input':{'type':'function','name':...}}
   ```
2. 触发前提：litellm 侧把 deepseek 注册为 `custom_openai/deepseek-v4-flash`（**chat 模式**，
   非 responses），主路径 429 走 fallback 时命中 → POST 到
   `http://36.151.241.10:8000/v1/chat/completions`。
3. 根因不在 vLLM，而在 compat_proxy 的 `_flatten_tools`：早期为 Responses 写，未做 path
   门控，chat 请求 nested tools（`{"type":"function","function":{...}}`）被打成 flat
   → vLLM chat 端点拒收。
4. 与签名 A 无关：不涉及 custom tool，纯 function tool 的 chat 请求就会崩。

**签名判定要点**：出现 `Field required` + `('body','tools',0,'function')` = 签名 B；
出现 `'ResponseCustomToolCall' object has no attribute 'get'` = 签名 A。都指向
compat_proxy，别在 litellm 层找。

## 根因定位

后端拓扑：`litellm-proxy(198)` → `openai/deepseek-v4-flash` + `mode:responses`
→ `api_base: http://36.151.241.10:8000/v1`（compat_proxy）→ vLLM `127.0.0.1:8001`。

崩点在 vLLM：`vllm/entrypoints/openai/responses/utils.py`
`_construct_message_from_response_item()`。它按 isinstance 链处理 input item：
`ResponseFunctionToolCall` / `ResponseReasoningItem` / `ResponseOutputMessage` /
`ResponseFunctionToolCallOutputItem` / dict(function_call_output)，
**没有 custom_tool_call 分支**，结尾 `return item` 兜底原样返回。

当 item 是 `ResponseCustomToolCall`（openai SDK 把 `type:custom_tool_call`
校验成的 pydantic 对象）时，走 `return item` 把 pydantic 对象塞进 messages 列表。
下一轮迭代 `prev_msg.get("role")` 在 pydantic 对象上调 `.get()` →
`AttributeError: 'ResponseCustomToolCall' object has no attribute 'get'`。

### 关键结论：升级 vLLM 解决不了

已核对 vLLM 官方 `main` 分支源码（GitHub），最新版 `_construct_message_from_response_item`
仅比 0.23.0 多一个 `dict role==assistant` 分支，**仍无 custom_tool_call 处理，
仍是 return item 兜底**。且新版 `construct_tool_dicts` 改用
`iter_response_function_tool_dicts()` **只保留 function 类型 tool**——
官方自己也把 custom tool 当 function 处理，反向印证转换方向正确。

### 定位方法（避免在 litellm 层空转）

litellm 侧 traceback 会把错误层层包装，关键要看到这一行：
```
MaskedHTTPStatusError: Server error '500 Internal Server Error'
for url 'http://36.151.241.10:8000/v1/responses'
```
说明错误来自下游而非 litellm。进程内复现拿真实 traceback：
```python
# 在 litellm-proxy pod 内
import litellm, asyncio, traceback
litellm._turn_on_debug()
inp=[{"role":"user","content":"x"},
     {"type":"custom_tool_call","call_id":"c1","name":"exec","input":"y"},
     {"type":"custom_tool_call_output","call_id":"c1","output":"z"}]
async def m():
    try:
        await litellm.aresponses(model="openai/deepseek-v4-flash",input=inp,
            tools=[{"type":"custom","name":"exec"}],
            api_base="http://36.151.241.10:8000/v1",api_key="x",stream=False)
    except Exception: traceback.print_exc()
asyncio.run(m())
```
拿到 vLLM 侧完整栈：在 GPU 机（local-gpu）上
`journalctl -u deepseek-v4-flash --no-pager -n 120 | grep 'File\|line\|Error'`。
MEMORY 教训：错误可能被包装 N 层，别停在 litellm，一路追到真正 raise 点。

## 修复方案

compat_proxy 的两个核心不变量（**任何后续改动都必须保持**）：

1. **path-aware rewrite gating**：所有 payload 改写只在 `/v1/responses` 上生效。
   两条端点契约完全不同：

   | 端点 | tools 形状 | 会话状态 | compat_proxy 需要做的 |
   |------|-----------|----------|--------------------|
   | `/v1/chat/completions` | nested `{"type":"function","function":{"name":...}}` | `messages[]` | **零改写**，透传 |
   | `/v1/responses` | flat `{"type":"function","name":...}` | `input[]` | flatten + custom→function + extract additional_tools |

   实现锚点：`_rewrite_json_body(body, content_type, path)` 内
   `is_responses = path.endswith("/responses")`，三个 rewrite 全包在 `if is_responses:` 里。
   历史上 07-20 就是因为 `_flatten_tools` 无条件跑 → chat 请求 nested tools 被打成 flat →
   vLLM 400 `body.tools[0].function: Field required`。

2. **custom tool → function 映射**（Responses API only）。**每条映射都在真实 vLLM(8001) 上实测过：**

   | 位置 | 原（custom） | 转（function） |
   |------|-------------|---------------|
   | tools[] | `{"type":"custom","name":N,"description":D}` | `{"type":"function","name":N,"description":D,"parameters":{"type":"object","properties":{"input":{"type":"string"}},"required":["input"]}}` |
   | input item | `{"type":"custom_tool_call","call_id":C,"name":N,"input":TEXT}` | `{"type":"function_call","call_id":C,"name":N,"arguments":json.dumps({"input":TEXT})}` |
   | input item | `{"type":"custom_tool_call_output","call_id":C,"output":O}` | `{"type":"function_call_output","call_id":C,"output":O}` |

### 为什么 input 文本要包成 {"input": TEXT}

vLLM 对 `function_call.arguments` 做 `json.loads` 且**要求结果是 JSON object**：
- `arguments="console.log(1)"`（裸文本）→ HTTP 400 "Expecting value line 1 column 1"
- `arguments='"console.log(1)"'`（JSON 字符串标量）→ 同样 400
- `arguments='{"input":"console.log(1)"}'`（JSON object）→ OK ✅
- `arguments=""`（空串）→ OK（vLLM 特例容忍）

custom tool 的 `input` 是自由文本（如 shell/JS 代码），必须包进 object 才合法。

### 代码位置与守卫

- 文件：`36.151.241.10:/home/cltx/deepseek-v4-flash/scripts/compat_proxy.py`
- 函数 `_convert_custom_tools` 用 `changed` 标志守卫，只在检测到 custom 字段时改写，
  普通 function tool / 无 tools 请求**零影响**（与既有 `_extract_additional_tools`
  同模式）。
- 调用点在 `_rewrite_json_body` 的 `if is_responses:` 分支内：
  additional_tools 提取**之后**、flatten **之前**（转出的 function tool 已是 flat 格式，
  flatten 会跳过，无冲突）。
- 消费端拓扑：canary/prod aliyun litellm-proxy + 198 pro litellm-proxy **全部共用**
  GPU 上这一个 compat_proxy(8000)，改一次覆盖全部。

## 操作步骤

**首选**：用固化的 `scripts/deploy_compat_proxy.sh`（syntax-check → 备份 → SIGKILL →
等 systemd 自恢复 → 跑 7 项回归）。

```bash
# 从 carher-admin 仓库根目录
.claude/skills/deepseek-compat-custom-tool-fix/scripts/deploy_compat_proxy.sh \
    /tmp/compat_proxy_new.py
# 结束时应打印 "DEPLOY OK"
```

**手动流程**（sudo 拿不到时的通用套路，`Restart=on-failure` 兜底）：

```bash
JMS=./scripts/jms
TGT=/home/cltx/deepseek-v4-flash/scripts/compat_proxy.py

# 1. 上传 + 远端 syntax check
cat /tmp/compat_proxy_new.py | $JMS ssh local-gpu -- \
  "cat > /tmp/compat_proxy_new.py && \
   python3 -c 'import ast;ast.parse(open(\"/tmp/compat_proxy_new.py\").read());print(\"OK\")'"

# 2. 备份 + 覆盖 + SIGKILL uvicorn（触发 systemd Restart=on-failure，~5s 拉起）
$JMS ssh local-gpu -- "\
  cp $TGT $TGT.bak-\$(date +%Y%m%d-%H%M%S) && \
  cp /tmp/compat_proxy_new.py $TGT && \
  kill -9 \$(pgrep -f 'uvicorn.*compat_proxy' | head -1)"

# 3. 健康检查（新 pid + 8000 LISTEN + /v1/models=200）
sleep 6
$JMS ssh local-gpu -- \
  "pgrep -af 'uvicorn.*compat_proxy'; \
   curl -sS -o /dev/null -w 'HTTP=%{http_code}\n' http://127.0.0.1:8000/v1/models"
```

服务单元：`deepseek-v4-flash-compat-proxy.service`（8000，proxy，`Restart=on-failure`
`RestartSec=5`）+ `deepseek-v4-flash.service`（8001，vLLM 本体，**不要动**）。
**避免用 `sudo systemctl restart`** — service 由 root 拥有，团队密钥库不总能拿到；
SIGKILL 靠 unit 自带 `Restart=on-failure` 兜底，效果等价且不需密码。

## 验证

**首选**：跑固化回归脚本，覆盖 7 项（chat×3 + responses×4）：

```bash
cat .claude/skills/deepseek-compat-custom-tool-fix/scripts/regression_compat_proxy.py | \
    ./scripts/jms ssh local-gpu -- 'cat > /tmp/rc.py && python3 /tmp/rc.py'
# 期望结尾：total=7 pass=7 fail=0
```

覆盖矩阵：

| # | 端点 | 场景 | 目的 |
|---|------|------|------|
| 1 | chat/completions | nested tools sync | 07-20 崩溃回归 |
| 2 | chat/completions | plain sync | 基线 |
| 3 | chat/completions | nested tools stream | Cursor 真实形状 |
| 4 | responses | custom_tool_call sync | 07-17 崩溃回归 |
| 5 | responses | custom_tool_call stream | Cursor 真实形状 |
| 6 | responses | plain sync | 基线 |
| 7 | responses | 标准 function tool | 无关请求零影响 |

**全链路**（可选）：

```bash
# 阿里云 carher (canary + prod 共用 compat_proxy)
$JMS ssh 226 -- "\
  MK=\$(kubectl -n carher get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d); \
  kubectl -n carher run curl-\$RANDOM --rm -i --restart=Never --image=curlimages/curl:latest -- \
    -s http://192.168.35.175:4000/v1/chat/completions \
    -H \"authorization: Bearer \$MK\" -H 'content-type: application/json' \
    -d '{\"model\":\"deepseek-v4-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\
         \"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"echo\",\"parameters\":{\"type\":\"object\"}}}],\
         \"max_tokens\":8}'"
# 期望：200 + choices[] 非空

# 生产日志确认 fallback 链通：
$JMS ssh 226 -- "kubectl -n carher logs deploy/litellm-proxy --since=20m 2>&1 | \
    grep -iE 'deepseek-v4-flash|Field required|InternalServerError.*deepseek' | tail -20"
# 期望：无新的 500 / Field required
```

## 陷阱清单

1. **别停在 litellm 层** — 错误被 exception_mapping 包装 N 层，真正 raise 在 vLLM。
   认准 `MaskedHTTPStatusError ... for url http://36.151.241.10:8000`。
2. **单纯 tools:type=custom 不崩** — 崩的是 input 里的 custom_tool_call 历史项；
   复现必须带历史项，否则误判"已修好"。
3. **arguments 必须是 JSON object** — 自由文本裸传 400；包成 {"input":TEXT}。
4. **升级 vLLM 无用** — 官方 main 至今未修（已核对源码），且新版静默丢 custom tool。
5. **改错文件白费** — 我曾误 patch litellm 的 openai responses transformation
   （CARHER_PATCH_FLATTEN_INPUT），根因不在那里，没用。改 compat_proxy 才对。
6. **stream 也要验** — Cursor/Codex 实际用 stream=true，别只测 sync。
7. **path-aware 门控必须** — `_flatten_tools` / `_convert_custom_tools` / `_extract_additional_tools`
   **只能对 `/v1/responses` 生效**。chat completions 用 nested tools
   `{"type":"function","function":{"name":...}}`，flatten 后 vLLM 拒收
   `body.tools[0].function: Field required`（2026-07-20 阿里云 carher deepseek-v4-flash
   走 custom_openai/chat 撞坑，见 [[project_deepseek_compat_chat_path_flatten_fix_2026_07_20]]）。
   `_rewrite_json_body` 已按 `path.endswith("/responses")` 门控这三个 rewrite。
8. **sudo 拿不到时用 SIGKILL+Restart** — service 配了 `Restart=on-failure`，
   `kill -9 <uvicorn-pid>` 触发非 0 退出 → systemd 5s 内自动拉起，等价于 restart。

## 时间线

| 日期 | 事件 |
|------|------|
| 2026-07-17 | fallback 到 deepseek 报 ResponseCustomToolCall 500 |
| 2026-07-17 | 一路追栈定位到 vLLM utils.py return item 兜底 |
| 2026-07-17 | 核对官方 main 分支确认最新版仍未修，排除升级方案 |
| 2026-07-17 | compat_proxy 加 _convert_custom_tools，实测映射后部署 |
| 2026-07-17 | E2E + 回归 + 全链路验证，生产日志确认 fallback 200 OK |
| 2026-07-20 | 阿里云 carher (custom_openai/chat) 报 `body.tools[0].function: Field required` — `_flatten_tools` 对 chat path 误伤。加 path-aware 门控（`is_responses = path.endswith("/responses")`），chat 路径跳过所有 rewrite；验证 4 场景通过 |
