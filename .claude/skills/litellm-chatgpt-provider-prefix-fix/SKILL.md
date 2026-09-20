---
name: litellm-chatgpt-provider-prefix-fix
description: >-
  诊断和修复 litellm-proxy 反复重启（liveness kill，exit 137，约每 10 分钟一次）的问题。
  根因签名：proxy previous log 末尾出现裸四行 "Sign in with ChatGPT using device code"
  后日志静默 3 分钟直到 SIGTERM。原因：DB 注册的 model 条目误用 chatgpt/ 原生 provider
  前缀（正确应为 openai/ + api_base 指向 acct svc），导致 proxy 进程内跑
  litellm/llms/chatgpt/authenticator.py 的 device-code 登录，同步 httpx + time.sleep
  轮询卡死 asyncio 事件循环 → liveness (6×30s) 超时 → kubelet 杀 pod。
  适用场景：198/aliyun litellm-proxy 周期性重启、liveness/readiness context deadline
  exceeded、日志出现 device code 提示、新 chatgpt acct 上线后 proxy 不稳。
metadata:
  requires:
    bins: ["kubectl", "ssh", "sshpass", "curl", "python3"]
  related_skills:
    - litellm-pro-ops
    - chatgpt-pool-on-198
    - chatgpt-pool-model-variant
    - litellm-sse-bare-response-fix
  related_memories:
    - feedback_litellm_chatgpt_provider_prefix_blocks_proxy
    - feedback_litellm_model_new_must_pass_id
    - feedback_litellm_model_new_no_api_key_field
    - feedback_litellm_model_mode_responses_patch
    - project_198_full_topology
---

# LiteLLM chatgpt/ Provider 前缀误注册 → Proxy 卡死重启修复

## 故障签名（先对签名再动手）

1. `kubectl -n litellm-product get pods -l app=litellm-proxy`：RESTARTS 持续增长，约每 10 分钟 +1
2. `lastState`：exit code **137**、reason **Error**（不是 OOMKilled）
3. events：大量 liveness/readiness `context deadline exceeded`
4. **决定性证据**：`kubectl logs <pod> --previous | tail` 末尾是裸四行：
   ```
   Sign in with ChatGPT using device code:
   1) Visit https://auth.openai.com/codex/device
   2) Enter code: XXXX-XXXXX
   Device codes are a common phishing target. Never share this code.
   ```
   之后无任何日志直到 SIGTERM。打印源是 `litellm/llms/chatgpt/authenticator.py`
   `_login_device_code()`，后续 `_poll_for_authorization_code()` 是同步
   httpx + time.sleep 轮询，卡死整个 worker 事件循环。

## 根因定位

**关键：DB 的 `litellm_params` 是加密存储的，psql `LIKE 'chatgpt/%'` 永远查不出来
（0 rows 是假阴性），必须走 `/model/info` API 拿解密后的值。**

```bash
# 在 198 上（sudo 后）：
MK=$(kubectl -n litellm-product get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
python3 scripts/scan_bad_models.py   # 见本 skill scripts/，需 MK 环境变量
```

坏条目形态：`litellm_params.model = chatgpt/gpt-5.6-sol`（等）。
正确形态（对照组）：`openai/chatgpt-gpt-5.6-sol` + `api_base=http://chatgpt-acct-N.litellm-product.svc.cluster.local:4000`。

注意 `/model/info` 会按 model_group_alias 展开条目（同一 id 出现两次，一次组名
`chatgpt-gpt-5.6-sol` 一次裸名 `gpt-5.6-sol`），**按唯一 id 数**统计真实行数。

## 修复步骤

### 1. 先验证目标 acct pod auth 可用（否则重注册没意义）

```bash
PK=$(kubectl -n litellm-product exec deploy/chatgpt-acct-62 -- printenv LITELLM_MASTER_KEY)
# 直连 acct svc IP，stream 默认；input 必须是 list
curl -s -m 90 -X POST http://<acct-svc-ip>:4000/v1/responses \
  -H "Authorization: Bearer $PK" -H "Content-Type: application/json" \
  -d '{"model":"chatgpt-gpt-5.6-sol","input":[{"role":"user","content":"say ok"}],"max_output_tokens":16}'
# 期望：SSE 流含 response.created
```

### 2. 删坏条目 + 重注册（先删后建——坏条目在主动杀 proxy，止血优先）

```bash
export MK=... PK=...
python3 scripts/fix_reregister.py 62,66,74 sol,terra,luna
```

注册体必须同时满足（缺一即踩历史坑）：
- `litellm_params.model`: `openai/chatgpt-gpt-5.6-<v>`（**不是** chatgpt/）
- `litellm_params.api_base`: svc **DNS 名**（不用 ClusterIP，svc 重建 IP 会变）
- `litellm_params.api_key`: `$PK`（acct pod 的 LITELLM_MASTER_KEY=POOL_KEY；
  漏传 → 下游 400 "No connected db"）
