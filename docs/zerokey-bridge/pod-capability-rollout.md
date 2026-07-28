# zerokey pod 能力目录上线记录(52 pod 全量)

2026-07-27。把实测的网页版能力(见 `web-capability-inventory.md`)接进 pod,
52 个 pod 全量验证通过。

## 1. 先纠正一个我自己的错误假设

上一轮我说过"这些模型 pod 里没配置,你来完成对应的配置"。**实测证明前提是错的。**

`config/constants.js` 的 `MODELS` **不拦请求**,只喂 `GET /v1/models`(广告)。
`core/chatgpt/api.js` 把 model **原样**透传上游(`model: model || 'auto'`,无查表):

| pod 内实测 | 结果 |
|---|---|
| `gpt-5.6-sol-wm`(不在旧列表里) | **200 OK** |
| `gpt-5-6-pro`(不在旧列表里) | **200 OK** |
| `gpt-5-2`(已从上游下线) | **200 OK** |
| `bogus-model-xyz`(纯编造) | **200 OK** |

→ **它是目录(catalogue),不是白名单(allowlist)。**

所以"新模型用不了"从来不是真问题。真问题是**目录不准**:
LiteLLM 的模型发现、IDE 的模型选择器都读这个接口,
陈旧目录会**藏起真实模型、同时推荐已死模型**。

## 2. 改了什么

### 2.1 模型目录:19 个(按实测 `/backend-api/models` 对齐)

新增 8 个(旧列表里没有):
`gpt-5-6-pro` `gpt-5-6-thinking` `gpt-5.6-sol-wm` `gpt-5.6-terra-wm`
`gpt-5.6-luna-wm` `gpt-5.5-wm` `gpt-5.5-cca-wm` `gpt-5-5-mini`

删掉 8 个已从上游目录消失的:
`gpt-5-4-pro` `gpt-5-4-thinking` `gpt-5-2` `gpt-5-1` `gpt-5` `gpt-5-mini`
`gpt-4-5` `agent-mode`

> ⚠️ 上游对这些退役 slug **仍然回 200**,所以"能不能调通"发现不了它们已下线。
> 只能靠对齐 `/backend-api/models` 的清单挡住。

### ⚠️ 修正:`-wm` 变体不进目录

第一版我把上游返回的 `-wm` 变体照抄了进去,**错的**。`-wm`(with-memory)
触发 conduit `stream_handoff`:首响应只回 `resume_conversation_token` JWT,
正文从内网 conduit 异步流,无状态 replay 跟不了 → 空返。

直连 chatgpt.com 实测:

| slug | 结果 |
|---|---|
| `gpt-5.6-sol` | **17004 字节,有正文** |
| `gpt-5.6-sol-wm` | **973 字节,只有 handoff,零正文** |

**坑点:经 pod 时两种写法都能返回正文**,只测 pod 发现不了 ——
差异只在直连路径暴露。

这条规则 skill 里早就写了
(`~/.claude/skills/zerokey-web-tool-injection/SKILL.md` "绝不加 `-wm`"),
**是我没先读**。所以目录用 plain slug:`gpt-5.6-sol` / `terra` / `luna` /
`gpt-5.5` / `gpt-5.5-cca`。`test-capabilities.js` 已加断言禁止 `-wm`。

顺带在 `/v1/models` 每条加 `context_window`(410000 / 262144 / 196608 / …),
客户端可以按上下文选模型而不是猜。

### 2.2 harvest 白名单:收敛到共享常量

`HARVESTABLE_RECIPIENTS = ['container.exec', 'python']`

模型自述 ~34 个命名空间,但抓包证明**只有 5 个会触发**,其中只有这两个带
可 harvest 的 `content_type:"code"` 体。另三个是
`api_tool.call_tool` / `api_tool.list_resources`(MCP 平面,OpenAI 服务端自己执行,
没东西可 harvest)和 `web.run`(无本地执行)。

`container.download` / `feed_chars` / `open_image` **一次都没触发过** ——
连明确要求"给我下载链接"时,模型也只回一个 `sandbox:/mnt/data/x.csv` 文本链接。
**给它们加 harvest 是死代码。**

`routes/web-tools.js` 原本自己硬编码一份 `rec !== 'container.exec' && rec !== 'python'`,
现在改为 `require('../config/constants')` —— 防止两处漂移。

### 2.3 修掉一条被证伪的注释

`web-tools.js` 头部原写:

> "The ChatGPT web backend ... **never emits native tool_calls**"

**这是错的,而且误导了一段时间的设计判断。** 网页版**有**协议级工具调用 ——
走 MCP connector,表现为 `recipient: "api_tool.call_tool"` +
JSON Schema 校验过的参数。已用真实飞书 MCP server 端到端验证
(24 个工具、真实数据、单轮内链式两步)。

区别在于:MCP 要求**远程 HTTPS**,OpenAI 侧碰不到**用户本机** ——
所以"在本机跑命令"仍是 exec-harvest 的活。两者各管一半。

## 3. 上线机制(踩到一个坑)

