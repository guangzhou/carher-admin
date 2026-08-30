# zk-delta：Cursor → LiteLLM 增量传输 体系修复 · 执行计划与闭环记录

- **开工**：2026-08-31 00:18 CST
- **交付期限**：2026-08-31 早
- **作者**：Claude（自主执行，全程不问人）
- **状态标记**：本文每一节末尾标注 `【事实】`/`【推测】`/`【未验】`
- **配套飞书文档（设计评审稿）**：https://t83dfrspj4.feishu.cn/docx/CINndAwmqoDqLuxiTqGcoYVkn2S

---

## 0. 真实目标（按用户口径重述，不是我的复述）

| # | 目标 | 判定方式（可证伪） |
|---|------|-------------------|
| G1 | **长会话不再撞墙**：Cursor 聊多久都不出 400 | 同一条会话跑到 ≥15 轮，`zkreq.log` 里 UA=Cursor 的 400 计数 = 0 |
| G2 | **省带宽、降服务器负担**（目标本身，非副产品） | Cursor→198 广域网上行字节**不随轮次线性增长**；第 N 轮上行 / 第 1 轮上行 < 2 |
| G3 | **推广阻力小**：同事零额外步骤 | 装机只跑既有安装器一次，不装新运行时、不改 Cursor 本体 |
| G4 | **静默降级必须喊出声** | 每一次降级/回落都有计数器与结构化日志行；人为注入一次失效必须硬报错，不能 200 |
| G5 | **门① 干净载荷不破** | 发 `ls`，网关收到的 payload 与基准逐字节相同 |
| G6 | **门② 既有功能不回退** | shell `ls` + 飞书建文档稳定出结果 |
| G7 | **交付一个全新的、可验证、已落地验证的项目** | 有独立目录、有测试、有部署、有真流量验收记录 |

【事实】G1–G6 逐条来自用户 08-30/08-31 的原话与飞书评审稿；G7 来自 08-31 00:0x 的指令。

---

## 1. 我缺少的关键信息（不问人，只能自己实测补齐或标为假设）

| # | 缺什么 | 为什么重要 | 我的补齐动作 | 补齐前的处理 |
|---|--------|-----------|-------------|-------------|
| M1 | Cursor 发出的 `/v1/chat/completions` **原始 body 的真实形状**（键序、tools 数量、messages 结构） | 增量协议要做到"服务端重建出的 body 与原 body 逐字节相同"，必须拿到真样本 | 在网关/nginx 侧抓一次真实 body 存盘，作为 golden fixture | 不写死任何形状假设；协议设计成"整包模板 + messages 数组"，与具体字段无关 |
| M2 | 36 行 zerokey 模型的 `litellm_params.model` 真值（DB 里是加密的） | 要知道每行从内置价格表继承到哪个数 | 走 `/model/info` API 用 master key 读 | 不影响改动本身（改的是 `model_info.max_input_tokens`，与 slug 无关） |
| M3 | 82 lane 当前 env 开关快照 | 改网关前必须有基准，改完能对比 | `kubectl get deploy -o json` 存盘 | 无 |
| M4 | 门② 的"飞书建文档"回归到底要不要真 GUI | 决定验收成本 | 先看 `cursor_gui_e2e_driver.py` 能否无人值守跑 | 若不能，降级为"我手动在 Cursor 里跑一次并留证" |
| M5 | 198 nginx 当前 vhost/location 结构 | 新服务要挂路由 | 读 `sites-enabled` 实体文件 | 不动既有 location，只**新增**一个 |

【事实】M1–M5 是开工时确实不知道的。下文"执行日志"记录每一项的补齐结果。

---

## 2. 关键设计决策（以及为什么不是别的做法）

### 2.1 400 与 带宽 是两件独立的事，必须分开修

- 400 的根因是 LiteLLM 入口的 `max_input_tokens` 继承值（105 万），**与传多少字节无关**。
- 带宽是 Cursor 全量重发（恒定 +50.6 万字节/轮）造成的，**修了闸门也不会自己变小**。

所以：**S1 修 400，S3/S4 修带宽**。任何把两者混成一件事的方案都会漏掉一半。【事实，见飞书评审稿 §2】

### 2.2 增量协议放在"服务端重建全量"这一侧，而不是"少发给上游"

这是本方案与 08-30 那次回滚的**根本区别**：

