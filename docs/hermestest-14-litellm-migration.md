# hermestest-14 LiteLLM 接入迁移记录

**日期**：2026-05-23
**机器**：JSZX-AI-03（内网 S3）
**实例**：hermestest-14（刘国现的her）
**目标**：将 Claude 等所有模型从直连 auto-link / OpenRouter 迁移到统一走 LiteLLM（`litellm.carher.net/v1`），对齐阿里云 carher-1000 的配置。

---

## 问题背景

改之前，hermestest-14 的模型路由分裂为三条：

| provider | base_url | 走 LiteLLM？ |
|---|---|---|
| chatgpt-pro（GPT） | `litellm.carher.net/v1` | ✅ |
| wangsu-litellm（Claude） | `cc.auto-link.com.cn/pro` | ❌ 直连 auto-link |
| openrouter | `openrouter.ai/api/v1` | ❌ 直连 OpenRouter |

Claude 调用完全绕过 LiteLLM，导致：
- SpendLogs 里看不到 Claude 用量
- litellmKey 对 Claude 调用无效，预算管控失效
- 两套 key 并存（`CARHER_PROD_KEY` + `ANTHROPIC_API_KEY`）

---

## 改动文件

### 文件 1：Hermes 引擎配置

**路径（宿主机）**：`/Data/carher-runtime/deploy/carher-14/data-hermes/.hermes/config.yaml`
**容器内路径**：`/opt/data/.hermes/config.yaml`

**改动内容**：将 `wangsu-litellm` provider 从直连 auto-link 改为走 LiteLLM；移除重复的 `custom_providers` 块；更新 `quick_commands` alias 指向；`terminal.env_passthrough` 去掉不再需要的 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`。

```yaml
# 改前
wangsu-litellm:
  base_url: "https://cc.auto-link.com.cn/pro"
  key_env: "ANTHROPIC_API_KEY"
  api_mode: "anthropic_messages"
  transport: "anthropic_messages"
  default_model: "anthropic.claude-opus-4-7"

# 改后
claude-litellm:
  name: "Claude via LiteLLM"
  base_url: "https://litellm.carher.net/v1"
  key_env: "CARHER_PROD_KEY"
  api_mode: "chat_completions"
  transport: "chat_completions"
  default_model: "claude-opus-4-7"
  models:
    claude-opus-4-7 / claude-opus-4-6 / claude-sonnet-4-6
```

> **注意**：`config.yaml` 是 Hermes 推理引擎的配置，不是 OpenClaw gateway 的配置。主对话入口读的是 `openclaw.runtime.json5`，所以这个文件的改动只影响 ACP agent 子进程调用路径。

---

### 文件 2：OpenClaw 运行时配置（主配置）

**路径（宿主机）**：`/Data/carher-runtime/deploy/carher-14/openclaw.runtime.json5`
**容器内路径**：`/data/.openclaw/openclaw.json`（只读挂载）

**这是真正影响机器人对话模型的文件。**

**改动内容**：

1. **新增 `carher-pro` provider 的全部 13 个模型**，`api` 统一用 `openai-completions`（GPT-5.5 用 `openai-responses`），全部指向 `litellm.carher.net/v1` + `${CARHER_PROD_KEY}`
2. **`agents.defaults.model.primary`** 保持 `carher-pro/chatgpt-gpt-5.5`
3. **`agents.defaults.models`** 对齐 carher-1000 alias 命名规范

**模型对照表（改后）**：

| alias | model id | 路由 |
|-------|----------|------|
| `gpt`（primary） | `chatgpt-gpt-5.5` | carher-pro (LiteLLM) |
| `gpt-5.4` | `chatgpt-gpt-5.4` | carher-pro (LiteLLM) |
| `codex` | `chatgpt-gpt-5.3-codex` | carher-pro (LiteLLM) |
| `opus` | `claude-opus-4-6` | carher-pro (LiteLLM) |
| `opus4.7` | `claude-opus-4-7` | carher-pro (LiteLLM) |
| `sonnet` | `claude-sonnet-4-6` | carher-pro (LiteLLM) |
| `gemini` | `gemini-3.1-pro-preview` | carher-pro (LiteLLM) |
| `glm` | `glm-5` | carher-pro (LiteLLM) |
| `minimax` | `minimax-m2.7` | carher-pro (LiteLLM) |
| `ds-pro` | `wangsu-deepseek-v4-pro` | carher-pro (LiteLLM) |
| `ds-flash` | `wangsu-deepseek-v4-flash` | carher-pro (LiteLLM) |
| `gemini35` | `wangsu-gemini-3.5-flash` | carher-pro (LiteLLM) |
| `glm51` | `wangsu-glm-5.1` | carher-pro (LiteLLM) |
| `or-gpt` | `openai/gpt-5.5` | openrouter（保留备用）|

---

## 设计说明：ACP 工具链走 auto-link 是正确的

```
▶ ACP toolchain ready (base=https://cc.auto-link.com.cn/pro, key redacted)
```

这是**预期行为**。S3 实例采用两套 key 并行的设计：

| 路径 | key | 用途 |
|------|-----|------|
| 主对话（OpenClaw gateway）| `CARHER_PROD_KEY` → LiteLLM | spend tracking、预算管控收口 |
| ACP agent 子进程 | `ANTHROPIC_API_KEY` → auto-link | Claude Code / acpx 独立配额，不经 LiteLLM 计费 |

`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` 由 `compose.cicd-{N}.yaml` 注入，保持不变。

---

## 备份文件

| 备份文件 | 说明 |
|---|---|
| `config.yaml.bak` | Hermes config 原始版本（容器内） |
| `openclaw.runtime.json5.bak-litellm-claude-20260522T*` | openclaw 主配置原始版本（宿主机） |

---

## 回滚方法

```bash
# 回滚 openclaw.runtime.json5
scripts/jms ssh JSZX-AI-03 "cp /Data/carher-runtime/deploy/carher-14/openclaw.runtime.json5.bak-litellm-20260522T152214Z \
  /Data/carher-runtime/deploy/carher-14/openclaw.runtime.json5"
scripts/jms ssh JSZX-AI-03 "docker restart hermestest-14"

# 回滚 config.yaml
scripts/jms ssh JSZX-AI-03 "docker exec hermestest-14 cp /opt/data/.hermes/config.yaml.bak /opt/data/.hermes/config.yaml"
scripts/jms ssh JSZX-AI-03 "docker restart hermestest-14"
```

---

## 参考：carher-1000 vs hermestest-14 架构对比

| 维度 | 阿里云 carher-1000 | S3 hermestest-14（改后）|
|---|---|---|
| 配置管理 | K8s CRD → Operator → ConfigMap | 宿主机文件直接挂载 |
| LiteLLM 接入 | `litellm-proxy.carher.svc:4000`（内网）| `litellm.carher.net/v1`（公网）|
| 模型数量 | 13 个 | 13 个（+1 openrouter 备用）|
| 模型 alias | 完全一致 | 完全一致 |
| ACP 路径 | 无独立 ACP env | 仍走 auto-link（待改）|
