# ChatGPT 网页版能力全清单(实测)

acct87 直连 `chatgpt.com`,2026-07-27。**区分「模型自述可用」与「实测真的会触发」** ——
只按前者做设计会踩空,本 session 已踩过。

底层机制见 `mcp-connector-native-toolcall.md`,落地案例见 `lark-mcp-connector-deployed.md`。

---

## 1. 模型:19 个

`GET /backend-api/models`(需 `x-openai-target-path`)

| slug | max_tokens | 名称 |
|---|---|---|
| `gpt-5-3` | 137000 | GPT-5.3 |
| `gpt-5-3-instant` | 137000 | GPT-5.3 Instant |
| `gpt-5-5` | 137000 | GPT-5.5 |
| `gpt-5-5-instant` | 137000 | GPT-5.5 Instant |
| `gpt-5-5-thinking` | 410000 | GPT-5.5 Thinking |
| `gpt-5-6-thinking` | 262144 | GPT-5.6 Thinking |
| `gpt-5.5-wm` | 262144 | GPT-5.5 |
| `gpt-5.5-cca-wm` | 262144 | GPT-5.5 (CCA) |
| `gpt-5.6-sol-wm` | 262144 | GPT-5.6 Sol |
| `gpt-5.6-terra-wm` | 262144 | GPT-5.6 Terra |
| `gpt-5.6-luna-wm` | 262144 | GPT-5.6 Luna |
| `gpt-5-5-pro` | 410000 | GPT-5.5 Pro |
| `gpt-5-6-pro` | 410000 | GPT-5.6 Pro |
| `gpt-5-3-mini` | 128000 | GPT-5.3 Mini |
| `gpt-5-5-mini` | 137000 | GPT-5.5 Mini |
| `gpt-5-4-t-mini` | 262144 | GPT-5.4 Thinking Mini |
| `o3` | 196608 | o3 |
| `o3-pro` | 196608 | o3-pro |
| `research` | 34815 | Deep Research |

**上下文最大 410000**:`gpt-5-6-pro` / `gpt-5-5-pro` / `gpt-5-5-thinking`。

### ⚠️ 两个坑

1. **`capabilities` 和 `product_features.tools` 全为空** —— 工具能力**不在**这个端点里,
   别指望从 `/models` 读出谁支持工具。`/models/config?slug=` 我们的调法实测 404。
2. **流式行为按模型不同**:`gpt-5-5-pro` SSE **内联返回**;
   `gpt-5.6-sol-wm` 返回 **`stream_handoff` + `resume_sse_endpoint`**,
   不跟进 handoff 只能拿到 973 字节空壳。**换模型必须重测流式路径。**

---

## 2. 工具命名空间:模型自述 33 个

问模型「列出你能寻址的全部 recipient」,它给出:

```
api_tool
bio.update
container.exec
container.feed_chars
container.open_image
container.download
gcal.create_event
gcal.delete_event
gcal.get_colors
gcal.read_event
gcal.respond_event
gcal.search_events
gcal.update_event
gcontacts.search_contacts
gmail.archive_emails
gmail.apply_labels_to_emails
gmail.batch_modify_email
gmail.batch_read_email
gmail.create_draft
gmail.create_label
gmail.delete_emails
gmail.forward_emails
gmail.list_drafts
gmail.list_labels
gmail.read_attachment
gmail.read_email_thread
gmail.search_email_ids
gmail.search_emails
gmail.send_draft
gmail.send_email
gmail.update_draft
python.exec
python_user_visible.exec
```

## 3. ⭐ 实测真正会触发的只有 5 个

把本 session 全部 SSE 抓包聚合(`grep '"recipient"'`):

| recipient | 出现次数 | 用途 |
|---|---|---|
| `all` | 181 | 普通文本,非工具 |
| `api_tool.list_resources` | 14 | 读服务端注册表 |
| **`api_tool.call_tool`** | **7** | **调 MCP connector — 我们的主通路** |
| `web.run` | 2 | 联网搜索 |
| `python` | 2 | 代码解释器 |
| `container.exec` | 1 | shell(exec-harvest 用这个)|

**`container.feed_chars` / `container.download` / `container.open_image` 一次都没触发过。**
明确要求「给我下载链接」时,模型只回一个 `sandbox:/mnt/data/x.csv` 文本链接,不发 `container.download`。

→ **结论:pod 侧 harvest 只需覆盖 `container.exec` + `python`(现状已覆盖),
补其他 `container.*` 是无效工作量。**

---

## 4. api_tool 的资源注册表:40 个

`api_tool.list_resources` 查根路径,返回的是**服务端已注册**的资源:

| 分组 | 数量 | 工具 |
|---|---|---|
| **Gmail** | 21 | `send_email` `read_email` `search_emails` `create_draft` `send_draft` `update_draft` `list_drafts` `forward_emails` `delete_emails` `archive_emails` `apply_labels_to_emails` `bulk_label_matching_emails` `create_label` `list_labels` `batch_modify_email` `batch_read_email` `batch_read_email_threads` `read_email_thread` `read_attachment` `search_email_ids` `get_profile` |
| **Google Calendar** | 12 | `create_event` `read_event` `update_event` `delete_event` `respond_event` `search_events` `batch_read_event` `get_availability` `get_colors` `fetch` `search` `get_profile` |
| **Google Contacts** | 3 | `search_contacts` `read_contact` `get_profile` |
| **Plugin_Management** | 4 | `get_app_permissions` `get_plugin_dependencies` `uninstall_app` `update_app_permissions` |