```
今天：  Cursor --[4MB 全量]--> nginx --> LiteLLM --> 网关 --[增量 500k]--> GPT 网页
08-30： Cursor --[被改造过的增量]--> ... 上游收到的东西变了 → 破门①，回滚
本方案：Cursor --[9KB 增量]--> 小代理 --> zk-delta 服务端【重建出逐字节相同的 4MB】--> LiteLLM --> 网关（完全不动）--> GPT 网页
```

**门① 由构造保证，不靠测试运气**：zk-delta 服务端吐给 LiteLLM 的 body 与 Cursor 原本会发的 body 逐字节相同 —— 这是一条可以离线断言的性质，不是"跑几个用例没发现问题"。

实现上的关键：服务端保存的是 **body 模板**（原 body 把 `messages` 置空）。JS 里 `JSON.parse` 保留字符串键的插入序，模板里 `messages` 键本来就存在，重新赋值不改变它的位置，因此 `JSON.stringify(template)` 与原始 body 逐字节相同。【推测→将由 S3 的 golden test 证实或推翻】

### 2.3 失效必须硬报错（G4 的落点）

`handle` 认不出、`base_count` 对不上、`base_digest` 不匹配 —— 一律返回 **HTTP 409 `delta_base_not_found`**，绝不"当全量转发"。小代理收到 409 后自动重发全量并拿新 handle，且**双侧各记一条计数**。

依据：官方 Responses WebSocket 模式在 `store=false` 下的契约就是"认不出的 id 必须硬报 `previous_response_not_found`"；我们自己的 `ws-ingress` 也是这么做的（认不出就 1011 关连接）。08-30 翻车那次正是因为静默失忆。【事实】

### 2.4 不碰的东西

- 不动 zerokey 网关往 GPT 网页那一跳的任何逻辑（它是好的）
- 不动全局 `enable_pre_call_checks`
- 不改 Cursor 本体压缩包
- 不 `kubectl apply` litellm-proxy
- 不删正在服务的 Pod

【事实，来自 CLAUDE.md + memory 红线】

---

## 3. 最小可执行步骤

> 规则：不跳步。每步做完把结果写进 §5 执行日志，含"实际输出"和"验收是否通过"。

### S0 · 基准与备份

| 项 | 内容 |
|---|---|
| 输入 | 198 生产现状 |
| 动作 | ① 备份 `LiteLLM_ProxyModelTable` 中 36 行 zerokey-cursor 的 `model_info`；② 备份 CM `zk-cursor-bpi-patch` 全量；③ 存 82 lane env 快照；④ 存 nginx vhost 快照 |
| 预期输出 | `/Data/backups/zk-delta-20260831/` 下 4 个文件 |
| 验收 | 4 个文件都存在且非空；模型行备份能被 `jq` 解析 |

### S1 · 修 400：给 zerokey 模型行显式配 `max_input_tokens`

| 项 | 内容 |
|---|---|
| 输入 | S0 的备份；36 行 `model_info` |
| 动作 | 对 `model_info->>'id' LIKE 'zerokey-cursor%'` 的行，`model_info` 合并 `{"max_input_tokens": 10000000}`；随后 `rollout restart deploy/litellm-proxy` + `rollout status` |
| 预期输出 | 36 行 `mit=10000000`；4 个 proxy pod 全就绪 |
| 验收 | ① SQL 复核 36/36；② scoped-key 对 `cursor-g-5.6-sol` 打一发真实线型（chat+stream+tools+effort）返回 200 且有正文；③ 构造一个 >105 万 token 的请求不再返回 `Context Window exceeded` |
| 回滚 | 用 S0 备份还原 `model_info` + 再 restart |

### S2 · 审计：把"没配就静默继承"变成会告警的事

| 项 | 内容 |
|---|---|
| 输入 | DB |
| 动作 | 写 `scripts/litellm-198-max-input-tokens-audit.sh`：列出所有 zerokey 支撑但 `max_input_tokens` 为空的行，非空则退出码 1 |
| 预期输出 | 脚本存在；当前跑一次退出码 0（S1 之后应该全配上了） |
| 验收 | 人为把一行清空 → 脚本退出码 1 并打印该行；恢复后退出码 0 |

### S3 · 新项目 `zk-delta`：协议 + 小代理 + 服务端 + 离线金样测试