- `model_info.id`: `chatgpt-acct-N-gpt-5.6-<v>`（漏传 → 生成 UUID 不可运维）
- `model_info.mode`: `"responses"`（漏传 → /v1/responses 被转成 chat 格式）
- `model_name` 只用组名 `chatgpt-gpt-5.6-<v>`，**不要**同时注册裸名
  `gpt-5.6-<v>`（与 model_group_alias 冲突，alias inflation）

### 3. 验证（smoke 必须 stream:true！）

**非流式 /v1/responses 打 chatgpt 池会收到 SSE 裸流 → proxy 报 APIError →
静默 fallback 到 deepseek-v4-flash 且 HTTP 200**——用 stream:false 验证会得到
"200 但其实全 fallback" 的假阳性。看响应里的 `model` 字段识别 fallback。

```bash
python3 scripts/verify_smoke.py 36   # 36 个 stream:true 请求、每个换 user 绕 affinity 粘滞
```

然后按日志核对目标 acct 真被选中且无异常：

```bash
for p in $(kubectl -n litellm-product get pods -l app=litellm-proxy -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n litellm-product logs $p --since=10m
done | grep -oE 'weighted-pick deployment=chatgpt-acct-[0-9]+-gpt-5\.6-[a-z]+' | sort | uniq -c
```

随机路由没覆盖到的 acct：进 proxy pod 内按 router 同路径直打
（svc DNS + 注册的 api_key），HTTP 200 + response.completed 即通过。

### 4. 稳定性确认

15–30 分钟后：`get pods` RESTARTS 不再增长 + 日志无新 device-code。
注意 rollout 会换 pod 使 RESTARTS 清零，对比时先确认 RS hash 没变过。

## 防御层（已部署，勿清理）

proxy Deployment 挂了 `CHATGPT_TOKEN_DIR=/chatgpt-noauth`（CM `chatgpt-noauth`，
内容是假 auth.json：`access_token=noauth-blocker-do-not-use, expires_at=4102444800`）。
作用：将来再误注册 chatgpt/ 条目时，authenticator 拿到"永不过期"假 token
直接 fail-fast（上游 401），不会进 device-code 阻塞轮询。**这是故意的，别删。**

## 追加事故：Claude Messages web_search → ChatGPT Responses 形状不兼容

### 适用签名

`claude-code-*` / Anthropic `/v1/messages` key 通过 per-key alias 或
`model_group_alias` 落到 `chatgpt-gpt-5.6-*` 后，SpendLogs / ErrorLogs 里出现：

1. `ChatgptException - {"detail":"Unsupported tool type: web_search_preview"}`
2. 修完 1 后又出现
   `Tool choice 'function' not found in 'tools' parameter.`

2026-08-11 案例：`claude-code-liuguoxian03` 的
`anthropic.claude-haiku-4-5` / `claude-gpt-5.6-luna` → `chatgpt-gpt-5.6-luna`。

### 三段式归因（以后照这个证据链，不许只 grep 源码下结论）

1. **假设 A：Anthropic transformer 不认识 `web_search_preview`，导致第一类错误。**
   - 证伪条件：失败 pod 上 `_map_tool_helper()` 已能把 `web_search_preview` / `web_search`
     映射成 Anthropic hosted tool，错误仍来自 ChatGPT backend。
   - 数据路径：进失败的 `chatgpt-acct-*` pod，读
     `/app/litellm/llms/anthropic/chat/transformation.py`，并本地调用
     `_map_tool_helper({"type":"web_search_preview"})`；同时看 inner pod 日志里的
     `https://chatgpt.com/backend-api/codex/responses` 400。

2. **假设 B：ChatGPT Codex backend 支持 web search，但不接受 OpenAI Responses
   的 `web_search_preview` 字段名。**
   - 证伪条件：直打 inner `/v1/responses`，`tools:[{"type":"web_search_preview"}]`
     和 `tools:[{"type":"web_search"}]` 都 200，或都 400。
   - 数据路径：用 acct pod 的 `LITELLM_MASTER_KEY` 直打 svc `/v1/responses`。
     实测：`web_search_preview` 400，`web_search` 200。

3. **假设 C：强制 web search 的 `tool_choice` 还会被桥接成 function，导致第二类错误。**
   - 证伪条件：直打 inner `/v1/responses`，
     `tool_choice:{"type":"function","name":"web_search"}` 能 200。
   - 数据路径：读
     `/app/litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py`：
     `translate_tool_choice_to_responses_api()` 把 Anthropic
     `{"type":"tool","name":"web_search"}` 翻成
     `{"type":"function","name":"web_search"}`；再直打 ChatGPT backend 形状矩阵。
     实测：`function/name=web_search` 400，`{"type":"web_search"}` 200。

### 修复边界

**不要 patch `litellm-proxy` 的 Anthropic adapter 来输出 `web_search`。**