### ⚠️ Plugin_Management 不能装东西

模型自述这 4 个能力的作用,并明确回答**没有任何能力能从 URL/OpenAPI 注册插件**:
查权限 / 查依赖(明确不安装)/ 卸载 / 改权限。**对话层没有安装入口。**

---

## 5. skills:`skills://plugins/` 实测是 artifact 模板

我原以为这是插件/skill 注册表 —— **不是**。实测返回 24 项:

**4 种输出格式**:`presentation` `pdf` `document` `spreadsheet`

**20 个 artifact 模板**:analytics-dashboard、business-review、design-report、
experiment-analysis、financial-budget、investment-committee-memo、legal-memorandum、
market-trends-report、minimal-letterhead、operating-calendar、operating-review、
project-kickoff、project-tracker、sales-pipeline、simple-dark-mode、simple-light-mode、
strategy-memorandum、system-design、team-alignment、three-statement-forecast

**与工具注册无关。**

---

## 6. ⭐ 唯一可扩展通路:MCP connector

在会话里凭空声明自定义工具会被拒绝(试过 `lark_docs_fetch`,模型明确说
「available api_tool capabilities in this chat do not include ...」)。
**扩展只有一条路:注册 MCP connector**(HTTP API 层,不是对话层)。

```
PATCH /backend-api/settings/account_user_setting?feature=developer_mode&value=true
POST  /backend-api/aip/connectors/mcp          # 注册
GET   /backend-api/aip/connectors/{id}/actions # OpenAI 抓到的真实 JSON Schema
POST  /backend-api/aip/connectors/links/noauth # ← 必做,漏了模型看不到工具
```

已落地:**飞书 24 个工具**已通,返回真实数据,链式两步调用成功。
工具:`scripts/chatgpt-onboard/zerokey-codex/bridge/mcp-connector-cli.js`

**限制**:MCP 要求远程 HTTPS(不支持本地 stdio)→ 「在用户本机跑 shell」
仍只能 exec-harvest。connector 是**账号级**,47 个账号需各自注册。

`custom_headers` 被服务端 gate(`403 Custom MCP headers are not enabled`,
非用户可开)→ 不能用密钥头鉴权。

---

## 7. 账号能力:plan=pro,66 个 feature flag

| 分组 | flags |
|---|---|
| **模型** | `gpt5` `gpt5_mini` `gpt5_pro` `gpt4_1` `gpt4_1_mini` `gpt_4_5` `o1_launch` `o3` `o3-mini` `o3_pro` `o4_mini` `model_switcher` `model_ab_use_v2` |
| **工具/执行** | `code_interpreter_available` `browsing_available` `search_tool` `image_gen_tool_enabled` `dalle_3` `voice_mode_tool_enabled` `canvas` `canvas_code_execution` `canvas_code_network_access` `canvas_o1` `canvas_opt_in` `chart_serialization` `d3_controls` `d3_editor` `d3_editor_gpts` |
| **插件/connector** | `plugins_available` `new_plugin_oauth_endpoint` `bizmo_settings` `gizmo_canvas_toggle` `gizmo_reviews` `gizmo_support_emails` |
| **语音/多模态** | `voice_advanced_ga` `voice_file_upload` `voice_image_upload_wingman` `voice_text_upload` `voice_text_upload_wingman` `video_screen_sharing` `share_multimodal_links` |
| **账号/安全** | `mfa` `sentinel_enabled_for_subscription` `workspace_ip_allowlist` `chatgpt_ios_attest` `no_auth_training_enabled_by_default` `privacy_policy_nov_2023` `spend-controls-migrated-to-groups` |
| 其他/内部代号 | `aura_available` `beta_features` `breeze_available` `cancellation_promotion` `caterpillar` `chat_preferences_available` `codex_sidebar_promotion_eligible` `golden_hour` `graphite` `mercury` `moonshine` `shareable_links` `snc` `starter_prompts` `sunshine_available` `user_settings_announcements` `wham` `writing_blocks_document_mode` |

**与我们相关的三个**:`plugins_available`、`new_plugin_oauth_endpoint`(MCP connector 前置)、
`code_interpreter_available`(exec-harvest 依赖它)。

首页 HTML 里还能读到 `enabledConnectors` 含 **`mcp_connector`**,以及两个 workspace 权限
`chatgpt.workspace.connector.mcp.create` / `chatgpt.workspace.connector.dev_mode`。

---

## 8. 怎么复现这份清单

```bash
cd scripts/chatgpt-onboard/zerokey-codex/bridge
# 路由不要猜(猜了 30+ 个全 404),从前端 bundle 提取:
node discover-chatgpt-routes.js --session sess.json --grep connector --enum DeveloperMode
# MCP connector 端到端自检(自动清理探针)
node mcp-connector-cli.js --session sess.json probe
```

模型/工具清单:问模型「List EVERY tool namespace available to you right now
(the exact recipient names you can address)」—— 但**必须再用抓包验证哪些真触发**。