| 项 | 内容 |
|---|---|
| 输入 | M1 抓到的真实 Cursor body 样本 |
| 动作 | 建 `zk-delta/`：`protocol.md`、`server/server.js`、`sidecar/sidecar.js`、`tests/*.js`、`Dockerfile`、`k8s/` |
| 预期输出 | `node zk-delta/tests/run.js` 全绿 |
| 验收（**硬**） | ① **逐字节金样**：把一条 9 轮真实全量序列喂进 sidecar→server，服务端每轮重建出的 body 与原 body `Buffer.compare === 0`；② **上行字节**：第 9 轮 sidecar 出网字节 / 第 9 轮原始字节 < 1%；③ **失效硬报错**：篡改 handle / base_digest → 409，且**没有**任何一条被当全量转发；④ **前缀被改**（用户编辑历史）→ sidecar 自动回落全量且计数 +1 |

### S4 · 落地：部署服务端 + 本机接小代理 + 真流量验收

| 项 | 内容 |
|---|---|
| 输入 | S3 产物 |
| 动作 | ① 构建镜像（**必须在 198 上构建，不在 Mac**）；② 部署 `zk-delta` Deployment 到 `litellm-product`；③ nginx **新增**一个 location，不动既有；④ 本机起 sidecar，把我自己的 Cursor 指过去 |
| 预期输出 | 服务 Ready；`/healthz` 200 |
| 验收（**硬**） | ① **门①**：发 `ls`，网关日志里收到的 payload 与 S0 基准逐字节一致；② **门②**：shell `ls` 出结果 + 飞书建文档出结果；③ **G2**：真实多轮会话，`zkreq.log` 上行字节不随轮次线性增长；④ **G1**：跑到 ≥15 轮无 400 |
| 回滚 | sidecar 配置里地址改回 `https://cc.auto-link.com.cn/pro/v1`，一条命令，不需重启 Cursor |

### S5 · 可观测：网关每轮一条结构化账本行 + 瘦身器按形状判断

| 项 | 内容 |
|---|---|
| 输入 | CM `zk-cursor-bpi-patch` 的 `responses.js` |
| 动作 | anchor-assert 补丁：`turn conv=… items_in=… chars_in=… chars_after_diet=… branch=… session=… ide_strip=… bytes_up=… upstream=…`；`item.type !== 'message'` → 按 `role`+`content` 形状判断 |
| 预期输出 | `/tmp/responses.rN.js` 过 `node --check`；CM key 数仍为 15 |
| 验收 | ① 82 lane 日志出现 `turn ` 行；② `ide_strip` 计数 > 0（今天是 0/2648）；③ 门①门② 复测通过 |
| 门槛 | **只有 S4 全绿之后才做**。S4 不绿就不动网关 |

### S6 · 并入安装器（推广面）

| 项 | 内容 |
|---|---|
| 输入 | S4 验收通过的 sidecar |
| 动作 | `cursor_team_setup.js` 增加：写 sidecar 文件、注册自启、健康检查、默认地址切到 `127.0.0.1:8788` |
| 预期输出 | `--repair` 能修复；`--no-delta` 能退回今天形态 |
| 验收 | 在一台干净目录上跑安装器 → sidecar 起来 → `ls` 与建文档都通 |
| 门槛 | **只有 S4 的门①门② 全绿才做**。这是对全体同事生效的一步，宁可留到明天人工点头 |

### S7 · 收口

记账：更新飞书评审稿、写 memory、更新本文 §5、`git commit`。

---

## 4. 风险与停止条件

| 风险 | 停止条件 | 处理 |
|---|---|---|
| 重建 body 与原 body 不逐字节相同 | S3 验收①红 | **停止 S4**，把差异写进本文，不上线。这是可能推翻整个第三档的唯一未知项 |
| S1 之后 scoped-key 回归有任一条不通 | S1 验收②红 | 立刻用 S0 备份还原 + restart，确认恢复 |
| 门① 或门② 任一红 | S4 验收①②红 | sidecar 地址改回，本文记录失败形态 |
| 我改坏了网关 | CM key 数 ≠ 15 或 `node --check` 失败 | 不 patch，用备份还原 |
| 半夜把生产打挂 | proxy pod 非全 Ready | 立刻还原，不继续 |

【事实】以上停止条件在开工前写死，执行中不得放宽。