pod 通过 ConfigMap `zk-image-patch` 挂到 `/patch`,启动脚本 `cp` 到 `/app`。

**坑:CM 里加了 key 不等于会被复制。** 启动脚本只 `cp` 它列出的文件,
新增 `constants.js` 必须**同时**:
1. 加进 CM(`--from-file=<dir>` 整目录重建,保留原有 8 个 key)
2. 在每个 deploy 的启动脚本里加 `cp /patch/constants.js /app/config/constants.js`

只做 1 会静默无效 —— 文件在 `/patch` 里躺着但 `/app` 用的还是镜像内的旧版。

**第二个坑:我把两个 CM 从 9 key 塌成 1 key。** 用"导出全部 key 到临时目录 →
加新文件 → `create cm --from-file=<目录>` 重建"时,内联 python 靠 shell 传路径失败
(`KeyError`),导出目录是空的 —— 但 `--from-file=<空目录>` **仍然 create 成功**,
整条链路无任何告警。幸好 CM 变更不触发 pod 重启,52 个 pod 仍跑已落盘的旧文件,
**无服务影响**;但若当时有 pod 重启就会回退到镜像内默认实现。

靠备份救回,且必须用 **`kubectl replace`** 而非 `apply`(备份里的
`resourceVersion` 已过期,`apply` 报 `the object has been modified`),
先删掉 `resourceVersion`/`uid`/`creationTimestamp` 再 replace。

修法:写了带三道断言的 `cmadd.py`(导出数==原key数、目录数==期望数、
`--dry-run` 生成物 key 数==期望数),任一不符立刻退出,绝不 apply。

**第三个坑:zero-87 挂的是另一个 CM。** 51 个 pod 用共享
`zk-image-patch`,**zero-87 单独用 `zk-image-patch-stream87`**(内容与共享版
逐字节等长,是个陈旧克隆)。第一次 audit 抓到它 md5 陈旧才发现。
→ **不要假设所有 pod 挂同一个 CM,按 volume 实际引用枚举。**

灰度顺序:
```
1. 单 pod 就地 cp 验证语法/加载        (不动共享 CM)
2. 共享 CM 加 constants.js + web-tools.js
3. 只给 zero-100 加 cp 行 -> rollout -> 验 md5 + 功能
4. 剩余 51 个批量加 cp 行 -> rollout
5. zero-87 的专属 CM 单独更新
6. 全量 audit(md5)+ 功能回归
```

## 4. 验证结果

| 检查 | 结果 |
|---|---|
| `constants.js` md5 正确(`b88d4634`,去 `-wm` 版)| **52/52** |
| `web-tools.js` md5 正确 | **52/52** |
| pod `/v1/models` 19 个,含 `gpt-5.6-sol` / `gpt-5-6-pro`,**0 个 `-wm`** | ✅ |
| 已退役 `gpt-5-2` 已消失 | ✅ |
| `gpt-5-6-pro` `context_window=410000` | ✅ |
| exec-harvest 仍产出 tool_call(单 pod ×3) | **3/3** |
| toolcall 跨 pod 抽样 | **8/8** |
| 端到端(经 bridge,磁盘校验) | **3/3** |
| bridge `tool_willing_pods` | **47/47** |
| 离线测试 `test-capabilities.js` | **18/18**(含"不含 -wm"断言)|
| bridge 离线套件 | **36 项全过** |

`test-capabilities.js` 已验证**能抓退化**:故意塞回退役 slug + 多余 recipient → FAIL 2 条。

## 5. 回滚

```bash
# CM 备份(上线时自动生成)
/tmp/zk-image-patch-bak-<HHMMSS>.yaml
/tmp/stream87-bak-<HHMMSS>.yaml
kubectl apply -f /tmp/zk-image-patch-bak-<HHMMSS>.yaml

# cp 行是纯增量,删掉即回到镜像内默认 constants.js:
#   kubectl patch deploy zero-N ... 去掉 "cp /patch/constants.js ..." 一行
```

因为 `MODELS` 不拦请求,**即使目录回滚成旧版,所有模型依然可用** ——
回滚只影响"广告什么",不影响能力。

## 6. 未做的事(不夸大)

- **MCP connector 没有做进 pod。** connector 是**账号级**、注册在 OpenAI 侧,
  不是 pod 的配置项。pod 侧无需改动即可用(`api_tool.call_tool` 由 OpenAI 执行)。
  47 个账号的批量注册工具是 `mcp-provision-pool.py`,**尚未全量执行**。
- **Gmail / Calendar / Contacts 那 40 个 api_tool 资源没有接**。它们绑定的是
  captured 账号自己的 Google 账号,对我们的用例(飞书/本机 shell)没有价值。
- **skills(24 项 artifact 模板)没有接** —— 实测那是 presentation/pdf/document/
  spreadsheet 的输出模板,与工具调用无关。
- `gpt-5-4-t-mini` 等 slug 的流式行为未逐一验证。已知 `gpt-5-5-pro` 内联、
  `gpt-5.6-sol-wm` 走 `stream_handoff` —— **换模型仍需重测流式路径**。
