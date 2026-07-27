# ChatGPT 网页版能力清单(实测)

acct87 直连 chatgpt.com,2026-07-27。**区分"模型自述可用"和"实测真的会触发"** ——
这两者差别很大,只按前者做设计会踩空。

## 1. 模型(19 个)

`GET /backend-api/models` 返回:

| slug | max_tokens | 备注 |
|---|---|---|
| `gpt-5-6-pro` / `gpt-5-5-pro` | **410000** | 上下文最大 |
| `gpt-5-5-thinking` | 410000 | |
| `gpt-5-6-thinking` | 262144 | |
| `gpt-5.6-sol-wm` / `terra-wm` / `luna-wm` | 262144 | 我们主用的 sol |
| `gpt-5.5-wm` / `gpt-5.5-cca-wm` | 262144 | |
| `gpt-5-4-t-mini` | 262144 | |
| `gpt-5-5` / `gpt-5-5-instant` / `gpt-5-3` / `gpt-5-3-instant` | 137000 | |
| `gpt-5-5-mini` | 137000 | |
| `gpt-5-3-mini` | 128000 | |
| `o3` / `o3-pro` | 196608 | |
| `research` | 34815 | deep research |

**`capabilities` 和 `product_features.tools` 全为空** —— 工具能力不在这个端点里,
别指望从 `/models` 读出谁支持工具。`/models/config?slug=` 实测 404
(前端走的是 `/models/config`,不带 `/backend-api` 前缀,我们的调法未命中)。

### ⚠️ 流式行为差异(踩过)

- `gpt-5-5-pro`:SSE **内联返回**,直接读到 message
- `gpt-5.6-sol-wm`:返回 **`stream_handoff`** + `resume_sse_endpoint`,
  不跟进这个 handoff 就只拿到 973 字节的空壳

**所以换模型时必须重测流式路径**,不能假设一样。本 session 的探针脚本
只处理内联,故用 `gpt-5-5-pro` 做能力探测。

## 2. 工具命名空间(模型自述 34 个)

问模型"列出你能寻址的全部 recipient",它给出:

```
api_tool                          ← MCP connector 通路(见下)
bio.update                        ← 记忆写入
container.exec                    ← 我们 exec-harvest 用的
container.feed_chars
container.open_image
container.download
python.exec
python_user_visible.exec
web.run                           ← 联网搜索
gcal.*        (7 个: create/delete/get_colors/read/respond/search/update_event)
gcontacts.search_contacts
gmail.*       (16 个: send/read/search/draft/label/archive/delete/forward...)
```

## 3. ⭐ 实测真正会触发的只有 5 个

把本 session 全部 SSE 抓包聚合(`grep '"recipient"'`):

| recipient | 出现次数 | 说明 |
|---|---|---|
| `all` | 181 | 普通文本,非工具 |
| `api_tool.list_resources` | 14 | 读服务端注册表 |
| `api_tool.call_tool` | 7 | **调 MCP connector,我们的主通路** |
| `web.run` | 2 | 联网 |
| `python` | 2 | 代码解释器 |
| `container.exec` | 1 | shell |

**`container.feed_chars` / `container.download` / `container.open_image`
一次都没触发过。** 试过明确要求"给我下载链接",模型只回一个
`sandbox:/mnt/data/report.csv` 文本链接,不发 `container.download`。

→ **结论:pod 侧 harvest 只需覆盖 `container.exec` + `python`(现状已覆盖),
补其他 `container.*` 是无效工作量。**

### `python` 的实际可用性

`python` recipient 确实会触发,且已在 pod 的 harvest 白名单里
(`web-tools.js:378` 允许 `container.exec` 和 `python`)。但注意
`execToToolCall()` 会把内容包成 `bash -lc <text>` —— **对 python 源码是错的**。
实测中 python 的代码体没有出现在我们能拼到的 SSE patch 里,所以目前
没有产生错误行为;但如果将来 python 开始返回可 harvest 的代码体,
这里会把 python 源码当 shell 跑。**已记录为待办,不是当前故障。**

## 4. `api_tool` = MCP connector(唯一可扩展的通路)

`api_tool.list_resources` 返回的是**服务端注册表**:

```
Gmail, Google_Calendar, Google_Contacts, Plugin_Management, skills://plugins/
```

- 在会话里凭空声明自定义工具 → 被拒绝
- `Plugin_Management` 的 4 个能力只能查权限/查依赖/卸载/改权限,**不能安装**
- `skills://plugins/` 实测是 **artifact 模板**(presentation/pdf/document/
  spreadsheet + 20 个 `artifact-template-*`),与工具注册无关

**扩展方式只有一条:注册 MCP connector。**
见 `mcp-connector-native-toolcall.md` 和 `lark-mcp-connector-deployed.md`。

## 5. 本机文件读写(exec-harvest,非 MCP)

MCP 要求远程 HTTPS,**碰不到用户本机**,所以本机操作只能 exec-harvest。
实测(用 `testkit/agentloop.py` 闭环,每项都独立校验磁盘、不信模型自述):

| 操作 | 结果 | 校验方式 |
|---|---|---|
| 写文件 | ✅ | 独立 `cat` 磁盘 |
| 读文件 | ✅ | 随机 token,模型不可能猜 |
| 读→算→写 | ✅ | `sum.txt == 15` |
| **真多轮文件链** | ✅ **5/5** | 必须先读 step1 才知道第二个文件名 |
| 原地改配置 | ✅ **3/3** | 磁盘校验 3 个字段 |
| 目录树探索找文件 | ✅ | 随机 needle |
| 追加写(保留原内容) | ⚠️ **6/10** | 磁盘校验行数 |

**真多轮 5/5 是关键结果** —— G2b 在本机路径上也成立。

### 追加写 6/10 的归因(未完成)

抓到一个失败样本,模型原话:

> "the execution environment returned an internal **container error (`ENOENT`)**"

`commands=0` = **模型压根没发命令**,因为上游沙箱自己报错。
这**不是**"模型改成叙述"(那是 `_looks_like_refusal()` 处理的问题)。

补测 16 次里只见 1 次 ENOENT(~6%),**所以 ENOENT 不足以解释 4/10 的失败**,
剩余失败原因未查明。**不要把 6/10 全归因于上游。**

### 两个自查出来的测量错误(教训)

1. 第一次跑追加写得 1/3 —— 是**我的测试脚本 bug**,`$i` 在单引号里没展开,
   三次写到同一目录。修正 fixture 后是 6/10。
   **测出异常低的数,先怀疑 fixture。**
2. 以为模型把 token 截断了(`...B4D` vs `...B4D8`)—— 查 `agentloop.py:134`
   发现是**我的 harness 打印限制 150 字符**。
   **报缺陷前先确认不是自己的显示截断。**

## 6. 结论:优化优先级

| 项 | 判断 |
|---|---|
| 补 `container.download` 等 harvest | ❌ 无效 —— 实测从不触发 |
| 挂更多 MCP connector | ✅ 唯一可扩展方向 |
| 换 410000 上下文模型 | ⚠️ 需先验证 `stream_handoff` 路径 |
| 查追加写剩余失败 | ✅ 唯一还在影响成功率的未知量 |
| `python` 被包成 `bash -lc` | ⚠️ 潜在缺陷,当前未触发 |