原因：`web_search_preview` 是 LiteLLM/OpenAI Responses 层通用形状，别的 provider
可能正需要它。正确边界是在 `chatgpt-acct-*` 的 ChatGPT provider 出口层，只在发往
ChatGPT Codex backend 前归一化：

- `/app/litellm/llms/chatgpt/responses/transformation.py`
- `/app/.venv/lib/python3.13/site-packages/litellm/llms/chatgpt/responses/transformation.py`

在 `ChatGPTResponsesAPIConfig.transform_responses_api_request()` 里
`request["stream"] = True` 后插入：

```python
# carher_web_search_tool_choice_patch
tools = request.get("tools")
if isinstance(tools, list):
    rewritten_tools = []
    changed_tools = False
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "web_search_preview":
            new_tool = dict(tool)
            new_tool["type"] = "web_search"
            rewritten_tools.append(new_tool)
            changed_tools = True
        else:
            rewritten_tools.append(tool)
    if changed_tools:
        request["tools"] = rewritten_tools
tool_choice = request.get("tool_choice")
if isinstance(tool_choice, dict):
    tc_type = tool_choice.get("type")
    tc_name = tool_choice.get("name") or (tool_choice.get("function") or {}).get("name")
    if tc_type == "web_search_preview" or (tc_type == "function" and tc_name == "web_search"):
        request["tool_choice"] = {"type": "web_search"}
```

脚本化入口：

```bash
# 在 198 / AIYJY-litellm 上跑，默认 dry-run
python3 .claude/skills/litellm-chatgpt-provider-prefix-fix/scripts/patch_chatgpt_web_search.py --dry-run
python3 .claude/skills/litellm-chatgpt-provider-prefix-fix/scripts/patch_chatgpt_web_search.py --apply
python3 .claude/skills/litellm-chatgpt-provider-prefix-fix/scripts/patch_chatgpt_web_search.py --verify-only --acct 109
```

### 端到端验证（必须做，不接受只看 HTTP 200）

1. **inner acct 形状矩阵**：任选一个已 rollout 的 `chatgpt-acct-*` pod，通过
   `/v1/responses` 验证三种都 200：
   - `tools=[{"type":"web_search_preview"}]` +
     `tool_choice={"type":"function","name":"web_search"}`
   - `tools=[{"type":"web_search"}]` +
     `tool_choice={"type":"function","name":"web_search"}`
   - `tools=[{"type":"web_search_preview"}]` +
     `tool_choice={"type":"web_search_preview"}`

2. **outer Anthropic `/v1/messages` E2E**：从 `litellm-proxy` 打：

   ```json
   {
     "model": "claude-gpt-5.6-luna",
     "max_tokens": 128,
     "messages": [{"role":"user","content":"Use web search if available. Return OK only."}],
     "tools": [{"type":"web_search_20250305","name":"web_search"}],
     "tool_choice": {"type":"tool","name":"web_search"}
   }
   ```

   期望：HTTP 200，Anthropic content 有文本；不能只看 `/v1/models`，它不反映健康。

### 回滚

把 `chatgpt-acct-*` deployment template 的 postStart patch 去掉并 rollout，或用上一版
Deployment manifest 恢复。回滚后应预期上述两个错误会重新出现；所以只有发现
`web_search` 正常流量被误改坏时才回滚。

## 陷阱清单

1. **psql 查 model 是假阴性** — litellm_params 加密存储，必须 /model/info。
2. **stream:false smoke 是假阳性** — 会静默 fallback，见上。
3. **多层 ssh/sudo/sh 嵌套里 `\x27` 不展开** — 容器内 grep 静默搜错模式；
   复杂脚本一律 `base64 | tr -d '\n'` 管道传输后执行。
4. **busybox grep 无 `--include`** — alpine 容器里带 --include 的 grep 静默失败。
5. **device-code 字样可能是转发内容** — SpendLogs 报错 payload / 用户对话里
   也会带这个词；真触发是独立裸四行 + 之后日志静默。
6. **acct pod 自己日志里的 device-code** 不影响 proxy（它们进程内自己阻塞），
   但说明该 acct auth 失效，应从路由摘除或重新灌 auth。
7. **Claude Messages web_search 有两层不兼容** — 先修 `tools` 后还要修
   `tool_choice`；只修 `web_search_preview→web_search` 会把错误从 unsupported tool
   推进到 `Tool choice 'function' not found`。

## 时间线（实例）

| 时间 (2026-07-14 UTC) | 事件 |
|------|------|
| ~14:49 | acct 扩容变更落地，18 行(9 唯一 id) chatgpt/ 条目入 DB |
| 14:49–17:35 | 4 个 proxy pod 每 ~10 分钟被 liveness 杀一次（各 14–15 次） |
| 17:35 | 同事并行缓解：挂 chatgpt-noauth 假 auth CM（fail-fast） |
| 17:49 | 删 9 条坏条目 + openai/ 重注册（根因移除） |
| 17:50+ | 流式流量 200 OK；40+ 分钟 0 重启 0 device-code |