---

## 5. 执行日志

> 每步实际发生了什么、实际输出、验收结论。失败也照实写。
>
> **基准模型口径更正**：开工时我按 `cursor-g-5.6-sol` 做验收，用户中途指正——基准是
> `cursor-web-fc-82-terra`（82 canary 车道）。S1 之后的所有验收都以 82-terra 重做了一遍，
> 下面记的是 82-terra 的数。

### S0 · 基准与备份 —— 通过

`/Data/backups/zk-delta-20260831/` 下 4 个文件齐全非空：36 行 `model_info` 的 JSON 备份（`jq` 可解析）、
CM `zk-cursor-bpi-patch` 全量、82 lane env 快照、nginx vhost 快照
（`cc.auto-link.com.cn.conf.pre-zkd`）。

### S1 · 修 400 —— 通过

- 36 行 `model_info->>'id' LIKE 'zerokey-cursor%'` 全部合并 `{"max_input_tokens": 10000000}`，SQL 复核 36/36。
- `rollout restart deploy/litellm-proxy` → 4/4 pod 就绪。
- 同一条负载的改前 / 改后对照（真实线型：chat+stream+tools+reasoning_effort，UA `Cursor/3.17.19`）：

| 请求 | 改之前 | 改之后 |
|---|---|---|
| `cursor-web-fc-82-terra` 小包 | — | **HTTP 200**，4.0s，有正文 |
| `cursor-web-fc-82-terra` 9,100,484 B | HTTP 400 `ContextWindowExceeded`，1.2s | **HTTP 200**，50.6s |
| `cursor-web-fc-82-terra-high` / `-max` | — | 均 200 |

【事实】400 的成因不是端点格式，是 LiteLLM 入口 `_pre_call_checks` 读 `model_info.max_input_tokens`；
该字段为空时按 `litellm_params.model` 的 slug 去内置价格表继承，继承到 1,050,000。
显式写死 1000 万后闸门不再误伤。

### S2 · 审计脚本 —— 通过，两条腿都验了

`scripts/litellm-198-max-input-tokens-audit.sh`：

- 正常态：`AUDIT OK total=36 missing=0`，退出码 0。
- 证伪腿：把真实行 `zerokey-cursor-fc-101-cursor-fc-5.3-codex` 的字段清空（`UPDATE 1`）
  → 脚本打印 `MISSING zerokey-cursor-fc-101-cursor-fc-5.3-codex ... missing=1`，退出码 1 → 还原后退出码 0。

【自纠】第一次做证伪腿时我猜了个不存在的 id，`UPDATE 0`，等于根本没清空，那次的"退出码 0"毫无意义。
重新拉了真实 36 个 id 才重做。

### S3 · zk-delta 离线金样 —— 36 绿 / 1 红

`node zk-delta/tests/run.js` → **PASS 36 FAIL 1**。唯一的红是 ⑨ 真实抓包金样，`fixtures/` 还是空的
（要真 Cursor GUI 出流量才能采，见下方 S4 的说明）。这条我没有粉饰成绿。

关键实测数：

| 项 | 判据 | 实测 |
|---|---|---|
| ② a 协议开销 | 出网 / 真新增内容 < 1.05x | **1.0010x**（开销 0.10%） |
| ② b G2 不随轮次增长 | 第9轮/第1轮 < 2 | **0.419x** |
| ② c 9 轮累计省上行 | > 50% | **81.9%** |
| ③ 四类篡改 | 全 409 且零静默全量转发 | 4/4 409，上游多收 0 发 |
| ⑤ 不可复现 body | 原样透传且计数 | 逐字节相同，`roundtrip_byte_mismatch` 计数 +1 |

【自纠 · 判据】我原先在 §3 S3 写的"第 9 轮出网 < 原始的 1%"是拍脑袋定的，对着数据一算就站不住：
增量的下限就是这一轮真正新增的内容本身。事故实测里 Cursor 每轮恒定新增约 505,700 字节，
第 14 轮总量 7MB，占比天然就是 7%，永远到不了 1%。改成了上表那三条有数据支撑的。

【自纠 · 设计】§2.2 原先把"`JSON.stringify` 能逐字节还原"当成设计属性写。这不是 JS 的保证：
`1.0`→`1`、`1e3`→`1000`、`é`→字面 é、美化空白被吃掉。改成**每条请求在本机运行时自证一次**
（`JSON.stringify(rebuild(template, items)) === 原始字节`），不通过就原样透传并计数。

### S4 · 落地与真流量 —— 主体通过

**部署**：Deployment `zk-delta` 在 `aiyjy-litellm-standby` 上 Running（`RESTARTS 0`）；
ConfigMap `zk-delta-src`；Service NodePort 30404；nginx **新增** `location ^~ /zkd/`，
既有规则一条没动（`/pro/v1/models` 复核仍 200）。`https://cc.auto-link.com.cn/zkd/healthz` → `{"ok":true,...}`。

刻意没建新镜像：代码是纯 Node 标准库，复用节点上已有的 `zerokey-codex` 镜像，源码用 ConfigMap 挂进去，
`imagePullPolicy: Never`。既不用在构建服务器出镜像，也不碰任何公网仓库。

**真流量 16 轮**（真模型、真 GPT 网页号池，`cursor-web-fc-82-terra`）：

| 轮 | HTTP | 模式 | 原始 | 出网 | 省 | 重建 | 用时 | 答 |
|---|---|---|---|---|---|---|---|---|
| 1 | 200 | full | 369,358 B | 369,658 B | -0.1% | 逐字节相同 | 17.8s | one |
| 2 | 200 | delta | 492,489 B | 123,451 B | 74.9% | 逐字节相同 | 4.7s | two |
| … | | | | | | | | |
| 16 | 200 | delta | 2,216,366 B | 123,459 B | 94.4% | 逐字节相同 | 3.2s | sixteen |

```
G1  400 计数 = 0
G2  出网增长 第16轮/第1轮 = 0.334x
    累计 本来要发 19.73MB → 实际出网 2.12MB，省 89.3%
门① 重建逐字节相同 16/16，不一致 0
门② 有实际答案的轮次 16/16
VERDICT = PASS
```

出网恒定在约 123,455 字节、不随轮次涨，而原始从 369KB 涨到 2.22MB。
模型能一路数到 sixteen，本身就说明重建出来的历史是真完整的——数错就说明历史丢了。

集群侧计数（同期）：`req 20（full 3 / delta 17）· rebuild_ok 20 · reject_409 0 · upstream 2xx 20 / non-2xx 0`，
广域网收 2.43MB → 集群内发出 21.18MB，放大 **8.7x**。

**门① 网关侧对照**（比 GUI 驱动更贴近门① 原话）：同一个 `ls` 请求发两遍，一遍走
`https://cc.auto-link.com.cn/pro/v1`，一遍走本机小代理，然后比 `zero-cursor-bpi-82` 日志里
它实际收到的东西：

```
两条腿的日志块 48 行，把 nonce 归一化后 diff 零差异
promptLength: 3487   attachments: 0   chatSessionId: null
```

3487 字里绝大部分是网关自己的协议前言，用户内容就那一行。没有转录体行标、没有大 item、
没有触发上传路径。**门① 通过。**

结构上本来也应如此：重建发生在 LiteLLM **之前**，网关和 GPT 网页端根本不知道有 zk-delta 这回事。

**门② 真功能对照**（`zk-delta/tests/live_agentloop.js`）：真 agent 循环——模型真发工具调用，
测试脚本真执行，结果真喂回去，看它能不能把活干完。同一个任务两条腿各跑一遍。

| 任务 | 基准（今天的地址） | zk-delta |
|---|---|---|
| shell `ls` | 2 步 / 工具执行 1 次 / 11.1s<br>答 `alpha.txt\nbravo.md\ncharlie` | 2 步 / 工具执行 1 次 / 9.1s<br>答 **逐字相同** |
| 飞书建文档 | 2 步 / 12.0s / 出真链接 `.../docx/DAz0djYL1oxU3vxAXa3cLvOrnfd` | 2 步 / 10.7s / 出真链接 `.../docx/XUcwdNOdKo70ooxh1wccHXavnqf` |

**门② 通过。** 两条腿都把活干完，答案一致，步数一致。

判据说明：只有"基准能做到而 zk-delta 做不到"才算门② 回退。两条腿都失败说明是链路本身的问题、
不是 zk-delta 引入的——脚本对这种情况输出"无结论"而不是"通过"，不给自己留兜底。

【留痕】飞书里真建了两篇测试文档（标题前缀 `zk-delta门②验证-`），没删，可自行清理。

**小代理侧累计计数**（含以上全部真流量）：

```
req 22 · delta 18 · full 4 · passthru 0 · fallback 0 · conflict_409 0 · delta_server_error 0
原始 21,182,102 B → 出网 2,430,703 B   省 88.5%
```

22 发全部逐字节往返，零回落、零 409、零透传。

### S4 验收结论

| 验收项 | 结果 |
|---|---|
| ① 门①：网关收到的 payload 与基准逐字节一致 | **通过**（48 行日志块零差异） |
| ② 门②：shell `ls` + 飞书建文档都出结果 | **通过**（双腿对照，答案一致） |
| ③ G2：上行不随轮次线性增长 | **通过**（16 轮 0.334x） |
| ④ G1：≥15 轮无 400 | **通过**（16 轮，400 计数 0） |

### 尚未验证 / 尚未执行的部分（照实写）

| 项 | 状态 | 原因 |
|---|---|---|
| S3 ⑨ 真实抓包金样 | **红** | `fixtures/` 为空。要采真 Cursor GUI 的 body，必须先把 Cursor 指到小代理；见下方说明。**这是唯一一条红。** |
| S5 网关账本行 + 瘦身器按形状判断 | **未做** | 见下方"为什么不动网关" |
| S6 并入安装器 | **未做** | 这一步对全体同事生效，留人工点头 |

【为什么不是我自己去点 Cursor】把 Cursor 的 BYOK 地址指到小代理，必须先完全退出 Cursor GUI
（外部写 `state.vscdb` 有内存覆盖竞态，见 `cursor_team_setup.js` 开头）。用户的 Cursor 当时开着，
凌晨强退有丢未保存内容的风险，这条我不越。改成给了一条可回滚的 `zk-delta/switch.sh on|off|status`，
一条命令切过去、一条命令切回来。切过去之后 `ZKD_CAPTURE` 会自动攒真实 body，回灌重跑 `tests/run.js`
就能把 ⑨ 变绿。

【为什么不动网关（S5）】S5 有两半，风险不同：

- **账本行**（纯 `console.log`）——不改发给上游的字节，风险低。
- **瘦身器按形状判断**（`dietCursorInput` / `_stripIdeHint` 的 `item.type !== 'message'` 门）——
  这一半**会改变发给 GPT 的 payload**。而门① 的整个定义就是"payload 不许变"。
  也就是说这一半必须走它自己的一整轮门① 验证，不能搭 zk-delta 的车。

zk-delta 之所以敢在凌晨上，正是因为它对上游**零字节差异**、结构上不可能碰门①。
瘦身器改动没有这个性质，它属于另一件事，不该混在这次交付里赶工。

### S7 · 收口（已完成）

| 动作 | 结果 |
|---|---|
| 飞书评审稿补写落地结果 | 已追加 §10（10.1–10.10），revision 13 → 14，两块画板均 `warnings: []` |
| 画板 | 自绘 SVG 两张：`docs/assets/zk-delta-arch.svg`（管线形状）、`docs/assets/zk-delta-bandwidth.svg`（16 轮曲线）。已导出预览逐张肉眼核过，修掉一处标注压线后原地 `whiteboard +update` 复用同一 token，未新建空白画板 |
| memory | 新增 `project_zk_delta_deployed_2026_08_31`、`feedback_falsification_leg_must_confirm_rows_affected`；`project_cursor_chat_fullresend_context_gate_400_2026_08_30` 补「已修」一节；`MEMORY.md` 三处索引 |
| 仓库 | `zk-delta/` 全量 + 本文 + 巡检脚本，提交 `26e46d7`；画板资产与 §10 原文另提交 |

飞书 §10 的正文与本文 §5 同源，两边数字一致；图里的每个数值都取自实测，没有示意值。

### S8 · 收口之后又补的两件（已完成）

S7 之后剩下的红项只有 ⑨，而 ⑨ 卡在"必须先让 Cursor 完全退出"这个我不该越的门槛上。
于是做了两件**零风险**的事：一件把 ⑨ 想覆盖的风险面用别的办法覆盖掉，一件让已经验过的东西
不会在我睡着之后被人悄悄改回去。

#### S8-1 编码对抗性性质测试（验收项 ⑩）

`zk-delta/tests/fuzz_encoding.js`，已并入 `run.js`。

诊断三段式：

- **假设**：⑨ 尚未采集 ⇒ "逐字节可复现"这条核心性质没有被真实数据验过。
- **证伪条件**：如果这个假设成立，那就应该存在一类**只有真实 Cursor 才写得出、
  而我构造不出来**的字节形态。反过来，如果 ⑨ 想抓的风险能被完全表达成"JSON 源文本
  经过 parse → re-serialize 之后字节会不会变"，那它就与"内容是什么"无关，
  可以脱离真实抓包穷举。
- **数据**：`common/framing.js` 的 `proveRoundTrip` 全部判据只看 `JSON.stringify(...) === raw`，
  没有任何一处读 item 的语义字段。所以风险面确实只由**编码形态**张成。
  按这个结论构造 6025 条样本，跑出 8 绿 0 红（明细见 `zk-delta/README.md`）。

**它不替代 ⑨**，⑨ 仍然留红。它只是让"没采到之前"不等于"没验过"。

过程中改掉了自己写错的两条判据，记在这里免得以后又踩：

| 我原来写的 | 为什么错 |
|---|---|
| 落单代理项 `"\ud800"` 必须被拒 | `JSON.stringify` 自 ES2019 起是 well-formed 的，落单代理会被写成 `\ud800` 转义形式，与源文本一致 ⇒ 它**本来就能**逐字节复现，是合法进增量的。**是我的判据错了，不是系统错了。** |
| 混合样本池里进增量的比例要够高 | "混合池里多少比例进增量"这个数**本身没有意义**——它只反映我往对抗池里塞了多少刁钻样本。换成有产品含义的：**干净紧凑的真实形状必须 100% 进增量**（进不去 = 静默退化成全量转发 = 白装），另加一条守着 ⑩a 不空转。 |

#### S8-2 只读漂移巡检

`zk-delta/k8s/audit.sh`，退出码 0 = 全部不变量成立，1 = 有漂移。

要巡的是这几条：`replicas == 1`、`strategy == Recreate`、`imagePullPolicy == Never`、
**集群上跑的源码指纹 == 仓库这份**（源码是 ConfigMap 挂进去的，pod 是新的不等于代码是新的）、
pod Running/Ready 且重启次数、nginx `sites-enabled` 里 `/zkd/` location 还在、
公网 `/healthz` 通、以及计数器有没有在偷偷退化（`rebuild_ok == req_total - reject_409`、
上游非 2xx 为 0、409 占比 < 30%）。

**为什么值得单独写一个脚本**：上面每一条被改掉之后，服务**照样返回 200**，不会有任何报警，
只会静默退化甚至错位。这是和 `max_input_tokens` 那个坑一模一样的形状——
"没配就静默继承"是件看不见的事，所以要把它变成会告警的事。

首跑（2026-08-31 02:09）：全部不变量成立，退出码 0。

**证伪腿**（按"证伪腿必须先确认真改到行"）：往本地 `common/framing.js` 追加一行注释，
先确认文件 sha 确实变了（`1710b64e…` → `c2021c57…`，不是空转），再跑巡检 ——
[3] 两条立刻变红、退出码 1；还原后 `git diff` 为 0 行。

顺带踩到并修掉一个老坑：`"$IMG（...）"` 里全角括号紧贴变量名会被吞进变量名
（`IMG（` 成了变量名，报 unbound variable）。凡是变量后面紧跟中文标点的一律加花括号。

#### S8-3 长时程 soak

`live_multiturn.js cursor-web-fc-82-terra 30 100`，打真模型真号池：

```
轮30  原始 3,284,963B  出网 102,983B  省 96.9%  重建逐字节相同  答:"thirty"
G1  400 计数 = 0
G2  出网增长 第30轮/第1轮 = 0.334x
    累计 51.40MB → 3.14MB，省 93.9%
门① 重建逐字节相同 30/30，不一致 0
VERDICT = PASS
```

模型一路数到 thirty 一轮没错——数错就说明历史丢了，所以这本身就是重建正确性的旁证。
30 轮比 16 轮多出来的价值在于：**它证明的是长时程不漂移**，而不是又多省了几个百分点。

离线金样现在是 **44 绿 / 1 红**（红的仍只有 ⑨）。提交 `0211852`。

